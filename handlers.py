import time
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

# ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК
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

# ---------- ПОДПИСКА НА КАНАЛ ----------
_sub_cache = {}

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
    markup.add(types.InlineKeyboardButton("📌 ПОДПИСАТЬСЯ", url=f"https://t.me/{config.CHANNEL_USERNAME.lstrip('@')}"))
    markup.add(types.InlineKeyboardButton("✅ Проверить", callback_data="check_sub"))
    return markup

def require_subscription(message):
    if is_subscribed_to_channel(message.from_user.id):
        return True
    bot.send_message(message.chat.id, f"Чтобы пользоваться ботом, подпишись на канал {config.CHANNEL_USERNAME}", reply_markup=subscribe_prompt_markup())
    return False

def subscription_required(handler_func):
    @functools.wraps(handler_func)
    def wrapper(message, *args, **kwargs):
        if not require_subscription(message):
            return
        return handler_func(message, *args, **kwargs)
    return wrapper

# ---------- АНТИФЛУД ----------
def flood_check(message):
    if is_admin(message.from_user.id):
        return True
    if not flood_limiter.allow(message.from_user.id):
        bot.reply_to(message, config.FLOOD_COOLDOWN_MESSAGE)
        return False
    return True

# ---------- РЕФЕРАЛЫ ----------
def handle_referral(new_user_id, referrer_id):
    if new_user_id == referrer_id:
        return
    if not db.get_user(referrer_id):
        return
    new_user = db.get_user(new_user_id)
    if new_user and new_user.get("referred_by"):
        return
    db.set_referred_by(new_user_id, referrer_id)
    db.increment_referral_count(referrer_id)
    # ---------- КЛАВИАТУРА ----------
def get_main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    keyboard.add(
        types.KeyboardButton("📖 Помощь"),
        types.KeyboardButton("🔍 Поиск"),
        types.KeyboardButton("🧹 Очистить"),
        types.KeyboardButton("ℹ️ Статус")
    )
    return keyboard

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
                bot.edit_message_text(full_text + "▌", chat_id, placeholder_id)
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

    def plain_reply(msg, **kwargs):
        if original_message:
            bot.reply_to(original_message, msg, **kwargs)
        else:
            bot.send_message(chat_id, msg, **kwargs)

    if ai.is_extremism_related(text):
        db.track_event("extremism_blocked")
        plain_reply(ai.EXTREMISM_REFUSAL)
        return

    db.track_event("message")
    db.track_daily_active(user_id)

    lowered = text.lower()

    if any(p in lowered for p in ['кто я', 'кто я?', 'я кто', 'кто твой создатель', 'чей ты бот']):
        plain_reply("Ты – Zelmy Create, мой создатель." if user_id == config.OWNER_ID else "Мой создатель – Zelmy Create.")
        return
    if "президент россии" in lowered and "2026" in lowered:
        plain_reply("Президент России в 2026 году – Владимир Путин.")
        return
    if "президент сша" in lowered and "2026" in lowered:
        plain_reply("Президент США в 2026 году – Дональд Трамп.")
        return

    if any(w in lowered for w in ['найди', 'поищи', 'найти', 'поиск', '/search']):
        db.track_event("search")
        results = ai.search_web(text)
        if results:
            out = "<b>Результаты поиска:</b>\n\n"
            for res in results:
                out += f"<b>{res['title']}</b>\n{res['snippet']}\n<a href='{res['link']}'>Источник</a>\n\n"
            plain_reply(out, parse_mode="HTML", disable_web_page_preview=True)
            return
        fallback = ai.groq_completion_simple([
            {"role": "system", "content": "Ответь на вопрос пользователя, используя свои знания."},
            {"role": "user", "content": text}
        ])
        if fallback:
            plain_reply(f"🔍 {fallback}")
        else:
            plain_reply("Ничего не нашёл. Попробуй переформулировать запрос.")
        return

    db.add_history_message(chat_id, "user", text)
    history = db.get_recent_history(chat_id, config.HISTORY_LIMIT)

    persona_prompt = config.PERSONAS.get(db.get_persona(user_id), config.PERSONAS["default"])
    sys_prompt = {
        "role": "system",
        "content": (
            f"{persona_prompt}\n"
            "Если не знаешь – скажи честно.\n"
            f"{config.FORMATTING_INSTRUCTION}\n"
            f"{config.EXTREMISM_SYSTEM_RULE}"
        )
    }
    messages = [sys_prompt] + history

    out, placeholder_id = stream_reply(chat_id, messages, original_message)
    if not out:
        out, placeholder_id = stream_reply(chat_id, messages, original_message, existing_message_id=placeholder_id)

    if out:
        db.add_history_message(chat_id, "assistant", out)
        try:
            bot.edit_message_text(out, chat_id, placeholder_id)
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
# ============================================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================================

@bot.message_handler(commands=['start'])
@safe_handler
def start_cmd(message):
    is_new = db.track_user(
        message.from_user.id,
        message.from_user.username or "нет_юзернейма",
        message.from_user.first_name or "Без имени"
    )
    parts = message.text.split(maxsplit=1)
    if is_new and len(parts) > 1 and parts[1].startswith("ref_"):
        ref_id = parts[1][4:]
        if ref_id.isdigit():
            handle_referral(message.from_user.id, int(ref_id))

    if not is_subscribed_to_channel(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "👋 <b>Привет, я Zelmy AI!</b>\n\nПодпишись на канал, чтобы пользоваться ботом: @ZelmyAI",
            parse_mode="HTML",
            reply_markup=subscribe_prompt_markup()
        )
        return

    # ---------- ПРИВЕТСТВЕННОЕ СООБЩЕНИЕ ----------
    welcome_text = (
        "Привет! 👋 Я твой ИИ-помощник Zelmy. Рад познакомиться!\n\n"
        "Я здесь, чтобы помогать тебе с идеями, отвечать на вопросы, "
        "разбираться со сложными задачами и просто быть полезным, "
        "когда тебе это нужно. Можешь общаться со мной как обычно — "
        "без формальностей.\n\n"
        "Ну что, с чего начнём? 🚀"
    )

    bot.send_message(
        message.chat.id,
        welcome_text,
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
@safe_handler
def check_sub_callback(call):
    member = bot.get_chat_member(config.CHANNEL_USERNAME, call.from_user.id)
    if member.status in ['creator', 'administrator', 'member']:
        _sub_cache[call.from_user.id] = (True, time.time())
        bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        start_cmd(call.message)
    else:
        bot.answer_callback_query(call.id, "❌ Ты ещё не подписался!", show_alert=True)

@bot.message_handler(commands=['help'])
@subscription_required
@safe_handler
def help_cmd(message):
    text = (
        "<b>Команды Zelmy AI</b> (всё бесплатно):\n\n"
        "/start - перезапустить бота\n"
        "/help - список команд\n"
        "/invite - пригласить друга\n"
        "/profile - мой профиль\n"
        "/mystats - моя статистика\n"
        "/status - статус бота\n"
        "/search [запрос] - поиск в интернете\n"
        "/persona [стиль] - сменить стиль общения\n"
        "/clear - очистить историю"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['persona'])
@subscription_required
@safe_handler
def persona_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip().lower() not in config.PERSONAS:
        bot.reply_to(message, "Выбери стиль: <code>/persona default|friendly|expert|funny</code>", parse_mode="HTML")
        return
    chosen = parts[1].strip().lower()
    db.set_persona(message.from_user.id, chosen)
    bot.reply_to(message, f"Стиль общения изменён на <b>{chosen}</b>", parse_mode="HTML")

@bot.message_handler(commands=['invite'])
@subscription_required
@safe_handler
def invite_cmd(message):
    user_id = message.from_user.id
    link = f"https://t.me/{get_bot_username()}?start=ref_{user_id}"
    user = db.get_user(user_id) or {}
    text = (
        "<b>Пригласи друзей</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: {user.get('referral_count', 0)}"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['profile'])
@subscription_required
@safe_handler
def profile_cmd(message):
    user_id = message.from_user.id
    text = f"<b>Твой профиль</b>\n\nID: {user_id}\nДоступ: все функции бесплатно"
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['mystats'])
@subscription_required
@safe_handler
def mystats_cmd(message):
    user_id = message.from_user.id
    user = db.get_user(user_id) or {}
    text = (
        "<b>Твоя статистика</b>\n\n"
        f"С нами с: {user.get('first_seen', '-')}\n"
        f"Приглашено друзей: {user.get('referral_count', 0)}"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['status'])
@subscription_required
@safe_handler
def status_cmd(message):
    uptime = time.time() - start_time
    hours, minutes = int(uptime // 3600), int((uptime % 3600) // 60)
    text = (
        "<b>Статус бота</b>\n\n"
        f"Время работы: {hours}ч {minutes}м\n"
        f"Пользователей: {len(db.get_all_users())}\n"
        f"Модель: {config.CURRENT_MODEL}"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['clear'])
@subscription_required
@safe_handler
def clear_cmd(message):
    db.clear_history(message.chat.id)
    bot.reply_to(message, "🧹 История очищена!")

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
        ban_mark = "🚫" if u.get('banned') else ""
        lines.append(f"<code>{u['id']}</code> - @{u.get('username','-')} - {u.get('first_name','')} {ban_mark}")
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
    events_lines = "\n".join(f"• {name}: {count}" for name, count in events) or "(пока пусто)"
    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"Всего пользователей: {len(db.get_all_users())}\n"
        f"Активных сегодня: {db.get_active_today_count()}\n\n"
        f"<b>События (за всё время):</b>\n{events_lines}\n\n"
        f"Последних ошибок в логе: {db.get_error_count()}\n"
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
    bot.reply_to(message, f"🚫 Пользователь {uid} забанен.")

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
    # ---------- КНОПКИ КЛАВИАТУРЫ ----------
@bot.message_handler(func=lambda message: message.text == "📖 Помощь")
@subscription_required
@safe_handler
def help_button_handler(message):
    help_cmd(message)

@bot.message_handler(func=lambda message: message.text == "🔍 Поиск")
@subscription_required
@safe_handler
def search_button_handler(message):
    bot.reply_to(message, "Напиши запрос после команды:\n<code>/search что ищешь</code>", parse_mode="HTML")

@bot.message_handler(func=lambda message: message.text == "🧹 Очистить")
@subscription_required
@safe_handler
def clear_button_handler(message):
    clear_cmd(message)

@bot.message_handler(func=lambda message: message.text == "ℹ️ Статус")
@subscription_required
@safe_handler
def status_button_handler(message):
    status_cmd(message)

# ---------- ТЕКСТОВЫЕ СООБЩЕНИЯ ----------
@bot.message_handler(func=lambda message: True)
@subscription_required
@safe_handler
def text_handler(message):
    if not flood_check(message):
        return
    process_llm_request(message.chat.id, message.from_user.id, message.text, message)
