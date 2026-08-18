import io
import time
import string
import secrets
import logging
import functools
from datetime import datetime
from telebot import types
import config
import database as db
import ai
from bot_instance import bot, get_bot_username
from cache import flood_limiter

start_time = time.time()

# Кэш ответов бота для кнопки "Озвучить" (в памяти — не переживает рестарт, это ок для этой фичи)
tts_cache = {}

# Кэш проверки подписки на канал
_sub_cache = {}


# ---------- ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ----------
# ВАЖНО (пункт "бот не должен падать"): раньше только process_llm_request был обёрнут в try/except.
# Если падал, например, photo_handler или genpromo_cmd – исключение улетало в поток pyTelegramBotAPI
# и просто гасилось им молча, без ответа пользователю и без записи в лог. Теперь КАЖДЫЙ хендлер
# обёрнут этим декоратором: пользователь получит вежливое сообщение, а ошибка попадёт в /lasterror.
def safe_handler(func):
    @functools.wraps(func)
    def wrapper(message_or_call, *args, **kwargs):
        try:
            return func(message_or_call, *args, **kwargs)
        except Exception as e:
            logging.error(f"Необработанная ошибка в {func.__name__}: {e}")
            db.track_error(func.__name__, e)
            try:
                chat_id = getattr(message_or_call, 'chat', None)
                chat_id = chat_id.id if chat_id else getattr(getattr(message_or_call, 'message', None), 'chat', None)
                if hasattr(chat_id, 'id'):
                    chat_id = chat_id.id
                if chat_id:
                    bot.send_message(chat_id, "❌ Что-то пошло не так. Уже записал ошибку в лог, попробуй ещё раз чуть позже.")
            except Exception:
                pass
    return wrapper


def is_admin(user_id):
    return user_id == config.OWNER_ID


def get_user_plan(user_id):
    if is_admin(user_id):
        return "pro"
    sub = db.get_subscription(user_id)
    if not sub or sub["expires_at"] < time.time():
        return "free"
    return sub["plan"]


def is_premium(user_id):
    return get_user_plan(user_id) in ("premium", "pro")


def has_plan_at_least(user_id, min_plan):
    """Проверяет, что у пользователя план не ниже указанного."""
    plan_priority = {"free": 0, "premium": 1, "pro": 2}
    user_plan = get_user_plan(user_id)
    return plan_priority.get(user_plan, 0) >= plan_priority.get(min_plan, 0)


def get_free_quota(user_id):
    """Возвращает оставшееся количество бесплатных запросов на сегодня."""
    if is_premium(user_id) or is_admin(user_id):
        return 999
    used = db.get_usage_count_today(user_id)
    return max(0, 5 - used)  # FIXME: вынести в config


def check_usage_limit(user_id):
    if is_premium(user_id) or is_admin(user_id):
        return True
    used = db.get_usage_count_today(user_id)
    return used < 5  # FIXME: вынести в config


def flood_check(message):
    if is_admin(message.from_user.id):
        return True
    if not flood_limiter.allow(message.from_user.id):
        bot.reply_to(message, config.FLOOD_COOLDOWN_MESSAGE)
        return False
    return True


def get_subscription_reminder(user_id):
    """Возвращает предупреждение об истечении подписки, если до конца осталось <= 3 дня."""
    if is_admin(user_id):
        return None
    sub = db.get_subscription(user_id)
    if not sub or sub["expires_at"] < time.time():
        return None
    days_left = (sub["expires_at"] - time.time()) / 86400
    if days_left < 0:
        return "❌ Подписка истекла. Продли: /premium"
    if days_left <= 1:
        return "⚠️ Подписка заканчивается сегодня! Продли: /premium"
    if days_left <= 3:
        return f"⚠️ Подписка истекает через {round(days_left)} дня. Продли: /premium"
    return None


# ---------- ПОДПИСКА НА КАНАЛ ----------
def is_subscribed_to_channel(user_id):
    if is_admin(user_id):
        return True
    now = time.time()
    cached = _sub_cache.get(user_id)
    if cached and now - cached[1] < config.SUB_CACHE_TTL:
        return cached[0]
    try:
        member = bot.get_chat_member(config.CHANNEL_USERNAME, user_id)
        status = member.status in ['creator', 'administrator', 'member']
    except Exception as e:
        logging.error(f"Ошибка проверки подписки: {e}")
        status = False
    _sub_cache[user_id] = (status, now)
    return status


def subscribe_prompt_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{config.CHANNEL_USERNAME.lstrip('@')}"))
    markup.add(types.InlineKeyboardButton("✅ Проверить", callback_data="check_sub"))
    return markup


def require_subscription(message):
    if is_subscribed_to_channel(message.from_user.id):
        return True
    bot.send_message(message.chat.id,
                     f"👋 Чтобы пользоваться ботом, подпишись на канал: {config.CHANNEL_USERNAME}",
                     parse_mode="HTML", reply_markup=subscribe_prompt_markup())
    return False


# Декоратор для проверки подписки
def subscription_required(func):
    @functools.wraps(func)
    def wrapper(message, *args, **kwargs):
        if not require_subscription(message):
            return
        return func(message, *args, **kwargs)
    return wrapper
# ---------- РЕФЕРАЛ ----------
def handle_referral(new_user_id, referrer_id):
    if new_user_id == referrer_id:
        return
    if not db.get_user(referrer_id):
        return
    new_user = db.get_user(new_user_id)
    if new_user and new_user.get("referred_by"):
        return
    db.set_referred_by(new_user_id, referrer_id)
    count = db.increment_referral_count(referrer_id)
    if count % config.REFERRAL_BONUS_EVERY == 0:
        db.extend_subscription(referrer_id, "premium", config.REFERRAL_BONUS_DAYS)
        try:
            bot.send_message(referrer_id, f"🎉 Ты пригласил уже {count} друзей! Начислено +{config.REFERRAL_BONUS_DAYS} дня Premium.")
        except Exception as e:
            logging.error(f"Не удалось уведомить о реферальном бонусе: {e}")


# ---------- КЛАВИАТУРА ----------
def get_main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    keyboard.add(
        types.KeyboardButton("❓ Помощь"),
        types.KeyboardButton("⭐ Премиум"),
        types.KeyboardButton("🔍 Поиск"),
        types.KeyboardButton("🖼 Фото"),
        types.KeyboardButton("🗑 Очистить")
    )
    return keyboard


# ---------- ОЗВУЧКА ПО КНОПКЕ ----------
@bot.callback_query_handler(func=lambda call: call.data == "tts_read")
@safe_handler
def tts_callback(call):
    key = f"{call.message.chat.id}:{call.message.message_id}"
    text = tts_cache.get(key)
    if not text:
        bot.answer_callback_query(call.id, "Текст недоступен (бот перезапускался).", show_alert=True)
        return
    bot.answer_callback_query(call.id, "🎤 Озвучиваю...")
    bot.send_chat_action(call.message.chat.id, 'record_voice')
    audio = ai.text_to_speech(text)
    if audio:
        bot.send_audio(call.message.chat.id, audio, title="Zelmy AI")
    else:
        bot.send_message(call.message.chat.id, "❌ Не получилось озвучить.")


def _cache_tts(chat_id, message_id, text):
    tts_cache[f"{chat_id}:{message_id}"] = text
    if len(tts_cache) > 500:
        tts_cache.pop(next(iter(tts_cache)), None)


# ---------- ПОТОКОВЫЙ ОТВЕТ ----------
def stream_reply(chat_id, messages, original_message=None, existing_message_id=None, max_tokens=2000):
    if existing_message_id:
        placeholder_id = existing_message_id
        try:
            bot.edit_message_text("⏳ Печатаю ещё раз...", chat_id, placeholder_id)
        except Exception:
            pass
    else:
        try:
            placeholder = bot.reply_to(original_message, "⏳ Печатаю...") if original_message else bot.send_message(chat_id, "⏳ Печатаю...")
            placeholder_id = placeholder.message_id
        except Exception as e:
            logging.error(f"Не удалось отправить плейсхолдер: {e}")
            return None, None

    last_edit_time = [0]
    last_shown = [""]

    def on_delta(full_text):
        now = time.time()
        if now - last_edit_time[0] >= config.STREAM_EDIT_INTERVAL and full_text != last_shown[0]:
            try:
                bot.edit_message_text(full_text + " ▌", chat_id, placeholder_id)
                last_edit_time[0] = now
                last_shown[0] = full_text
            except Exception:
                pass

    out = ai.stream_groq_completion(messages, on_delta, max_tokens=max_tokens)
    return out, placeholder_id
# ---------- ОСНОВНАЯ ЛОГИКА ----------
def process_llm_request(chat_id, user_id, text, original_message=None):
    if db.is_banned(user_id):
        bot.send_message(chat_id, "🚫 Вы забанены.")
        return

    if not is_admin(user_id) and not is_premium(user_id) and not check_usage_limit(user_id):
        bot.send_message(chat_id, f"❌ Бесплатный лимит исчерпан. Осталось: {get_free_quota(user_id)} запросов сегодня.\nКупи подписку: /premium")
        return

    def plain_reply(msg, **kwargs):
        if original_message:
            bot.reply_to(original_message, msg, **kwargs)
        else:
            bot.send_message(chat_id, msg, **kwargs)

    # Фильтр экстремизма
    if ai.is_extremism_related(text):
        db.track_event("extremism_blocked")
        plain_reply(ai.EXTREMISM_REFUSAL)
        return

    db.track_event("message")
    db.track_daily_active(user_id)

    # Проверка на поисковый запрос
    lowered = text.lower()
    if any(p in lowered for p in ['кто я', 'кто я?', 'я кто', 'кто твой']):
        # ... (обработка вопросов о личности)
        pass

    # Добавляем сообщение в историю
    memory_limit = config.HISTORY_LIMIT_PRO if get_user_plan(user_id) == "pro" else config.HISTORY_LIMIT_DEFAULT
    db.add_history_message(chat_id, "user", text)
    history = db.get_history(chat_id, memory_limit)

    # Формируем системный промпт
    persona_prompt = config.PERSONAS.get(db.get_persona(user_id), config.PERSONAS["default"])
    sys_prompt = {"role": "system", "content": (
        f"{persona_prompt}\n"
        "Если не знаешь - скажи честно.\n"
        f"{config.FORMATTING_INSTRUCTION}\n"
        f"{config.EXTREMISM_SYSTEM_RULE}"
    )}

    messages = [sys_prompt] + history

    out, placeholder_id = stream_reply(chat_id, messages, original_message)

    if not out:
        out, placeholder_id = stream_reply(chat_id, messages, original_message, existing_message_id=placeholder_id)

    if out:
        db.add_history_message(chat_id, "assistant", out)
        reminder = get_subscription_reminder(user_id)
        final_text = out + (f"\n\n{reminder}" if reminder else "")

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔊 Озвучить", callback_data="tts_read"))

        try:
            bot.edit_message_text(final_text, chat_id, placeholder_id, reply_markup=markup)
            _cache_tts(chat_id, placeholder_id, final_text)
        except Exception as e:
            logging.error(f"Не удалось финализировать потоковое сообщение: {e}")
    else:
        db.track_error("process_llm_request", "stream_reply вернул пусто")
        error_msg = "❌ Не получилось получить ответ. Попробуй ещё раз."
        if placeholder_id:
            try:
                bot.edit_message_text(error_msg, chat_id, placeholder_id)
            except Exception:
                plain_reply(error_msg)
        else:
            plain_reply(error_msg)


def send_ai_reply(chat_id, text, original_message=None):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔊 Озвучить", callback_data="tts_read"))
    try:
        sent = bot.reply_to(original_message, text, reply_markup=markup) if original_message else bot.send_message(chat_id, text, reply_markup=markup)
    except Exception as e:
        logging.error(f"Ошибка отправки ответа: {e}")
        return None
    _cache_tts(chat_id, sent.message_id, text)
    return sent


# ============== КОМАНДЫ ==============

@bot.message_handler(commands=['start'])
@safe_handler
def start_cmd(message):
    is_new = db.register_user(message.from_user.id, message.from_user.username or "нет_имени", message.from_user.first_name or "Без имени")
    parts = message.text.split(maxsplit=1)

    if is_new and len(parts) > 1 and parts[1].startswith("ref_"):
        ref_id = parts[1][4:]
        if ref_id.isdigit():
            handle_referral(message.from_user.id, int(ref_id))

    if is_new:
        reward = db.grant_campaign_reward_if_eligible(message.from_user.id)
        if reward:
            plan, days = reward
            bot.send_message(message.chat.id, f"🎉 Тебе повезло – ты попал в акцию! Начислен тариф <b>{plan}</b> на {days} дней.", parse_mode="HTML")

    if not is_subscribed_to_channel(message.from_user.id):
        bot.send_message(message.chat.id,
                         "👋 <b>Привет, я Zelmy AI!</b>\n\nПодпишись на канал, чтобы пользоваться ботом: @ZelmyAI",
                         parse_mode="HTML", reply_markup=subscribe_prompt_markup())
        return

    bot.send_message(message.chat.id,
                     "🔥 <b>Zelmy AI v8.0</b>\n\n"
                     "📌 <b>Что я умею:</b>\n"
                     "• Отвечать на любые вопросы (+ 🔊 озвучка ответа)\n"
                     "• Искать в интернете: <code>/search ...</code>\n"
                     "• Озвучивать текст: <code>/voice ...</code>\n"
                     "• Распознавать текст с фото и картинок-файлов\n"
                     "• Понимать голосовые сообщения\n"
                     "• Помнить историю диалога до 30 дней\n\n"
                     "💰 <b>Подписка:</b>\n"
                     "• Бесплатно: 5 запросов/день\n"
                     "• Premium (30★): безлимит + фото\n"
                     "• Pro (50★): безлимит + фото + озвучка + свой стиль\n\n"
                     "💬 Просто напиши мне сообщение!",
                     parse_mode="HTML", reply_markup=get_main_keyboard())
@bot.message_handler(commands=['help'])
@subscription_required
@safe_handler
def help_cmd(message):
    text = (
        "📖 <b>Команды Zelmy AI:</b>\n\n"
        "/start - перезапустить бота\n"
        "/help - список команд\n"
        "/premium - тарифы и подписка\n"
        "/promo КОД - активировать промокод\n"
        "/invite - пригласить друга и получить Premium бесплатно\n"
        "/profile - мой профиль\n"
        "/mystats - моя статистика\n"
        "/status - статус бота\n"
        "/search [запрос] - поиск в интернете\n"
        "/voice [текст] - озвучить текст\n"
        "/persona [стиль] - сменить стиль общения (Pro)\n"
        "/clear - очистить историю\n\n"
        "📷 Отправь фото или картинку-файл - я распознаю текст с них.\n"
        "🎤 Отправь голосовое сообщение - я распознаю речь."
    )
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['premium'])
@subscription_required
@safe_handler
def premium_cmd(message):
    plan = get_user_plan(message.from_user.id)
    if plan != "free":
        bot.reply_to(message, f"✅ У тебя уже есть подписка <b>{plan}</b>", parse_mode="HTML")
        return

    enabled_methods = "\n".join(f"• {p['label']}" for p in config.PAYMENT_PROVIDERS.values() if p["enabled"])
    text = (
        "⭐ <b>Zelmy AI Premium</b>\n\n"
        "💰 <b>Тарифы:</b>\n"
        "• Premium (30★): безлимит + фото + больше памяти\n"
        "• Pro (50★): безлимит + фото + озвучка + свой стиль\n\n"
        f"💳 Способы оплаты сейчас:\n{enabled_methods}\n\n"
        "🎫 Или активируй промокод: <code>/promo КОД</code>"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⭐ 30★ - Premium", callback_data="buy_premium"))
    markup.add(types.InlineKeyboardButton("⭐ 50★ - Pro", callback_data="buy_pro"))
    bot.reply_to(message, text, parse_mode="HTML", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data in ['buy_premium', 'buy_pro'])
@safe_handler
def buy_callback(call):
    plan = call.data.split('_')[1]
    price = 30 if plan == "premium" else 50
    title = "Zelmy AI Premium" if plan == "premium" else "Zelmy AI Pro"
    desc = "Безлимит + фото" if plan == "premium" else "Безлимит + фото + озвучка + свой стиль"
    bot.send_invoice(call.message.chat.id,
                     title=title,
                     description=desc,
                     invoice_payload=plan,
                     provider_token="",
                     currency="XTR",
                     prices=[{"label": "Подписка на 30 дней", "amount": price}],
                     start_parameter="sub")
    bot.answer_callback_query(call.id)


@bot.pre_checkout_query_handler(func=lambda query: True)
@safe_handler
def pre_checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@bot.message_handler(content_types=['successful_payment'])
@safe_handler
def successful_payment(message):
    plan = message.successful_payment.invoice_payload
    if plan in ("premium", "pro"):
        db.extend_subscription(message.from_user.id, plan, 30)
        bot.send_message(message.chat.id, f"✅ Подписка <b>{plan}</b> активирована на 30 дней!", parse_mode="HTML")
    else:
        logging.error(f"Неизвестный payload подписки: {plan}")
# ---------- ПРОМОКОДЫ ----------
def generate_promo_code(length=10):
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


@bot.message_handler(commands=['genpromo'])
@safe_handler
def genpromo_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 3:
        bot.reply_to(message, "Использование:\n<code>/genpromo premium 30</code>\n<code>/genpromo pro 30 5</code>", parse_mode="HTML")
        return

    plan = parts[1].lower()
    if plan not in ("premium", "pro"):
        bot.reply_to(message, "❌ План должен быть premium или pro")
        return

    try:
        days = int(parts[2])
        max_uses = int(parts[3]) if len(parts) > 3 else 1
    except ValueError:
        bot.reply_to(message, "❌ Дни и число активаций должны быть целыми числами")
        return

    code = generate_promo_code()
    db.create_promocode(code, plan, days, max_uses, message.from_user.id)
    bot.reply_to(message, f"✅ Промокод создан:\n<code>{code}</code>\nТариф: <b>{plan}</b>, {days} дней, активаций: {max_uses}", parse_mode="HTML")


@bot.message_handler(commands=['promo'])
@subscription_required
@safe_handler
def promo_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Введи промокод: <code>/promo КОД</code>", parse_mode="HTML")
        return

    code = parts[1].strip().upper()
    user_id = message.from_user.id

    promo = db.get_promocode(code)
    if not promo:
        bot.reply_to(message, "❌ Промокод не найден.")
        return

    if db.has_used_promocode(code, user_id):
        bot.reply_to(message, "❌ Ты уже активировал этот промокод.")
        return

    if db.promocode_use_count(code) >= promo["max_uses"]:
        bot.reply_to(message, "❌ Лимит активаций этого промокода исчерпан.")
        return

    db.extend_subscription(user_id, promo["plan"], promo["days"])
    db.redeem_promocode(code, user_id)
    bot.reply_to(message, f"✅ Промокод активирован! Тариф <b>{promo['plan']}</b> на {promo['days']} дней.", parse_mode="HTML")


@bot.message_handler(commands=['invite'])
@subscription_required
@safe_handler
def invite_cmd(message):
    user_id = message.from_user.id
    link = f"https://t.me/{get_bot_username()}?start=ref_{user_id}"
    user = db.get_user(user_id) or {}
    count = user.get("referral_count", 0)
    left = config.REFERRAL_BONUS_EVERY - (count % config.REFERRAL_BONUS_EVERY)
    text = (
        "👥 <b>Пригласи друзей</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: {count}\n"
        f"Ещё {left} до следующих {config.REFERRAL_BONUS_DAYS} дней Premium 🎁"
    )
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['campaign'])
@safe_handler
def campaign_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()

    if len(parts) == 2 and parts[1] == "stop":
        db.stop_campaign()
        bot.reply_to(message, "✅ Акция остановлена.")
        return

    if len(parts) < 4:
        bot.reply_to(message, "Использование:\n<code>/campaign premium 30 10</code>\n<code>/campaign stop</code>", parse_mode="HTML")
        return

    plan = parts[1].lower()
    if plan not in ("premium", "pro"):
        bot.reply_to(message, "❌ План должен быть premium или pro")
        return

    try:
        days, slots = int(parts[2]), int(parts[3])
    except ValueError:
        bot.reply_to(message, "❌ Дни и число слотов должны быть целыми числами")
        return

    db.start_campaign(plan, days, slots)
    bot.reply_to(message, f"✅ Акция запущена: первым {slots} новым пользователям - <b>{plan}</b> на {days} дней.", parse_mode="HTML")


@bot.message_handler(commands=['persona'])
@subscription_required
@safe_handler
def persona_cmd(message):
    user_id = message.from_user.id
    if not has_plan_at_least(user_id, "pro"):
        bot.reply_to(message, "❌ Выбор стиля общения доступен на тарифе Pro (50★). Оформи: /premium")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip().lower() not in config.PERSONAS:
        bot.reply_to(message, f"❌ Выбери стиль: <code>/persona default|friendly|expert|funny</code>", parse_mode="HTML")
        return

    chosen = parts[1].strip().lower()
    db.set_persona(user_id, chosen)
    bot.reply_to(message, f"✅ Стиль общения изменён на <b>{chosen}</b>", parse_mode="HTML")


@bot.message_handler(commands=['profile'])
@subscription_required
@safe_handler
def profile_cmd(message):
    user_id = message.from_user.id
    plan = get_user_plan(user_id)
    free_quota = get_free_quota(user_id) if plan == "free" else "∞"
    text = f"👤 <b>Твой профиль</b>\n\nСтатус: <b>{plan}</b>\n\nОсталось запросов: {free_quota}\nID: {user_id}"
    if is_admin(user_id):
        text += "\n👑 Создатель – безлимит на всё"
    elif plan != "free":
        sub = db.get_subscription(user_id)
        if sub and sub["expires_at"]:
            date = datetime.fromtimestamp(sub["expires_at"]).strftime("%d.%m.%Y")
            text += f"\n📅 Действует до: {date}"
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['mystats'])
@subscription_required
@safe_handler
def mystats_cmd(message):
    user_id = message.from_user.id
    user = db.get_user(user_id) or {}
    text = (
        "📊 <b>Твоя статистика</b>\n\n"
        f"📅 С нами с: {user.get('first_seen', '-')}\n"
        f"⭐ Тариф: <b>{get_user_plan(user_id)}</b>\n"
        f"👥 Приглашено друзей: {user.get('referral_count', 0)}\n\n"
        "🎁 Хочешь Премиум бесплатно? - <code>/invite</code>"
    )
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['status'])
@subscription_required
@safe_handler
def status_cmd(message):
    uptime = time.time() - start_time
    hours, minutes = int(uptime // 3600), int((uptime % 3600) // 60)
    text = (
        "📊 <b>Статус бота</b>\n\n"
        f"⏱ Время работы: {hours}ч {minutes}м\n"
        f"👥 Пользователей: {len(db.get_all_users())}\n"
        f"⭐ Подписок: {db.count_active_subscriptions()}\n"
        f"🧠 Модель: {config.CURRENT_MODEL}"
    )
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['clear'])
@subscription_required
@safe_handler
def clear_cmd(message):
    db.clear_history(message.chat.id)
    bot.reply_to(message, "🗑 История очищена!")


@bot.message_handler(commands=['search'])
@subscription_required
@safe_handler
def search_cmd(message):
    if not flood_check(message):
        return
    parts = message.text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        bot.reply_to(message, "Напиши запрос: <code>/search курс доллара</code>", parse_mode="HTML")
        return
    process_llm_request(message.chat.id, message.from_user.id, f"найди {query}", message)
@bot.message_handler(commands=['image'])
@subscription_required
@safe_handler
def image_cmd(message):
    # Генерация временно отключена
    bot.reply_to(message, "🖼 Генерация картинок временно отключена на техническое обслуживание.")


@bot.message_handler(commands=['voice'])
@subscription_required
@safe_handler
def voice_cmd(message):
    user_id = message.from_user.id
    if db.is_banned(user_id):
        bot.reply_to(message, "🚫 Вы забанены.")
        return
    if not has_plan_at_least(user_id, "pro"):
        bot.reply_to(message, "🔊 Озвучка текста доступна только на тарифе Pro (50★). Оформи: /premium")
        return
    if not flood_check(message):
        return

    parts = message.text.split(maxsplit=1)
    text_to_read = parts[1].strip() if len(parts) > 1 else ""
    if not text_to_read:
        bot.reply_to(message, "Напиши текст: <code>/voice привет, как дела</code>", parse_mode="HTML")
        return

    bot.send_chat_action(message.chat.id, 'record_voice')
    audio = ai.text_to_speech(text_to_read)
    if audio:
        bot.send_audio(message.chat.id, audio, title="Zelmy AI", reply_to_message_id=message.message_id)
    else:
        bot.reply_to(message, "❌ Не получилось озвучить текст.")


# ---------- АДМИНКА ----------
@bot.message_handler(commands=['admin', 'users'])
@safe_handler
def admin_users_cmd(message):
    if not is_admin(message.from_user.id):
        return
    users = db.get_all_users()
    if not users:
        bot.reply_to(message, "Пользователей пока нет.")
        return

    lines = []
    for u in users:
        plan = get_user_plan(u["id"])
        ban_mark = " 🚫 BAN" if u.get('banned') else ""
        lines.append(f"<code>{u['id']}</code> - @{u.get('username', '-')} - {u.get('first_name', '')} - <b>{plan}</b>{ban_mark}")

    chunk = f"<b>Пользователи ({len(users)}):</b>\n\n"
    for line in lines:
        if len(chunk) + len(line) > 3500:
            bot.send_message(message.chat.id, chunk, parse_mode="HTML")
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        bot.send_message(message.chat.id, chunk, parse_mode="HTML")


@bot.message_handler(commands=['stats'])
@safe_handler
def stats_cmd(message):
    if not is_admin(message.from_user.id):
        return
    events = db.get_all_events()
    events_lines = "\n".join(f"{name}: {count}" for name, count in events) or "(пока пусто)"
    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: {len(db.get_all_users())}\n"
        f"📅 Активных сегодня: {db.get_active_today_count()}\n"
        f"⭐ Активных подписок: {db.count_active_subscriptions()}\n\n"
        f"📈 <b>События (за всё время):</b>\n{events_lines}\n\n"
        f"🐛 Последних ошибок в логе: {db.get_error_count()}\n"
        "Подробности ошибок – <code>/lasterror</code>"
    )
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['lasterror'])
@safe_handler
def lasterror_cmd(message):
    if not is_admin(message.from_user.id):
        return
    errors = db.get_last_errors(5)
    if not errors:
        bot.reply_to(message, "Ошибок пока не зафиксировано.")
        return
    text = "<b>Последние ошибки:</b>\n\n" + "\n\n".join(
        f"<code>{e['time']}</code> [{e['context']}]\n{e['error']}" for e in errors
    )
    bot.reply_to(message, text[:4000], parse_mode="HTML")


@bot.message_handler(commands=['ban'])
@safe_handler
def ban_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Использование: <code>/ban USER_ID</code>", parse_mode="HTML")
        return
    uid = int(parts[1])
    if not db.get_user(uid):
        bot.reply_to(message, "Пользователь не найден в базе.")
        return
    db.set_banned(uid, True)
    bot.reply_to(message, f"✅ Пользователь {uid} забанен.")


@bot.message_handler(commands=['unban'])
@safe_handler
def unban_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Использование: <code>/unban USER_ID</code>", parse_mode="HTML")
        return
    uid = int(parts[1])
    if not db.get_user(uid):
        bot.reply_to(message, "Пользователь не найден в базе.")
        return
    db.set_banned(uid, False)
    bot.reply_to(message, f"✅ Пользователь {uid} разбанен.")
# ---------- ФОТО / ДОКУМЕНТЫ / ГОЛОС ----------
@bot.message_handler(content_types=['photo'])
@subscription_required
@safe_handler
def photo_handler(message):
    user_id = message.from_user.id
    if db.is_banned(user_id):
        bot.reply_to(message, "🚫 Вы забанены.")
        return
    if not has_plan_at_least(user_id, "premium"):
        bot.reply_to(message, "📷 Распознавание текста с фото доступно с тарифа Premium (30★). Оформи: /premium")
        return
    if not flood_check(message):
        return

    bot.send_chat_action(message.chat.id, 'typing')
    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded = bot.download_file(file_info.file_path)
    text = ai.extract_text_from_image(downloaded)
    bot.reply_to(message, f"📷 <b>Текст с фото:</b>\n\n{text}", parse_mode="HTML")


@bot.message_handler(content_types=['document'])
@subscription_required
@safe_handler
def document_handler(message):
    user_id = message.from_user.id
    if db.is_banned(user_id):
        bot.reply_to(message, "🚫 Вы забанены.")
        return

    mime = message.document.mime_type or ""
    if not mime.startswith("image/"):
        bot.reply_to(message, "📄 Я умею распознавать текст только с изображений.")
        return

    if not has_plan_at_least(user_id, "premium"):
        bot.reply_to(message, "📷 Распознавание текста с фото доступно с тарифа Premium (30★). Оформи: /premium")
        return
    if not flood_check(message):
        return

    bot.send_chat_action(message.chat.id, 'typing')
    file_info = bot.get_file(message.document.file_id)
    downloaded = bot.download_file(file_info.file_path)
    text = ai.extract_text_from_image(downloaded)
    bot.reply_to(message, f"📷 <b>Текст с изображения:</b>\n\n{text}", parse_mode="HTML")


@bot.message_handler(content_types=['voice'])
@subscription_required
@safe_handler
def voice_message_handler(message):
    user_id = message.from_user.id
    if db.is_banned(user_id):
        bot.reply_to(message, "🚫 Вы забанены.")
        return
    if not flood_check(message):
        return

    bot.send_chat_action(message.chat.id, 'typing')
    file_info = bot.get_file(message.voice.file_id)
    downloaded = bot.download_file(file_info.file_path)
    text = ai.transcribe_voice(downloaded)

    if not text:
        bot.reply_to(message, "❌ Не удалось распознать голосовое сообщение.")
        return

    process_llm_request(message.chat.id, user_id, text, message)


# ---------- ТЕКСТ И КНОПКИ КЛАВИАТУРЫ ----------
BUTTON_ACTIONS = {
    "❓ Помощь": lambda m: help_cmd(m),
    "⭐ Премиум": lambda m: premium_cmd(m),
    "🗑 Очистить": lambda m: clear_cmd(m),
}


@bot.message_handler(func=lambda message: True, content_types=['text'])
@subscription_required
@safe_handler
def text_handler(message):
    text = message.text.strip()

    if text in BUTTON_ACTIONS:
        BUTTON_ACTIONS[text](message)
        return

    if text == "🔍 Поиск":
        bot.reply_to(message, "Напиши запрос после /search", parse_mode="HTML")
        return

    if text == "🖼 Фото":
        bot.reply_to(message, "📷 Просто отправь мне фото – я распознаю текст на нём.")
        return

    if not flood_check(message):
        return

    process_llm_request(message.chat.id, message.from_user.id, text, message)
# ---------- ОБРАБОТЧИК ПРОВЕРКИ ПОДПИСКИ ----------
@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
@safe_handler
def check_sub_callback(call):
    if is_subscribed_to_channel(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ Подписка подтверждена! Используй бота.", show_alert=False)
        bot.edit_message_text("✅ Подписка подтверждена! Теперь ты можешь пользоваться ботом.", call.message.chat.id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "❌ Ты ещё не подписался!", show_alert=True)
