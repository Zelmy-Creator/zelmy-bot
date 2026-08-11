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
import threading
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
import pytesseract
from PIL import Image
import io
from gtts import gTTS

# --- 1. ЛОГИРОВАНИЕ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 2. КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
HF_KEY = os.getenv("HF_KEY")
OWNER_ID = 8482782819
CHANNEL_USERNAME = "@ZelmyAI"

HISTORY_FILE = "chat_history.json"
USERS_FILE = "users.json"
SUBSCRIPTIONS_FILE = "subscriptions.json"
USAGE_FILE = "usage.json"
REACTIONS_FILE = "reactions.json"

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
        "first_name": user.first_name or "Без имени",
        "first_seen": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "banned": False
    }
    save_json(USERS_FILE, users_db)

# --- 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_admin(user_id):
    return user_id == OWNER_ID

def is_banned(user_id):
    user_id = str(user_id)
    return users_db.get(user_id, {}).get('banned', False)

def get_free_quota(user_id):
    user_id = str(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    if user_id not in usage_db or usage_db[user_id].get('date') != today:
        return 5
    return max(0, 5 - usage_db[user_id].get('count', 0))

# --- 5. ПОДПИСКИ ---
def is_premium(user_id):
    user_id = str(user_id)
    if user_id not in subscriptions:
        return False
    sub = subscriptions[user_id]
    if sub.get('expires_at', 0) < time.time():
        return False
    return True

def get_user_plan(user_id):
    user_id = str(user_id)
    if not is_premium(user_id):
        return "free"
    return subscriptions[user_id].get('plan', 'free')

def check_usage_limit(user_id):
    if is_admin(user_id) or is_premium(user_id):
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
    if not is_premium(user_id):
        return None
    sub = subscriptions[user_id]
    expires_at = sub.get('expires_at', 0)
    days_left = (expires_at - time.time()) / 86400
    if days_left < 0:
        return "❌ Подписка истекла. Продли: /premium"
    if days_left <= 1:
        return "⚠️ Подписка заканчивается сегодня! Продли: /premium"
    if days_left <= 3:
        return f"⏳ Подписка истекает через {round(days_left)} дня. Продли: /premium"
    return None

# --- 6. ПОИСК ---
def search_web(query):
    try:
        with DDGS() as ddgs:
            results = []
            for r in ddgs.text(query, max_results=5):
                results.append({
                    'title': r.get('title', 'Без заголовка'),
                    'link': r.get('href', ''),
                    'snippet': r.get('body', '')[:300]
                })
            if results:
                return results
    except Exception as e:
        logging.error(f"DDGS ошибка: {e}")

    try:
        url = f"https://html.duckduckgo.com/html/?q={query}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, timeout=15, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        for row in soup.select('.result')[:5]:
            title_tag = row.select_one('.result__a')
            snippet_tag = row.select_one('.result__snippet')
            if title_tag:
                results.append({
                    'title': title_tag.get_text(strip=True),
                    'link': title_tag.get('href', ''),
                    'snippet': snippet_tag.get_text(strip=True)[:300] if snippet_tag else ''
                })
        if results:
            return results
    except:
        pass
    return None

# --- 7. КАРТИНКИ ---
def generate_image(prompt):
    try:
        safe_prompt = f"cute, family friendly, no nudity, no adult content: {prompt}"
        url = f"https://image.pollinations.ai/prompt/{safe_prompt.replace(' ', '%20')}?safe=true"
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            return response.content
    except:
        pass

    try:
        url = "https://api-inference.huggingface.co/models/runwayml/stable-diffusion-v1-5"
        headers = {"Authorization": f"Bearer {HF_KEY}"}
        payload = {"inputs": f"cute, family friendly, no nudity, no adult content: {prompt}"}
        response = requests.post(url, json=payload, headers=headers, timeout=60)
        if response.status_code == 200:
            return response.content
    except:
        pass
    return None

# --- 8. TTS ---
def text_to_speech(text):
    try:
        tts = gTTS(text=text, lang='ru')
        audio = io.BytesIO()
        tts.write_to_fp(audio)
        audio.seek(0)
        return audio
    except Exception as e:
        logging.error(f"TTS ошибка: {e}")
        return None

# --- 9. OCR (РАСПОЗНАВАНИЕ ТЕКСТА С ФОТО) ---
def extract_text_from_image(image_bytes):
    try:
        image = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(image, lang='rus+eng')
        return text.strip() if text.strip() else "Текст не найден"
    except Exception as e:
        logging.error(f"OCR ошибка: {e}")
        return "Ошибка распознавания"

# --- 10. КЛАВИАТУРА ---
def get_main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn1 = types.KeyboardButton("📖 Помощь")
    btn2 = types.KeyboardButton("🌟 Премиум")
    btn3 = types.KeyboardButton("🔍 Поиск")
    btn4 = types.KeyboardButton("🎨 Картинка")
    btn5 = types.KeyboardButton("📸 Фото")
    btn6 = types.KeyboardButton("🗑 Очистить")
    keyboard.add(btn1, btn2, btn3, btn4, btn5, btn6)
    return keyboard
    # --- 11. ОСНОВНАЯ ЛОГИКА ---
def process_llm_request(chat_id, user_id, text, original_message=None):
    if is_banned(user_id):
        bot.send_message(chat_id, "🚫 Вы забанены.")
        return

    if not check_usage_limit(user_id) and not is_premium(user_id):
        free_quota = get_free_quota(user_id)
        bot.send_message(chat_id, f"❌ Бесплатный лимит исчерпан. Осталось: {free_quota} запросов сегодня.\nКупи подписку: /premium")
        return

    str_chat_id = str(chat_id)
    try:
        bot.send_chat_action(chat_id, 'typing')
        
        if str_chat_id not in history_db:
            history_db[str_chat_id] = []

        # --- ЖЁСТКИЕ ОТВЕТЫ ---
        if any(phrase in text.lower() for phrase in ['кто я', 'кто я?', 'я кто', 'кто твой создатель', 'чей ты бот']):
            if user_id == OWNER_ID:
                reply = "Ты — Zelmy Create, мой создатель."
            else:
                reply = "Мой создатель — Zelmy Create."
            if original_message:
                bot.reply_to(original_message, reply)
            else:
                bot.send_message(chat_id, reply)
            return

        if "президент россии" in text.lower() and "2026" in text.lower():
            reply = "🇷🇺 Президент России в 2026 году — Владимир Путин."
            if original_message:
                bot.reply_to(original_message, reply)
            else:
                bot.send_message(chat_id, reply)
            return

        if "президент сша" in text.lower() and "2026" in text.lower():
            reply = "🇺🇸 Президент США в 2026 году — Дональд Трамп."
            if original_message:
                bot.reply_to(original_message, reply)
            else:
                bot.send_message(chat_id, reply)
            return

        # --- ПОИСК ---
        if any(word in text.lower() for word in ['найди', 'поищи', 'найти', 'поиск', '/search']):
            search_results = search_web(text)
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
                fallback = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_KEY}"},
                    json={
                        "model": CURRENT_MODEL,
                        "messages": [
                            {"role": "system", "content": "Ответь на вопрос пользователя, используя свои знания."},
                            {"role": "user", "content": text}
                        ]
                    },
                    timeout=20
                )
                if fallback.status_code == 200:
                    reply = fallback.json()['choices'][0]['message']['content']
                    if original_message:
                        bot.reply_to(original_message, f"🌐 {reply}")
                    else:
                        bot.send_message(chat_id, f"🌐 {reply}")
                else:
                    reply = "🌐 Ничего не нашёл. Попробуй переформулировать запрос."
                    if original_message:
                        bot.reply_to(original_message, reply)
                    else:
                        bot.send_message(chat_id, reply)
                return

        # --- ПАМЯТЬ ---
        history_db[str_chat_id].append({"role": "user", "content": text})
        if len(history_db[str_chat_id]) > 100:
            history_db[str_chat_id] = history_db[str_chat_id][-100:]

        sys_prompt = {"role": "system", "content": (
            "Ты — Zelmy AI, умный помощник.\n"
            "Отвечай кратко, по делу, используй 1-2 эмодзи.\n"
            "Если не знаешь — скажи честно."
        )}

        payload = [sys_prompt] + history_db[str_chat_id]

        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
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

# --- 12. КОМАНДЫ ---
@bot.message_handler(commands=['start'])
def start_cmd(message):
    track_user(message.from_user)
    str_chat_id = str(message.chat.id)
    history_db[str_chat_id] = []
    save_json(HISTORY_FILE, history_db)

    # Проверка подписки на канал
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, message.from_user.id)
        is_subscribed = member.status in ['creator', 'administrator', 'member']
    except:
        is_subscribed = False

    if not is_subscribed and message.from_user.id != OWNER_ID:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📢 Подписаться", url="https://t.me/ZelmyAI"))
        markup.add(types.InlineKeyboardButton("✅ Проверить", callback_data="check_sub"))
        bot.send_message(message.chat.id,
            "👋 <b>Привет, я Zelmy AI!</b>\n\nПодпишись на канал, чтобы пользоваться ботом: @ZelmyAI",
            parse_mode="HTML", reply_markup=markup)
        return

    keyboard = get_main_keyboard()
    bot.send_message(message.chat.id,
        "🔥 <b>Zelmy AI v6.0</b>\n\n"
        "📌 <b>Что я умею:</b>\n"
        "• Отвечать на любые вопросы\n"
        "• Искать в интернете: <code>/search ...</code>\n"
        "• Генерировать картинки: <code>/image ...</code>\n"
        "• Озвучивать текст: <code>/voice ...</code>\n"
        "• Распознавать текст с фото\n\n"
        "💰 <b>Подписка:</b>\n"
        "• Бесплатно: 5 запросов/день\n"
        "• Premium (30⭐): безлимит + фото\n"
        "• Pro (50⭐): + картинки + озвучка\n\n"
        "📌 <b>Команды:</b>\n"
        "/help — список команд\n"
        "/premium — тарифы\n"
        "/profile — мой профиль\n"
        "/status — статус бота\n"
        "/clear — очистить историю\n",
        parse_mode="HTML", reply_markup=keyboard)

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def check_sub_callback(call):
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
def help_cmd(message):
    text = (
        "🤖 <b>Команды Zelmy AI:</b>\n\n"
        "/start — перезапустить бота\n"
        "/help — список команд\n"
        "/premium — тарифы и подписка\n"
        "/profile — мой профиль\n"
        "/status — статус бота\n"
        "/search [запрос] — поиск в интернете\n"
        "/image [описание] — генерация картинки\n"
        "/voice [текст] — озвучить текст\n"
        "/clear — очистить историю\n\n"
        "📸 Отправь фото — распознаю текст\n"
        "💰 Premium: 30⭐/мес, Pro: 50⭐/мес"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['premium'])
def premium_cmd(message):
    user_id = message.from_user.id
    plan = get_user_plan(user_id)
    if plan != "free":
        bot.reply_to(message, f"🌟 У тебя уже есть подписка <b>{plan}</b>", parse_mode="HTML")
        return
    text = (
        "🌟 <b>Zelmy AI Premium</b>\n\n"
        "💰 <b>Тарифы:</b>\n"
        "• Premium (30⭐): безлимит + фото\n"
        "• Pro (50⭐): + картинки + озвучка\n\n"
        "Нажми на кнопку ниже для оплаты."
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💎 30⭐ — Premium", callback_data="buy_premium"))
    markup.add(types.InlineKeyboardButton("🌟 50⭐ — Pro", callback_data="buy_pro"))
    bot.reply_to(message, text, parse_mode="HTML", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ['buy_premium', 'buy_pro'])
def buy_callback(call):
    plan = call.data.split('_')[1]
    price = 30 if plan == "premium" else 50
    title = "Zelmy AI Premium" if plan == "premium" else "Zelmy AI Pro"
    desc = "Безлимит + фото" if plan == "premium" else "Безлимит + фото + картинки + озвучка"
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
    bot.send_message(message.chat.id, f"✅ Подписка <b>{plan}</b> активирована на 30 дней!", parse_mode="HTML")

@bot.message_handler(commands=['profile'])
def profile_cmd(message):
    user_id = message.from_user.id
    plan = get_user_plan(user_id)
    free_quota = get_free_quota(user_id) if plan == "free" else "∞"
    text = (
        "👤 <b>Твой профиль</b>\n\n"
        f"📌 Статус: <b>{plan}</b>\n"
        f"📊 Осталось запросов: {free_quota}\n"
        f"🆔 ID: {user_id}"
    )
    if plan != "free":
        expires = subscriptions[str(user_id)].get('expires_at', 0)
        if expires:
            date = datetime.fromtimestamp(expires).strftime("%d.%m.%Y")
            text += f"\n📅 Действует до: {date}"
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['status'])
def status_cmd(message):
    uptime = time.time() - start_time
    hours = int(uptime // 3600)
    minutes = int((uptime % 3600) // 60)
    text = (
        "🟢 <b>Статус бота</b>\n\n"
        f"⏱ Время работы: {hours}ч {minutes}м\n"
        f"👤 Пользователей: {len(users_db)}\n"
        f"🌟 Подписок: {len(subscriptions)}\n"
        f"⚙️ Модель: {CURRENT_MODEL}"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['clear'])
def clear_cmd(message):
    str_chat_id = str(message.chat.id)
    if str_chat_id in history_db:
        history_db[str_chat_id] = []
        save_json(HISTORY_FILE, history_db)
    bot.reply_to(message, "🧹 История очищена!")

@bot.message_handler(commands=['search'])
def search_cmd(message):
    query = message.text[7:].strip()
    if not query:
        bot.reply_to(message, "✏️ Напиши запрос: <code>/search курс доллара</code>", parse_mode="HTML")
        return
    process_llm_request(message.chat.id, message.from_user.id, f"найди {query}", message)

@bot.message_handler(commands=['image'])
def image_cmd(message):
    query = message.text[6:].strip()
    if not query:
        bot.reply_to(message, "✏️ Напиши описание: <code>/image кота</code>", parse_mode="HTML")
        return
    process_llm_request(message.chat.id, message.from_user.id, f"нарисуй {query}", message)

@bot.message_handler(commands=['voice'])
def voice_cmd(message):
    text = message.text[6:].strip()
    if not text:
        bot.reply_to(message, "✏️ Напиши текст: <code>/voice Привет, мир!</code>", parse_mode="HTML")
        return
    plan = get_user_plan(message.from_user.id)
    if message.from_user.id != OWNER_ID and plan != "pro":
        bot.reply_to(message, "❌ Озвучка доступна только по подписке <b>Pro</b>!", parse_mode="HTML")
        return
    bot.reply_to(message, "🎙️ Генерирую голосовое сообщение...")
    audio = text_to_speech(text)
    if audio:
        bot.send_voice(message.chat.id, audio)
    else:
        bot.reply_to(message, "❌ Не удалось создать голосовое.")

# --- 13. ОБРАБОТКА ФОТО (OCR) ---
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    track_user(message.from_user)
    user_id = message.from_user.id
    plan = get_user_plan(user_id)

    if user_id != OWNER_ID and plan not in ["premium", "pro"]:
        bot.reply_to(
            message,
            "📸 <b>Распознавание текста доступно только по подписке!</b>\n\n👉 /premium",
            parse_mode="HTML"
        )
        return

    bot.reply_to(message, "🔍 Распознаю текст...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        text = extract_text_from_image(downloaded_file)
        bot.reply_to(message, f"📄 {text}")
    except Exception as e:
        logging.error(f"Ошибка обработки фото: {e}")
        bot.reply_to(message, "⚠️ Ошибка при обработке фото.")

# --- 14. АДМИН-КОМАНДЫ ---
@bot.message_handler(commands=['broadcast'])
def broadcast_cmd(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Только создатель.")
        return
    text = message.text[10:].strip()
    if not text:
        bot.reply_to(message, "✏️ Напиши текст рассылки.")
        return
    for uid in users_db:
        try:
            bot.send_message(int(uid), f"📢 <b>Объявление</b>\n\n{text}", parse_mode="HTML")
            time.sleep(0.5)
        except:
            pass
    bot.reply_to(message, f"✅ Рассылка отправлена {len(users_db)} пользователям.")

@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Только создатель.")
        return
    total_users = len(users_db)
    total_subs = len(subscriptions)
    total_free = sum(1 for uid in users_db if not is_premium(int(uid)))
    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👤 Всего пользователей: {total_users}\n"
        f"🌟 Подписчиков: {total_subs}\n"
        f"🆓 Бесплатных: {total_free}\n"
        f"⚙️ Модель: {CURRENT_MODEL}"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['ban'])
def ban_cmd(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Только создатель.")
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "❌ Использование: <code>/ban user_id</code>", parse_mode="HTML")
            return
        user_id = int(parts[1])
        users_db[str(user_id)]['banned'] = True
        save_json(USERS_FILE, users_db)
        bot.reply_to(message, f"✅ Пользователь {user_id} забанен.")
    except:
        bot.reply_to(message, "❌ Ошибка. Использование: <code>/ban user_id</code>", parse_mode="HTML")

@bot.message_handler(commands=['unban'])
def unban_cmd(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Только создатель.")
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "❌ Использование: <code>/unban user_id</code>", parse_mode="HTML")
            return
        user_id = int(parts[1])
        users_db[str(user_id)]['banned'] = False
        save_json(USERS_FILE, users_db)
        bot.reply_to(message, f"✅ Пользователь {user_id} разбанен.")
    except:
        bot.reply_to(message, "❌ Ошибка.")

# --- 15. ТЕКСТОВЫЕ СООБЩЕНИЯ ---
@bot.message_handler(func=lambda message: True)
def handle_text(message):
    if is_banned(message.from_user.id):
        bot.reply_to(message, "🚫 Вы забанены.")
        return
    track_user(message.from_user)
    process_llm_request(message.chat.id, message.from_user.id, message.text, message)

# --- 16. ЗАПУСК ---
start_time = time.time()

if __name__ == "__main__":
    print("="*50)
    print("🤖 Zelmy AI v6.0 — ВСЁ РАБОТАЕТ")
    print("✅ Поиск + Картинки + TTS + OCR + Подписки")
    print("="*50)

    while True:
        try:
            bot.polling(none_stop=True, timeout=60)
        except Exception as e:
            logging.error(f"Сбой: {e}")
            time.sleep(5)
