import json
import os
import time
import requests
import telebot
from telebot import types
import logging
import re
import base64
from datetime import datetime, timedelta

# --- 1. ЛОГИРОВАНИЕ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 2. КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
APIFY_KEY = os.getenv("APIFY_KEY")
OWNER_ID = 8482782819
CHANNEL_USERNAME = "@ZelmyAI"

HISTORY_FILE = "chat_history.json"
USERS_FILE = "users.json"
SUBSCRIPTIONS_FILE = "subscriptions.json"
USAGE_FILE = "usage.json"

bot = telebot.TeleBot(BOT_TOKEN)
CURRENT_MODEL = "llama-3.1-8b-instant"

# --- 3. РАБОТА С БАЗАМИ ---
def load_json(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_json(filepath, data):
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass

history_db = load_json(HISTORY_FILE)
users_db = load_json(USERS_FILE)
subscriptions = load_json(SUBSCRIPTIONS_FILE)
usage_db = load_json(USAGE_FILE)

def track_user(user):
    str_id = str(user.id)
    users_db[str_id] = {
        "username": user.username or "нет_юзернейма",
        "first_name": user.first_name or "Без имени"
    }
    save_json(USERS_FILE, users_db)

# --- 4. ПОДПИСКИ ---
def is_premium(user_id):
    user_id = str(user_id)
    if user_id not in subscriptions:
        return False
    sub = subscriptions[user_id]
    return sub.get('expires_at', 0) > time.time()

def get_user_plan(user_id):
    user_id = str(user_id)
    if user_id not in subscriptions:
        return "free"
    sub = subscriptions[user_id]
    if sub.get('expires_at', 0) < time.time():
        return "free"
    return sub.get('plan', 'free')

def check_usage_limit(user_id):
    if user_id == OWNER_ID or is_premium(user_id):
        return True
    user_id = str(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    if user_id not in usage_db:
        usage_db[user_id] = {}
    if usage_db[user_id].get('date') != today:
        usage_db[user_id] = {'date': today, 'count': 0}
    if usage_db[user_id]['count'] >= 5:
        return False
    usage_db[user_id]['count'] += 1
    save_json(USAGE_FILE, usage_db)
    return True

def get_subscription_reminder(user_id):
    user_id = str(user_id)
    if user_id not in subscriptions:
        return None
    sub = subscriptions[user_id]
    expires_at = sub.get('expires_at', 0)
    days_left = (expires_at - time.time()) / 86400
    if days_left < 0:
        return "❌ Твоя подписка истекла. Чтобы продлить — напиши /premium"
    if days_left <= 1:
        return "⚠️ Братан, у тебя последний день подписки! Продли её: /premium"
    if days_left <= 3:
        return f"⏳ Напоминаю: подписка истекает через {round(days_left)} дня. Продли: /premium"
    return None

# --- 5. ПОИСК ЧЕРЕЗ APIFY BRAVE ---
def search_apify_brave(query):
    try:
        url = "https://api.apify.com/v2/acts/miroslav~brave-search/runs"
        params = {"token": APIFY_KEY}
        payload = {"query": query, "count": 5}
        response = requests.post(url, json=payload, params=params, timeout=15)
        if response.status_code != 200:
            return None
        data = response.json()
        results = []
        for item in data.get('data', {}).get('web', {}).get('results', []):
            results.append({
                'title': item.get('title', 'Без заголовка'),
                'link': item.get('url', ''),
                'snippet': item.get('description', '')[:300]
            })
        return results[:5]
    except Exception as e:
        logging.error(f"Apify ошибка: {e}")
        return None

# --- 6. УЛУЧШЕНИЕ ПРОМПТА ДЛЯ КАРТИНОК ---
def enhance_prompt_with_groq(simple_prompt):
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": "Ты — эксперт по написанию промптов для нейросетей. Преврати простой запрос пользователя в подробный, качественный промпт для генерации изображений. Добавь детали, стиль, освещение, качество. Не добавляй людей."},
                    {"role": "user", "content": f"Улучши этот запрос для генерации картинки: {simple_prompt}"}
                ],
                "temperature": 0.7
            },
            timeout=10
        )
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
        return simple_prompt
    except:
        return simple_prompt

# --- 7. ГЕНЕРАЦИЯ КАРТИНОК ---
def generate_image(prompt):
    try:
        safe_prompt = f"realistic, high quality, detailed, no people, no nudity, safe: {prompt}"
        url = f"https://image.pollinations.ai/prompt/{safe_prompt.replace(' ', '%20')}?safe=true"
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            return response.content
        return None
    except Exception as e:
        logging.error(f"Генерация картинки ошибка: {e}")
        return None

# --- 8. КЛАВИАТУРА ---
def get_main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn1 = types.KeyboardButton("📖 Помощь")
    btn2 = types.KeyboardButton("🌟 Премиум")
    btn3 = types.KeyboardButton("🔍 Найди")
    btn4 = types.KeyboardButton("🎨 Нарисуй")
    btn5 = types.KeyboardButton("📸 Фото")
    btn6 = types.KeyboardButton("🗑 Сброс")
    keyboard.add(btn1, btn2, btn3, btn4, btn5, btn6)
    return keyboard

# --- 9. ОСНОВНАЯ ЛОГИКА ---
def process_llm_request(chat_id, user_id, text, original_message=None):
    str_chat_id = str(chat_id)
    
    if not check_usage_limit(user_id) and not is_premium(user_id):
        reply = "❌ Бесплатный лимит (5 запросов/день) исчерпан. Купи подписку: /premium"
        if original_message:
            bot.reply_to(original_message, reply)
        else:
            bot.send_message(chat_id, reply)
        return
    
    try:
        bot.send_chat_action(chat_id, 'typing')
        
        creator_phrases = ['кто я', 'кто я?', 'я кто', 'ты признаешь себя', 'ты считаешь себя', 'ты мой создатель', 'ты создатель', 'кто мой создатель', 'чей ты бот']
        if any(phrase in text.lower() for phrase in creator_phrases):
            if user_id == OWNER_ID:
                reply = "Ты — Zelmy Create, мой создатель. Я всегда буду помнить это."
            else:
                reply = "Мой создатель — Zelmy Create. Он единственный, кто управляет мной."
            if original_message:
                bot.reply_to(original_message, reply)
            else:
                bot.send_message(chat_id, reply)
            return

        if any(word in text.lower() for word in ['найди', 'поищи', 'найти', 'поиск']):
            search_results = search_apify_brave(text)
            if search_results:
                reply = "🔍 <b>Результаты поиска:</b>\n\n"
                for res in search_results:
                    reply += f"• <b>{res['title']}</b>\n{res['snippet']}\n<a href='{res['link']}'>Источник</a>\n\n"
                if original_message:
                    bot.reply_to(original_message, reply, parse_mode="HTML")
                else:
                    bot.send_message(chat_id, reply, parse_mode="HTML")
                return
            else:
                reply = "🌐 Ничего не нашёл. Попробуй переформулировать запрос."
                if original_message:
                    bot.reply_to(original_message, reply)
                else:
                    bot.send_message(chat_id, reply)
                return

        if text.lower().startswith('нарисуй') or text.lower().startswith('сгенерируй'):
            if user_id != OWNER_ID and not is_premium(user_id):
                reply = "❌ Генерация картинок доступна только по подписке! /premium"
                if original_message:
                    bot.reply_to(original_message, reply)
                else:
                    bot.send_message(chat_id, reply)
                return
            prompt = text[8:].strip()
            if not prompt:
                reply = "❌ Напиши, что нарисовать: <code>нарисуй кота</code>"
                if original_message:
                    bot.reply_to(original_message, reply, parse_mode="HTML")
                else:
                    bot.send_message(chat_id, reply, parse_mode="HTML")
                return
            bot.send_message(chat_id, "🎨 Генерирую картинку... Подожди 5-10 секунд.")
            enhanced_prompt = enhance_prompt_with_groq(prompt)
            image_data = generate_image(enhanced_prompt)
            if image_data:
                bot.send_photo(chat_id, image_data, caption=f"🖼️ Сгенерировано по запросу: {prompt}")
            else:
                bot.send_message(chat_id, "❌ Не удалось сгенерировать картинку. Попробуй позже.")
            return

        if str_chat_id not in history_db:
            history_db[str_chat_id] = []

        history_db[str_chat_id].append({"role": "user", "content": text})
        if len(history_db[str_chat_id]) > 100:
            history_db[str_chat_id] = history_db[str_chat_id][-100:]

        sys_prompt = {"role": "system", "content": (
            "Ты — Zelmy AI, мощный ИИ-ассистент.\n"
            "Отвечай развернуто, используя свои знания.\n"
            "Если не знаешь — честно скажи 'я не знаю'."
        )}

        payload = [sys_prompt] + history_db[str_chat_id]

        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": CURRENT_MODEL,
                "messages": payload,
                "temperature": 0.5
            },
            timeout=30
        )

        if response.status_code == 200:
            reply = response.json()['choices'][0]['message']['content']
            history_db[str_chat_id].append({"role": "assistant", "content": reply})
            save_json(HISTORY_FILE, history_db)
            
            reminder = get_subscription_reminder(user_id)
            if reminder:
                reply += f"\n\n{reminder}"
            
            if original_message:
                bot.reply_to(original_message, reply)
            else:
                bot.send_message(chat_id, reply)
        else:
            error_text = f"❌ Ошибка Groq: {response.status_code}"
            if original_message:
                bot.reply_to(original_message, error_text)
            else:
                bot.send_message(chat_id, error_text)

    except Exception as e:
        logging.error(f"Ошибка: {e}")
        error_text = f"❌ Ошибка: {str(e)[:200]}"
        try:
            if original_message:
                bot.reply_to(original_message, error_text)
            else:
                bot.send_message(chat_id, error_text)
        except:
            pass

# --- 10. ЗРЕНИЕ (РАБОТАЕТ ДЛЯ ВСЕХ, КТО МОЖЕТ) ---
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    track_user(message.from_user)
    user_id = message.from_user.id

    # Проверяем: владелец ИЛИ подписка
    if user_id != OWNER_ID and not is_premium(user_id):
        bot.reply_to(
            message,
            "📸 <b>Зрение доступно только по подписке!</b>\n\n"
            "Оформи Premium за 30 Stars/месяц и я смогу:\n"
            "• Описывать картинки\n"
            "• Отвечать на вопросы по фото\n"
            "• Распознавать текст на изображениях\n\n"
            "👉 /premium — чтобы оформить подписку",
            parse_mode="HTML"
        )
        return

    bot.reply_to(message, "📸 Анализирую изображение... Подожди пару секунд.")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        image_base64 = base64.b64encode(downloaded_file).decode('utf-8')
        
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "qwen/qwen3.6-27b",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Опиши, что изображено на этой картинке, подробно, но кратко (до 5 предложений)."},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                        ]
                    }
                ]
            },
            timeout=30
        )
        
        if response.status_code == 200:
            reply = response.json()['choices'][0]['message']['content']
            bot.reply_to(message, f"🖼️ <b>Описание:</b>\n{reply}", parse_mode="HTML")
        else:
            bot.reply_to(message, "❌ Не удалось распознать изображение. Попробуй позже.")
            
    except Exception as e:
        logging.error(f"Ошибка обработки фото: {e}")
        bot.reply_to(message, "⚠️ Произошла ошибка при обработке фото.")

# --- 11. КНОПКИ КЛАВИАТУРЫ ---
@bot.message_handler(func=lambda msg: msg.text == "📖 Помощь")
def help_button(msg):
    bot.reply_to(msg, "📖 Напиши /help")

@bot.message_handler(func=lambda msg: msg.text == "🌟 Премиум")
def premium_button(msg):
    premium_cmd(msg)

@bot.message_handler(func=lambda msg: msg.text == "🔍 Найди")
def search_button(msg):
    bot.reply_to(msg, "✏️ Напиши: <code>найди ...</code>", parse_mode="HTML")

@bot.message_handler(func=lambda msg: msg.text == "🎨 Нарисуй")
def draw_button(msg):
    bot.reply_to(msg, "✏️ Напиши: <code>нарисуй ...</code>", parse_mode="HTML")

@bot.message_handler(func=lambda msg: msg.text == "📸 Фото")
def photo_button(msg):
    bot.reply_to(msg, "📸 Отправь мне фото, и я опишу его (доступно по подписке)")

@bot.message_handler(func=lambda msg: msg.text == "🗑 Сброс")
def reset_button(msg):
    reset_history(msg)

# --- 12. КОМАНДЫ ---
@bot.message_handler(commands=['start'])
def start_cmd(message):
    logging.info(f"Start от {message.from_user.id}")
    track_user(message.from_user)
    str_chat_id = str(message.chat.id)
    history_db[str_chat_id] = []
    save_json(HISTORY_FILE, history_db)

    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, message.from_user.id)
        is_subscribed = member.status in ['creator', 'administrator', 'member']
    except:
        is_subscribed = False

    if not is_subscribed and message.from_user.id != OWNER_ID:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📢 Подписаться на канал", url="https://t.me/ZelmyAI"))
        markup.add(types.InlineKeyboardButton("✅ Я подписался", callback_data="check_sub"))
        welcome_text = (
            "👋 <b>Привет, я Zelmy AI!</b>\n\n"
            "Я — твой мощный ИИ-помощник с доступом к интернету.\n\n"
            "🤖 <b>Что я умею:</b>\n"
            "• Отвечать на любые вопросы (ИИ Groq)\n"
            "• Искать информацию: <code>найди ...</code>\n"
            "• Генерировать картинки: <code>нарисуй ...</code>\n"
            "• Распознавать фото (по подписке)\n\n"
            "👨‍💻 <b>Мой создатель:</b> Zelmy Create\n\n"
            "📢 <b>Чтобы пользоваться ботом, подпишись на канал:</b>"
        )
        bot.send_message(message.chat.id, welcome_text, parse_mode="HTML", reply_markup=markup)
        return

    keyboard = get_main_keyboard()
    welcome_text = (
        "🔥 <b>Zelmy AI — ПЛАТИНОВАЯ ВЕРСИЯ</b>\n\n"
        "📌 <b>Что я умею:</b>\n"
        "• Искать в интернете: <code>найди ...</code>\n"
        "• Генерировать картинки: <code>нарисуй ...</code>\n"
        "• Распознавать фото (по подписке)\n"
        "• Отвечать на любые вопросы (Groq)\n"
        "• Помнить до 100 сообщений диалога\n\n"
        "💰 <b>Подписка:</b>\n"
        "• Бесплатно: 5 запросов/день\n"
        "• Premium: 30 Stars/мес — безлимит\n"
        "• Pro: 50 Stars/мес — + генерация картинок и зрение\n\n"
        "📌 <b>Команды:</b>\n"
        "/premium, /reset, /model, /stats\n\n"
        "📢 <b>Наш канал:</b> <a href='https://t.me/ZelmyAI'>@ZelmyAI</a>"
    )
    bot.send_message(message.chat.id, welcome_text, parse_mode="HTML", reply_markup=keyboard)

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def callback_check_sub(call):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, call.from_user.id)
        if member.status in ['creator', 'administrator', 'member']:
            bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
            bot.delete_message(call.message.chat.id, call.message.message_id)
            start_cmd(call.message)
        else:
            bot.answer_callback_query(call.id, "❌ Ты ещё не подписался!", show_alert=True)
    except:
        bot.answer_callback_query(call.id, "❌ Ошибка проверки.", show_alert=True)

@bot.message_handler(commands=['help'])
def show_help(message):
    text = (
        "🤖 <b>Команды Zelmy AI:</b>\n\n"
        "/start — перезапустить\n"
        "/premium — тарифы и подписка\n\n"
        "<code>найди ...</code> — поиск в интернете\n"
        "<code>нарисуй ...</code> — генерация картинки (Premium)\n"
        "📸 Отправь фото — описание (Premium)\n\n"
        "/reset, /model, /stats — админские"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['premium'])
def premium_cmd(message):
    user_id = message.from_user.id
    plan = get_user_plan(user_id)
    if plan != "free":
        bot.reply_to(message, f"🌟 У тебя уже есть подписка <b>{plan}</b>", parse_mode="HTML")
        return
    text = "🌟 <b>Zelmy AI Premium</b>\n\n💰 <b>Тарифы:</b>\n• 30 Stars/мес — Premium\n• 50 Stars/мес — Pro\n\n📌 Нажми на кнопку ниже для оплаты."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💎 30 Stars — Premium", callback_data="buy_premium"))
    markup.add(types.InlineKeyboardButton("🌟 50 Stars — Pro", callback_data="buy_pro"))
    bot.reply_to(message, text, parse_mode="HTML", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ['buy_premium', 'buy_pro'])
def handle_purchase(call):
    plan = call.data.split('_')[1]
    price = 30 if plan == "premium" else 50
    title = "Zelmy AI Premium" if plan == "premium" else "Zelmy AI Pro"
    desc = "Безлимит запросов и поиска" if plan == "premium" else "Всё из Premium + генерация картинок и зрение"
    try:
        bot.send_invoice(
            call.message.chat.id,
            title=title,
            description=desc,
            invoice_payload=plan,
            provider_token="",
            currency="XTR",
            prices=[{"label": "Подписка на 30 дней", "amount": price}],
            start_parameter="sub"
        )
        bot.answer_callback_query(call.id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка: {e}", show_alert=True)

@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def successful_payment(message):
    user_id = str(message.from_user.id)
    plan = message.successful_payment.invoice_payload
    if plan == "premium":
        subscriptions[user_id] = {'plan': 'premium', 'expires_at': time.time() + 30 * 24 * 60 * 60}
    elif plan == "pro":
        subscriptions[user_id] = {'plan': 'pro', 'expires_at': time.time() + 30 * 24 * 60 * 60}
    save_json(SUBSCRIPTIONS_FILE, subscriptions)
    bot.send_message(message.chat.id, f"✅ Подписка <b>{plan}</b> активирована на 30 дней! Спасибо!", parse_mode="HTML")

@bot.message_handler(commands=['model'])
def switch_model(message):
    global CURRENT_MODEL
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Только создатель.")
        return
    if CURRENT_MODEL == "llama-3.1-8b-instant":
        CURRENT_MODEL = "llama-3.3-70b-versatile"
        bot.reply_to(message, "✅ 70B модель")
    else:
        CURRENT_MODEL = "llama-3.1-8b-instant"
        bot.reply_to(message, "✅ 8B модель")

@bot.message_handler(commands=['reset'])
def reset_history(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Только создатель.")
        return
    str_chat_id = str(message.chat.id)
    history_db[str_chat_id] = []
    save_json(HISTORY_FILE, history_db)
    bot.reply_to(message, "🧹 История очищена!")

@bot.message_handler(commands=['stats'])
def show_stats(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Доступ запрещен.")
        return
    total_users = len(users_db)
    total_subs = len(subscriptions)
    bot.reply_to(message,
        f"📊 <b>Статистика:</b>\n"
        f"👤 Пользователей: {total_users}\n"
        f"🌟 Подписок: {total_subs}\n"
        f"⚙️ Модель: {CURRENT_MODEL}",
        parse_mode="HTML"
    )

# --- 13. ГОЛОСОВЫЕ ---
@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    logging.info(f"Голос от {message.from_user.id}")
    track_user(message.from_user)
    voice_path = f"voice_{message.from_user.id}.ogg"
    try:
        bot.send_chat_action(message.chat.id, 'typing')
        file_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        with open(voice_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        with open(voice_path, 'rb') as audio_file:
            response = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_KEY}"},
                files={"file": (voice_path, audio_file, "audio/ogg")},
                data={"model": "whisper-large-v3"}
            )
        if response.status_code == 200:
            recognized_text = response.json().get('text', '')
            bot.reply_to(message, f"🎤 Распознано: {recognized_text}")
            process_llm_request(message.chat.id, message.from_user.id, recognized_text, message)
        else:
            bot.reply_to(message, "❌ Не удалось распознать.")
    except Exception as e:
        bot.reply_to(message, f"⚠️ Ошибка: {str(e)[:200]}")
    finally:
        if os.path.exists(voice_path):
            os.remove(voice_path)

# --- 14. ТЕКСТ ---
@bot.message_handler(func=lambda message: True)
def handle_text(message):
    logging.info(f"Текст от {message.from_user.id}: {message.text[:50] if message.text else 'пусто'}")
    track_user(message.from_user)
    process_llm_request(message.chat.id, message.from_user.id, message.text, message)

# --- 15. ЗАПУСК ---
print("="*50)
print("🤖 **Zelmy AI PLATINUM v3.0**")
print("✅ Зрение + Поиск + Картинки + Подписка")
print("="*50)

while True:
    try:
        bot.polling(none_stop=True, timeout=60)
    except Exception as e:
        logging.error(f"Сбой: {e}")
        print(f"⚠️ Переподключение через 5 секунд...")
        time.sleep(5)
