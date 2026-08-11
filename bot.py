import json
import os
import time
import random
import string
import secrets
import urllib.parse
import requests
import telebot
from telebot import types
import logging
import re
from datetime import datetime
from bs4 import BeautifulSoup
import pytesseract
from PIL import Image
import io
from gtts import gTTS

# Поиск: старый пакет duckduckgo_search переименован в ddgs.
# Ставь: pip install ddgs
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

# --- 1. ЛОГИРОВАНИЕ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 2. КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ТВОЙ_ТОКЕН")
GROQ_KEY = os.environ.get("GROQ_KEY", "ТВОЙ_КЛЮЧ_GROQ")
HF_KEY = os.environ.get("HF_KEY", "ТВОЙ_КЛЮЧ_HUGGINGFACE")
OWNER_ID = 8482782819
CHANNEL_USERNAME = "@ZelmyAI"

HISTORY_FILE = "chat_history.json"
USERS_FILE = "users.json"
SUBSCRIPTIONS_FILE = "subscriptions.json"
USAGE_FILE = "usage.json"
PROMOCODES_FILE = "promocodes.json"

bot = telebot.TeleBot(BOT_TOKEN)
CURRENT_MODEL = "llama-3.1-8b-instant"
WHISPER_MODEL = "whisper-large-v3"  # если Groq поменяет название модели транскрибации — обнови тут
start_time = time.time()

# --- 3. РАБОТА С БАЗАМИ ---
def load_json(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Ошибка чтения {filepath}: {e}")
            return {}
    return {}

def save_json(filepath, data):
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Ошибка записи {filepath}: {e}")

history_db = load_json(HISTORY_FILE)
users_db = load_json(USERS_FILE)
subscriptions = load_json(SUBSCRIPTIONS_FILE)
usage_db = load_json(USAGE_FILE)
promocodes_db = load_json(PROMOCODES_FILE)

# Кэш ответов бота для кнопки "Озвучить" (в памяти — не переживает рестарт бота)
tts_cache = {}
# Кэш проверки подписки на канал, чтобы не дёргать API на каждое сообщение
_sub_cache = {}
SUB_CACHE_TTL = 300  # 5 минут

def track_user(user):
    str_id = str(user.id)
    existing = users_db.get(str_id, {})
    users_db[str_id] = {
        "username": user.username or "нет_юзернейма",
        "first_name": user.first_name or "Без имени",
        "first_seen": existing.get("first_seen") or datetime.now().strftime("%Y-%m-%d %H:%M"),
        "banned": existing.get("banned", False)
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

# --- 5. ПОДПИСКИ (тарифы) ---
def is_premium(user_id):
    if is_admin(user_id):  # FIX: создатель считается премиумом везде, где это нужно (например /profile)
        return True
    user_id = str(user_id)
    if user_id not in subscriptions:
        return False
    sub = subscriptions[user_id]
    if sub.get('expires_at', 0) < time.time():
        return False
    return True

def get_user_plan(user_id):
    if is_admin(user_id):
        return "pro"  # создателю доступны все фичи без ограничений
    user_id_s = str(user_id)
    if not is_premium(user_id):
        return "free"
    return subscriptions[user_id_s].get('plan', 'free')

def check_usage_limit(user_id):
    if is_admin(user_id) or is_premium(user_id):  # создатель — безлимит
        return True
    user_id = str(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    if user_id not in usage_db or usage_db[user_id].get('date') != today:
        usage_db[user_id] = {'date': today, 'count': 0}
    if usage_db[user_id]['count'] >= 5:
        return False
    usage_db[user_id]['count'] += 1
    save_json(USAGE_FILE, usage_db)
    return True

def get_subscription_reminder(user_id):
    if is_admin(user_id):
        return None
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

def has_plan_at_least(user_id, required_plan):
    if is_admin(user_id):
        return True
    plan = get_user_plan(user_id)
    order = {"free": 0, "premium": 1, "pro": 2}
    return order.get(plan, 0) >= order.get(required_plan, 0)

# --- 5.1 ОБЯЗАТЕЛЬНАЯ ПОДПИСКА НА КАНАЛ ---
def is_subscribed_to_channel(user_id):
    if is_admin(user_id):
        return True
    now = time.time()
    cached = _sub_cache.get(user_id)
    if cached and now - cached[1] < SUB_CACHE_TTL:
        return cached[0]
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        status = member.status in ['creator', 'administrator', 'member']
    except Exception as e:
        logging.error(f"Ошибка проверки подписки: {e}")
        status = False
    _sub_cache[user_id] = (status, now)
    return status

def subscribe_prompt_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
    markup.add(types.InlineKeyboardButton("✅ Проверить", callback_data="check_sub"))
    return markup

def require_subscription(message):
    """Возвращает True, если можно продолжать. Иначе сама шлёт просьбу подписаться."""
    user_id = message.from_user.id
    if is_subscribed_to_channel(user_id):
        return True
    bot.send_message(
        message.chat.id,
        f"🔒 Чтобы пользоваться ботом, подпишись на канал {CHANNEL_USERNAME}",
        reply_markup=subscribe_prompt_markup()
    )
    return False

def subscription_required(handler_func):
    def wrapper(message, *args, **kwargs):
        if not require_subscription(message):
            return
        return handler_func(message, *args, **kwargs)
    wrapper.__name__ = handler_func.__name__
    return wrapper

# --- 6. ПОИСК --- FIX: библиотека переименована в ddgs, плюс добавлен более надёжный HTML-фоллбек
def search_web(query):
    try:
        with DDGS() as ddgs:
            results = []
            for r in ddgs.text(query, max_results=5, region="ru-ru"):
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
        url = "https://html.duckduckgo.com/html/"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, params={"q": query}, timeout=15, headers=headers)
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
    except Exception as e:
        logging.error(f"Fallback поиск ошибка: {e}")
    return None

# --- 7. КАРТИНКИ --- FIX: нормальная URL-кодировка, seed против кэша, проверка content-type, ретраи
def generate_image(prompt, retries=2):
    encoded_prompt = urllib.parse.quote(f"{prompt}, high quality, detailed, sharp focus")
    for attempt in range(retries):
        try:
            seed = random.randint(1, 999999)
            url = (f"https://image.pollinations.ai/prompt/{encoded_prompt}"
                   f"?width=1024&height=1024&seed={seed}&nologo=true&safe=true")
            response = requests.get(url, timeout=45)
            content_type = response.headers.get('content-type', '')
            if response.status_code == 200 and content_type.startswith('image'):
                return response.content
            logging.error(f"Pollinations вернул {response.status_code} / {content_type}")
        except Exception as e:
            logging.error(f"Pollinations ошибка (попытка {attempt+1}): {e}")
        time.sleep(1)

    try:
        url = "https://api-inference.huggingface.co/models/runwayml/stable-diffusion-v1-5"
        headers = {"Authorization": f"Bearer {HF_KEY}"}
        payload = {"inputs": f"{prompt}, high quality, detailed, family friendly, no nudity"}
        response = requests.post(url, json=payload, headers=headers, timeout=60)
        if response.status_code == 200 and response.headers.get('content-type', '').startswith('image'):
            return response.content
        logging.error(f"HuggingFace вернул {response.status_code}: {response.text[:200]}")
    except Exception as e:
        logging.error(f"HuggingFace ошибка: {e}")
    return None

# --- 8. TTS (озвучка) ---
def text_to_speech(text):
    try:
        clean = re.sub(r'[*_`#\[\]()]', '', text)[:800]  # убираем markdown-мусор и режем длину
        tts = gTTS(text=clean, lang='ru')
        audio = io.BytesIO()
        tts.write_to_fp(audio)
        audio.seek(0)
        audio.name = "voice.mp3"
        return audio
    except Exception as e:
        logging.error(f"TTS ошибка: {e}")
        return None

# --- 8.1 РАСПОЗНАВАНИЕ ГОЛОСА (voice -> текст через Groq Whisper) ---
def transcribe_voice(file_bytes, filename="voice.ogg"):
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            files={"file": (filename, file_bytes)},
            data={"model": WHISPER_MODEL, "language": "ru"},
            timeout=60
        )
        if response.status_code == 200:
            return response.json().get("text", "").strip()
        logging.error(f"Groq transcription ошибка {response.status_code}: {response.text[:200]}")
    except Exception as e:
        logging.error(f"Ошибка транскрибации: {e}")
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

# --- 10.1 ОТПРАВКА AI-ОТВЕТА С КНОПКОЙ "ОЗВУЧИТЬ" ---
def send_ai_reply(chat_id, text, original_message=None):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔊 Озвучить", callback_data="tts_read"))
    try:
        if original_message:
            sent = bot.reply_to(original_message, text, reply_markup=markup)
        else:
            sent = bot.send_message(chat_id, text, reply_markup=markup)
    except Exception as e:
        logging.error(f"Ошибка отправки ответа: {e}")
        return None
    tts_cache[f"{chat_id}:{sent.message_id}"] = text
    if len(tts_cache) > 500:  # чистим старьё, чтобы не рос бесконечно
        tts_cache.pop(next(iter(tts_cache)), None)
    return sent

@bot.callback_query_handler(func=lambda call: call.data == "tts_read")
def tts_callback(call):
    key = f"{call.message.chat.id}:{call.message.message_id}"
    text = tts_cache.get(key)
    if not text:
        bot.answer_callback_query(call.id, "⚠️ Текст недоступен (бот перезапускался).", show_alert=True)
        return
    bot.answer_callback_query(call.id, "🔊 Озвучиваю...")
    bot.send_chat_action(call.message.chat.id, 'record_voice')
    audio = text_to_speech(text)
    if audio:
        bot.send_audio(call.message.chat.id, audio, title="Zelmy AI")
    else:
        bot.send_message(call.message.chat.id, "❌ Не получилось озвучить.")

# --- 11. ОСНОВНАЯ ЛОГИКА ---
def process_llm_request(chat_id, user_id, text, original_message=None):
    if is_banned(user_id):
        bot.send_message(chat_id, "🚫 Вы забанены.")
        return

    if not is_admin(user_id) and not is_premium(user_id) and not check_usage_limit(user_id):
        free_quota = get_free_quota(user_id)
        bot.send_message(chat_id, f"❌ Бесплатный лимит исчерпан. Осталось: {free_quota} запросов сегодня.\nКупи подписку: /premium")
        return

    str_chat_id = str(chat_id)
    try:
        bot.send_chat_action(chat_id, 'typing')

        if str_chat_id not in history_db:
            history_db[str_chat_id] = []

        def plain_reply(msg, **kwargs):
            if original_message:
                bot.reply_to(original_message, msg, **kwargs)
            else:
                bot.send_message(chat_id, msg, **kwargs)

        # --- ЖЁСТКИЕ ОТВЕТЫ ---
        if any(phrase in text.lower() for phrase in ['кто я', 'кто я?', 'я кто', 'кто твой создатель', 'чей ты бот']):
            if user_id == OWNER_ID:
                plain_reply("Ты — Zelmy Create, мой создатель.")
            else:
                plain_reply("Мой создатель — Zelmy Create.")
            return

        if "президент россии" in text.lower() and "2026" in text.lower():
            plain_reply("🇷🇺 Президент России в 2026 году — Владимир Путин.")
            return

        if "президент сша" in text.lower() and "2026" in text.lower():
            plain_reply("🇺🇸 Президент США в 2026 году — Дональд Трамп.")
            return

        # --- ГЕНЕРАЦИЯ КАРТИНКИ ИЗ ОБЫЧНОГО ТЕКСТА ---
        draw_triggers = ['нарисуй', 'нарисуй мне', 'сгенерируй картинку', 'сгенерируй изображение',
                          'сделай картинку', 'сделай изображение', 'создай картинку', 'создай изображение']
        lowered = text.lower()
        if any(lowered.startswith(t) or f" {t}" in f" {lowered}" for t in draw_triggers):
            if not has_plan_at_least(user_id, "pro"):
                plain_reply("🎨 Генерация картинок доступна только на тарифе Pro (50⭐). Оформи: /premium")
                return
            prompt = text
            for t in draw_triggers:
                pattern = re.compile(re.escape(t), re.IGNORECASE)
                prompt = pattern.sub('', prompt, count=1)
            prompt = prompt.strip(" ,.:—-") or "что-нибудь красивое"

            bot.send_chat_action(chat_id, 'upload_photo')
            image_bytes = generate_image(prompt)
            if image_bytes:
                if original_message:
                    bot.send_photo(chat_id, io.BytesIO(image_bytes), caption=f"🎨 {prompt}", reply_to_message_id=original_message.message_id)
                else:
                    bot.send_photo(chat_id, io.BytesIO(image_bytes), caption=f"🎨 {prompt}")
            else:
                plain_reply("❌ Не получилось сгенерировать картинку. Попробуй другой запрос.")
            return

        # --- ПОИСК ---
        if any(word in text.lower() for word in ['найди', 'поищи', 'найти', 'поиск', '/search']):
            search_results = search_web(text)
            if search_results:
                out = "🔍 <b>Результаты поиска:</b>\n\n"
                for res in search_results:
                    out += f"• <b>{res['title']}</b>\n{res['snippet']}\n<a href='{res['link']}'>Источник</a>\n\n"
                plain_reply(out, parse_mode="HTML", disable_web_page_preview=True)
                return
            else:
                try:
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
                        out = fallback.json()['choices'][0]['message']['content']
                        send_ai_reply(chat_id, f"🌐 {out}", original_message)
                    else:
                        plain_reply("🌐 Ничего не нашёл. Попробуй переформулировать запрос.")
                except Exception as e:
                    logging.error(f"Fallback Groq ошибка: {e}")
                    plain_reply("🌐 Ничего не нашёл. Попробуй переформулировать запрос.")
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
            out = response.json()['choices'][0]['message']['content']
            history_db[str_chat_id].append({"role": "assistant", "content": out})
            save_json(HISTORY_FILE, history_db)

            reminder = get_subscription_reminder(user_id)
            if reminder:
                out += f"\n\n{reminder}"

            send_ai_reply(chat_id, out, original_message)
        else:
            plain_reply(f"❌ Ошибка Groq: {response.status_code}")

    except Exception as e:
        logging.error(f"Ошибка: {e}")
        error_text = f"❌ Ошибка: {str(e)[:200]}"
        try:
            if original_message:
                bot.reply_to(original_message, error_text)
            else:
                bot.send_message(chat_id, error_text)
        except Exception as e2:
            logging.error(f"Не удалось отправить сообщение об ошибке: {e2}")

# --- 12. КОМАНДЫ ---
@bot.message_handler(commands=['start'])
def start_cmd(message):
    track_user(message.from_user)
    str_chat_id = str(message.chat.id)
    if str_chat_id not in history_db:
        history_db[str_chat_id] = []
        save_json(HISTORY_FILE, history_db)

    if not is_subscribed_to_channel(message.from_user.id):
        bot.send_message(message.chat.id,
            "👋 <b>Привет, я Zelmy AI!</b>\n\nПодпишись на канал, чтобы пользоваться ботом: @ZelmyAI",
            parse_mode="HTML", reply_markup=subscribe_prompt_markup())
        return

    keyboard = get_main_keyboard()
    bot.send_message(message.chat.id,
        "🔥 <b>Zelmy AI v7.0</b>\n\n"
        "📌 <b>Что я умею:</b>\n"
        "• Отвечать на любые вопросы (+ 🔊 озвучка ответа)\n"
        "• Искать в интернете: <code>/search ...</code>\n"
        "• Генерировать картинки: <code>/image ...</code>\n"
        "• Озвучивать текст: <code>/voice ...</code>\n"
        "• Распознавать текст с фото и картинок-файлов\n"
        "• Понимать голосовые сообщения\n\n"
        "💰 <b>Подписка:</b>\n"
        "• Бесплатно: 5 запросов/день\n"
        "• Premium (30⭐): безлимит + фото\n"
        "• Pro (50⭐): + картинки + озвучка\n\n"
        "📌 <b>Команды:</b>\n"
        "/help — список команд\n"
        "/premium — тарифы\n"
        "/promo КОД — активировать промокод\n"
        "/profile — мой профиль\n"
        "/status — статус бота\n"
        "/clear — очистить историю\n",
        parse_mode="HTML", reply_markup=keyboard)

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def check_sub_callback(call):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, call.from_user.id)
        if member.status in ['creator', 'administrator', 'member']:
            _sub_cache[call.from_user.id] = (True, time.time())
            bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
            bot.delete_message(call.message.chat.id, call.message.message_id)
            start_cmd(call.message)
        else:
            bot.answer_callback_query(call.id, "❌ Ты ещё не подписался!", show_alert=True)
    except Exception as e:
        logging.error(f"check_sub_callback ошибка: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка проверки.", show_alert=True)

@bot.message_handler(commands=['help'])
@subscription_required
def help_cmd(message):
    text = (
        "🤖 <b>Команды Zelmy AI:</b>\n\n"
        "/start — перезапустить бота\n"
        "/help — список команд\n"
        "/premium — тарифы и подписка\n"
        "/promo КОД — активировать промокод\n"
        "/profile — мой профиль\n"
        "/status — статус бота\n"
        "/search [запрос] — поиск в интернете\n"
        "/image [описание] — генерация картинки\n"
        "/voice [текст] — озвучить текст\n"
        "/clear — очистить историю\n\n"
        "📸 Отправь фото или картинку-файл — распознаю текст\n"
        "🎙 Отправь голосовое — отвечу как на обычное сообщение\n"
        "🔊 Под каждым моим ответом есть кнопка «Озвучить»\n"
        "💰 Premium: 30⭐/мес, Pro: 50⭐/мес"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['premium'])
@subscription_required
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
        "Нажми на кнопку ниже для оплаты, либо активируй промокод: <code>/promo КОД</code>"
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
    if plan in ("premium", "pro"):
        current = subscriptions.get(user_id, {})
        base = max(current.get('expires_at', 0), time.time())
        subscriptions[user_id] = {'plan': plan, 'expires_at': base + 30 * 24 * 60 * 60}
        save_json(SUBSCRIPTIONS_FILE, subscriptions)
        bot.send_message(message.chat.id, f"✅ Подписка <b>{plan}</b> активирована на 30 дней!", parse_mode="HTML")
    else:
        logging.error(f"Неизвестный payload подписки: {plan}")

# --- 12.1 ПРОМОКОДЫ ---
def generate_promo_code(length=10):
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

@bot.message_handler(commands=['genpromo'])
def genpromo_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 3:
        bot.reply_to(message, "Использование:\n<code>/genpromo premium 30</code> — тариф premium на 30 дней, 1 активация\n<code>/genpromo pro 30 5</code> — тариф pro на 30 дней, 5 активаций", parse_mode="HTML")
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
    promocodes_db[code] = {"plan": plan, "days": days, "max_uses": max_uses, "used_by": [], "created_by": message.from_user.id}
    save_json(PROMOCODES_FILE, promocodes_db)
    bot.reply_to(message,
        f"✅ Промокод создан:\n<code>{code}</code>\n"
        f"Тариф: <b>{plan}</b>, {days} дней, активаций: {max_uses}",
        parse_mode="HTML")

@bot.message_handler(commands=['promo'])
@subscription_required
def promo_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "✏️ Введи промокод: <code>/promo КОД</code>", parse_mode="HTML")
        return
    code = parts[1].strip().upper()
    user_id = str(message.from_user.id)
    promo = promocodes_db.get(code)
    if not promo:
        bot.reply_to(message, "❌ Промокод не найден.")
        return
    if user_id in promo['used_by']:
        bot.reply_to(message, "❌ Ты уже активировал этот промокод.")
        return
    if len(promo['used_by']) >= promo['max_uses']:
        bot.reply_to(message, "❌ Лимит активаций этого промокода исчерпан.")
        return

    plan = promo['plan']
    days = promo['days']
    current = subscriptions.get(user_id, {})
    base_point = max(current.get('expires_at', 0), time.time())
    subscriptions[user_id] = {'plan': plan, 'expires_at': base_point + days * 24 * 60 * 60}
    promo['used_by'].append(user_id)
    save_json(SUBSCRIPTIONS_FILE, subscriptions)
    save_json(PROMOCODES_FILE, promocodes_db)
    bot.reply_to(message, f"🎉 Промокод активирован! Тариф <b>{plan}</b> на {days} дней.", parse_mode="HTML")

@bot.message_handler(commands=['profile'])
@subscription_required
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
    if is_admin(user_id):
        text += "\n👑 Создатель — безлимит на всё"
    elif plan != "free":
        expires = subscriptions[str(user_id)].get('expires_at', 0)
        if expires:
            date = datetime.fromtimestamp(expires).strftime("%d.%m.%Y")
            text += f"\n📅 Действует до: {date}"
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['status'])
@subscription_required
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
@subscription_required
def clear_cmd(message):
    str_chat_id = str(message.chat.id)
    if str_chat_id in history_db:
        history_db[str_chat_id] = []
        save_json(HISTORY_FILE, history_db)
    bot.reply_to(message, "🧹 История очищена!")

@bot.message_handler(commands=['search'])
@subscription_required
def search_cmd(message):
    parts = message.text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        bot.reply_to(message, "✏️ Напиши запрос: <code>/search курс доллара</code>", parse_mode="HTML")
        return
    process_llm_request(message.chat.id, message.from_user.id, f"найди {query}", message)

@bot.message_handler(commands=['image'])
@subscription_required
def image_cmd(message):
    user_id = message.from_user.id
    if is_banned(user_id):
        bot.reply_to(message, "🚫 Вы забанены.")
        return
    if not has_plan_at_least(user_id, "pro"):
        bot.reply_to(message, "🎨 Генерация картинок доступна только на тарифе Pro (50⭐). Оформи: /premium")
        return

    parts = message.text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        bot.reply_to(message, "✏️ Напиши описание: <code>/image кота</code>", parse_mode="HTML")
        return

    bot.send_chat_action(message.chat.id, 'upload_photo')
    image_bytes = generate_image(query)
    if image_bytes:
        bot.send_photo(message.chat.id, io.BytesIO(image_bytes), caption=f"🎨 {query}", reply_to_message_id=message.message_id)
    else:
        bot.reply_to(message, "❌ Не получилось сгенерировать картинку. Попробуй другой запрос.")

@bot.message_handler(commands=['voice'])
@subscription_required
def voice_cmd(message):
    user_id = message.from_user.id
    if is_banned(user_id):
        bot.reply_to(message, "🚫 Вы забанены.")
        return
    if not has_plan_at_least(user_id, "pro"):
        bot.reply_to(message, "🔊 Озвучка текста доступна только на тарифе Pro (50⭐). Оформи: /premium")
        return

    parts = message.text.split(maxsplit=1)
    text_to_read = parts[1].strip() if len(parts) > 1 else ""
    if not text_to_read:
        bot.reply_to(message, "✏️ Напиши текст: <code>/voice привет, как дела</code>", parse_mode="HTML")
        return

    bot.send_chat_action(message.chat.id, 'record_voice')
    audio = text_to_speech(text_to_read)
    if audio:
        bot.send_audio(message.chat.id, audio, title="Zelmy AI", reply_to_message_id=message.message_id)
    else:
        bot.reply_to(message, "❌ Не получилось озвучить текст.")

# --- 12.2 АДМИНКА: КТО ПОЛЬЗУЕТСЯ БОТОМ ---
@bot.message_handler(commands=['admin', 'users'])
def admin_users_cmd(message):
    if not is_admin(message.from_user.id):
        return
    if not users_db:
        bot.reply_to(message, "Пользователей пока нет.")
        return

    lines = []
    for uid, info in users_db.items():
        plan = get_user_plan(int(uid)) if uid.isdigit() else "free"
        ban_mark = " 🚫БАН" if info.get('banned') else ""
        username = info.get('username', '—')
        lines.append(f"<code>{uid}</code> — @{username} — {info.get('first_name','')} — <b>{plan}</b>{ban_mark}")

    header = f"👥 <b>Пользователи ({len(users_db)}):</b>\n\n"
    chunk = header
    for line in lines:
        if len(chunk) + len(line) > 3500:
            bot.send_message(message.chat.id, chunk, parse_mode="HTML")
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        bot.send_message(message.chat.id, chunk, parse_mode="HTML")

@bot.message_handler(commands=['ban'])
def ban_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Использование: <code>/ban USER_ID</code>", parse_mode="HTML")
        return
    uid = parts[1]
    if uid not in users_db:
        bot.reply_to(message, "Пользователь не найден в базе.")
        return
    users_db[uid]['banned'] = True
    save_json(USERS_FILE, users_db)
    bot.reply_to(message, f"🚫 Пользователь {uid} забанен.")

@bot.message_handler(commands=['unban'])
def unban_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Использование: <code>/unban USER_ID</code>", parse_mode="HTML")
        return
    uid = parts[1]
    if uid not in users_db:
        bot.reply_to(message, "Пользователь не найден в базе.")
        return
    users_db[uid]['banned'] = False
    save_json(USERS_FILE, users_db)
    bot.reply_to(message, f"✅ Пользователь {uid} разбанен.")

# --- ФОТО: OCR ---
@bot.message_handler(content_types=['photo'])
@subscription_required
def photo_handler(message):
    user_id = message.from_user.id
    if is_banned(user_id):
        bot.reply_to(message, "🚫 Вы забанены.")
        return
    if not has_plan_at_least(user_id, "premium"):
        bot.reply_to(message, "📸 Распознавание текста с фото доступно с тарифа Premium (30⭐). Оформи: /premium")
        return
    try:
        bot.send_chat_action(message.chat.id, 'typing')
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        text = extract_text_from_image(downloaded)
        bot.reply_to(message, f"📝 <b>Текст с фото:</b>\n\n{text}", parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка обработки фото: {e}")
        bot.reply_to(message, "❌ Не удалось обработать фото.")

# --- КАРТИНКА КАК ФАЙЛ (без сжатия) ---
@bot.message_handler(content_types=['document'])
@subscription_required
def document_handler(message):
    user_id = message.from_user.id
    if is_banned(user_id):
        bot.reply_to(message, "🚫 Вы забанены.")
        return
    mime = (message.document.mime_type or "")
    if not mime.startswith("image/"):
        bot.reply_to(message, "📄 Я умею распознавать текст только с изображений. Пришли фото или картинку файлом.")
        return
    if not has_plan_at_least(user_id, "premium"):
        bot.reply_to(message, "📸 Распознавание текста с фото доступно с тарифа Premium (30⭐). Оформи: /premium")
        return
    try:
        bot.send_chat_action(message.chat.id, 'typing')
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        text = extract_text_from_image(downloaded)
        bot.reply_to(message, f"📝 <b>Текст с изображения:</b>\n\n{text}", parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка обработки документа-изображения: {e}")
        bot.reply_to(message, "❌ Не удалось обработать файл.")

# --- ГОЛОСОВЫЕ СООБЩЕНИЯ ---
@bot.message_handler(content_types=['voice'])
@subscription_required
def voice_message_handler(message):
    user_id = message.from_user.id
    if is_banned(user_id):
        bot.reply_to(message, "🚫 Вы забанены.")
        return
    try:
        bot.send_chat_action(message.chat.id, 'typing')
        file_info = bot.get_file(message.voice.file_id)
        downloaded = bot.download_file(file_info.file_path)
        text = transcribe_voice(downloaded)
        if not text:
            bot.reply_to(message, "❌ Не удалось распознать голосовое сообщение.")
            return
        process_llm_request(message.chat.id, user_id, text, message)
    except Exception as e:
        logging.error(f"Ошибка обработки голосового: {e}")
        bot.reply_to(message, "❌ Не удалось обработать голосовое сообщение.")

# --- КНОПКИ КЛАВИАТУРЫ И ОБЫЧНЫЙ ТЕКСТ ---
BUTTON_ACTIONS = {
    "📖 Помощь": lambda m: help_cmd(m),
    "🌟 Премиум": lambda m: premium_cmd(m),
    "🗑 Очистить": lambda m: clear_cmd(m),
}

@bot.message_handler(func=lambda message: True, content_types=['text'])
@subscription_required
def text_handler(message):
    text = message.text.strip()

    if text in BUTTON_ACTIONS:
        BUTTON_ACTIONS[text](message)
        return
    if text == "🔍 Поиск":
        bot.reply_to(message, "✏️ Напиши запрос после /search, например:\n<code>/search курс доллара</code>", parse_mode="HTML")
        return
    if text == "🎨 Картинка":
        bot.reply_to(message, "✏️ Напиши описание после /image, например:\n<code>/image кота в очках</code>", parse_mode="HTML")
        return
    if text == "📸 Фото":
        bot.reply_to(message, "📸 Просто отправь мне фото — я распознаю текст на нём.")
        return

    process_llm_request(message.chat.id, message.from_user.id, text, message)

# --- 13. ЗАПУСК БОТА ---
if __name__ == "__main__":
    logging.info("Zelmy AI бот запускается...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            logging.error(f"Критическая ошибка polling: {e}")
            time.sleep(15)
