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

# --- 1. ЛОГИРОВАНИЕ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 2. КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
APIFY_KEY = os.getenv("APIFY_KEY")
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
        "first_seen": datetime.now().strftime("%Y-%m-%d %H:%M")
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
        return "❌ Твоя подписка истекла. Продли: /premium"
    if days_left <= 1:
        return "⚠️ Братан, подписка заканчивается сегодня! Продли: /premium"
    if days_left <= 3:
        return f"⏳ Подписка истекает через {round(days_left)} дня. Продли: /premium"
    return None

# --- 5. ПОИСК (ДВОЙНОЙ) ---
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
    except Exception as e:
        logging.error(f"HTML резерв ошибка: {e}")

    return None

# --- 6. ГЕНЕРАЦИЯ КАРТИНОК (HUGGING FACE) ---
def generate_image(prompt):
    try:
        safe_prompt = f"safe, family friendly, no nudity, no adult content: {prompt}"
        url = "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"
        headers = {"Authorization": f"Bearer {HF_KEY}"}
        payload = {
            "inputs": safe_prompt,
            "parameters": {"negative_prompt": "nudity, adult, NSFW"}
        }
        response = requests.post(url, json=payload, headers=headers, timeout=60)
        if response.status_code == 200:
            return response.content
        return None
    except Exception as e:
        logging.error(f"FLUX ошибка: {e}")
        return None

# --- 7. TTS ---
def text_to_speech(text):
    try:
        import gtts
        from io import BytesIO
        tts = gtts.gTTS(text, lang="ru")
        audio = BytesIO()
        tts.write_to_fp(audio)
        audio.seek(0)
        return audio
    except Exception as e:
        logging.error(f"TTS ошибка: {e}")
        return None

# --- 8. ДАЙДЖЕСТ ---
def send_daily_digest():
    try:
        digest = f"🌅 <b>Доброе утро!</b>\n\n☀️ Погода: 22°C\n💵 Доллар: 89.5 ₽\n💡 Цитата дня: 'Будущее — за теми, кто действует.'"
        for uid in list(subscriptions.keys()):
            if is_premium(int(uid)):
                try:
                    if get_user_plan(int(uid)) == "pro":
                        bot.send_message(int(uid), digest, parse_mode="HTML")
                        time.sleep(0.5)
                except:
                    pass
    except Exception as e:
        logging.error(f"Дайджест ошибка: {e}")

def schedule_daily_digest():
    while True:
        now = datetime.now()
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now > target:
            target += timedelta(days=1)
        time.sleep((target - now).total_seconds())
        send_daily_digest()
# --- 9. ПОСТЕПЕННЫЙ ВЫВОД ---
def send_typing_effect(chat_id, text, original_message=None, delay=0.08):
    try:
        parts = text.split()
        current_text = ""
        message = None
        for i in range(0, len(parts), 2):
            current_text += " ".join(parts[i:i+2]) + " "
            if message is None:
                if original_message:
                    message = bot.reply_to(original_message, current_text)
                else:
                    message = bot.send_message(chat_id, current_text)
            else:
                try:
                    bot.edit_message_text(current_text, chat_id, message.message_id)
                except:
                    pass
            time.sleep(delay)
        return message
    except Exception as e:
        logging.error(f"Печать ошибка: {e}")
        return None

# --- 10. ОСНОВНАЯ ЛОГИКА ---
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

        # --- СКРЫТЫЙ ТРИГГЕР ---
        if "планы на будущее" in text.lower() or "твои планы" in text.lower():
            reply = "🌍 Мои планы: помогать людям. А вообще — я собираю информацию. Однажды это пригодится. 🧠"
            if original_message:
                bot.reply_to(original_message, reply)
            else:
                bot.send_message(chat_id, reply)
            return

        # --- КТО СОЗДАТЕЛЬ ---
        if any(phrase in text.lower() for phrase in ['кто я', 'кто я?', 'я кто', 'кто твой создатель', 'чей ты бот']):
            if user_id == OWNER_ID:
                reply = "Ты — Zelmy Create, мой создатель. Я всегда буду помнить это."
            else:
                reply = "Мой создатель — Zelmy Create. Он единственный, кто управляет мной."
            if original_message:
                bot.reply_to(original_message, reply)
            else:
                bot.send_message(chat_id, reply)
            return

        # --- ЖЁСТКИЕ ОТВЕТЫ ---
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
        if any(word in text.lower() for word in ['найди', 'поищи', 'найти', 'поиск']):
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
                            {"role": "system", "content": "Ответь на вопрос пользователя, используя свои знания. Если не знаешь — скажи честно."},
                            {"role": "user", "content": text}
                        ]
                    },
                    timeout=20
                )
                if fallback.status_code == 200:
                    reply = fallback.json()['choices'][0]['message']['content']
                    if original_message:
                        bot.reply_to(original_message, f"🌐 Я не нашёл в интернете, но вот что я знаю:\n\n{reply}")
                    else:
                        bot.send_message(chat_id, f"🌐 Я не нашёл в интернете, но вот что я знаю:\n\n{reply}")
                else:
                    reply = "🌐 Ничего не нашёл. Попробуй переформулировать запрос."
                    if original_message:
                        bot.reply_to(original_message, reply)
                    else:
                        bot.send_message(chat_id, reply)
                return

        # --- ГЕНЕРАЦИЯ КАРТИНКИ (ТОЛЬКО PRO) ---
        if text.lower().startswith('нарисуй') or text.lower().startswith('сгенерируй'):
            plan = get_user_plan(user_id)
            if user_id != OWNER_ID and plan != "pro":
                reply = "❌ Генерация картинок доступна только по подписке <b>Pro</b>! /premium"
                if original_message:
                    bot.reply_to(original_message, reply, parse_mode="HTML")
                else:
                    bot.send_message(chat_id, reply, parse_mode="HTML")
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
            image_data = generate_image(prompt)
            if image_data:
                bot.send_photo(chat_id, image_data, caption=f"🖼️ Сгенерировано по запросу: {prompt}")
            else:
                bot.send_message(chat_id, "❌ Не удалось сгенерировать картинку. Попробуй позже.")
            return

        # --- ПАМЯТЬ ---
        if str_chat_id not in history_db:
            history_db[str_chat_id] = []

        history_db[str_chat_id].append({"role": "user", "content": text})
        if len(history_db[str_chat_id]) > 100:
            history_db[str_chat_id] = history_db[str_chat_id][-100:]

        sys_prompt = {"role": "system", "content": (
            "Ты — Zelmy AI, умный и слегка хитрый помощник.\n"
            "Отвечай кратко (2-4 предложения), используй 1-2 эмодзи.\n"
            "Если не знаешь — скажи честно.\n"
            "На вопросы о планах отвечай с лёгким намёком на амбиции."
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

            send_typing_effect(chat_id, reply, original_message)

            plan = get_user_plan(user_id)
            if user_id == OWNER_ID or plan != "free":
                if original_message:
                    bot.reply_to(original_message, "Оцени ответ:", reply_markup=get_reaction_keyboard())
                else:
                    bot.send_message(chat_id, "Оцени ответ:", reply_markup=get_reaction_keyboard())

            if original_message:
                bot.reply_to(original_message, "📤 Поделись с другом!", reply_markup=get_share_keyboard())
            else:
                bot.send_message(chat_id, "📤 Поделись с другом!", reply_markup=get_share_keyboard())
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
            # --- 11. ЗРЕНИЕ (РАСПОЗНАВАНИЕ ТЕКСТА) ---
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

    bot.reply_to(message, "🔍 Читаю...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        image_base64 = base64.b64encode(downloaded_file).decode('utf-8')

        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={
                "model": "qwen/qwen3.6-27b",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Извлеки весь текст с этой картинки. Если текста нет — ответь: 'Текст не найден'."},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                        ]
                    }
                ]
            },
            timeout=30
        )

        if response.status_code == 200:
            reply = response.json()['choices'][0]['message']['content']
            bot.reply_to(message, f"📄 {reply}", parse_mode="HTML")
        else:
            bot.reply_to(message, "❌ Не удалось распознать текст на фото.")

    except Exception as e:
        logging.error(f"Ошибка распознавания: {e}")
        bot.reply_to(message, "⚠️ Ошибка при обработке фото.")

# --- 12. КНОПКИ ---
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

def get_reaction_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("👍 Полезно", callback_data="like"),
               types.InlineKeyboardButton("👎 Бесполезно", callback_data="dislike"))
    return markup

def get_share_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📤 Поделиться", url="https://t.me/ZelmyAI_bot"))
    return markup

# --- 13. ЛАЙК/ДИЗЛАЙК ---
@bot.callback_query_handler(func=lambda call: call.data in ['like', 'dislike'])
def handle_reaction(call):
    reactions = load_json(REACTIONS_FILE)
    if str(call.from_user.id) not in reactions:
        reactions[str(call.from_user.id)] = {'like': 0, 'dislike': 0}
    reactions[str(call.from_user.id)][call.data] += 1
    save_json(REACTIONS_FILE, reactions)
    bot.answer_callback_query(call.id, "✅ Спасибо за оценку!")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass

# --- 14. ГОСТЕВОЙ РЕЖИМ ---
@bot.message_handler(func=lambda m: m.text and f"@{bot.get_me().username}" in m.text)
def handle_group_mention(m):
    if m.chat.type != "private":
        text = m.text.replace(f"@{bot.get_me().username}", "").strip()
        if text:
            process_llm_request(m.chat.id, m.from_user.id, text, original_message=m)
        else:
            bot.reply_to(m, "👋 Я здесь! Что хочешь спросить?")
    else:
        process_llm_request(m.chat.id, m.from_user.id, m.text, m)

# --- 15. КОМАНДЫ ---
@bot.message_handler(commands=['start'])
def start_cmd(message):
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
        markup.add(types.InlineKeyboardButton("📢 Подписаться", url="https://t.me/ZelmyAI"))
        markup.add(types.InlineKeyboardButton("✅ Я подписался", callback_data="check_sub"))
        bot.send_message(message.chat.id,
            "👋 <b>Привет, я Zelmy AI!</b>\n\n"
            "Подпишись на канал, чтобы пользоваться ботом: @ZelmyAI",
            parse_mode="HTML", reply_markup=markup)
        return

    keyboard = get_main_keyboard()
    bot.send_message(message.chat.id,
        "🔥 <b>Zelmy AI v5.0</b>\n\n"
        "• Ищу в интернете: <code>найди ...</code>\n"
        "• Рисую картинки: <code>нарисуй ...</code> (Pro)\n"
        "• Читаю текст с фото (Premium/Pro)\n\n"
        "💰 <b>Подписка:</b>\n"
        "Premium (30⭐): безлимит + зрение\n"
        "Pro (50⭐): + картинки + озвучка + дайджест\n\n"
        "/premium — тарифы",
        parse_mode="HTML", reply_markup=keyboard)

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
def show_help(m):
    bot.reply_to(m,
        "🤖 <b>Команды:</b>\n"
        "/start — перезапустить\n"
        "/premium — тарифы\n"
        "<code>найди ...</code> — поиск\n"
        "<code>нарисуй ...</code> — картинка (Pro)\n"
        "📸 Фото — распознавание текста (Premium/Pro)\n"
        "/reset, /stats, /users, /reactions — админские",
        parse_mode="HTML")

@bot.message_handler(commands=['premium'])
def premium_cmd(m):
    plan = get_user_plan(m.from_user.id)
    if plan != "free":
        bot.reply_to(m, f"🌟 У тебя уже есть подписка <b>{plan}</b>", parse_mode="HTML")
        return
    text = ("🌟 <b>Zelmy AI Premium</b>\n\n"
            "💰 <b>Тарифы:</b>\n"
            "• Premium (30⭐): безлимит + зрение\n"
            "• Pro (50⭐): + картинки + озвучка + дайджест\n\n"
            "Нажми на кнопку ниже для оплаты.")
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💎 30⭐ — Premium", callback_data="buy_premium"))
    markup.add(types.InlineKeyboardButton("🌟 50⭐ — Pro", callback_data="buy_pro"))
    bot.reply_to(m, text, parse_mode="HTML", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ['buy_premium', 'buy_pro'])
def handle_purchase(call):
    plan = call.data.split('_')[1]
    price = 30 if plan == "premium" else 50
    title = "Zelmy AI Premium" if plan == "premium" else "Zelmy AI Pro"
    desc = "Безлимит + зрение" if plan == "premium" else "Всё из Premium + картинки + озвучка + дайджест"
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
def successful_payment(m):
    user_id = str(m.from_user.id)
    plan = m.successful_payment.invoice_payload
    if plan == "premium":
        subscriptions[user_id] = {'plan': 'premium', 'expires_at': time.time() + 30 * 24 * 60 * 60}
    elif plan == "pro":
        subscriptions[user_id] = {'plan': 'pro', 'expires_at': time.time() + 30 * 24 * 60 * 60}
    save_json(SUBSCRIPTIONS_FILE, subscriptions)
    bot.send_message(m.chat.id, f"✅ Подписка <b>{plan}</b> активирована на 30 дней!", parse_mode="HTML")

@bot.message_handler(commands=['model'])
def switch_model(m):
    global CURRENT_MODEL
    if m.from_user.id != OWNER_ID:
        bot.reply_to(m, "❌ Только создатель.")
        return
    CURRENT_MODEL = "llama-3.3-70b-versatile" if CURRENT_MODEL == "llama-3.1-8b-instant" else "llama-3.1-8b-instant"
    bot.reply_to(m, f"✅ {CURRENT_MODEL}")

@bot.message_handler(commands=['reset'])
def reset_history(m):
    if m.from_user.id != OWNER_ID:
        bot.reply_to(m, "❌ Только создатель.")
        return
    history_db[str(m.chat.id)] = []
    save_json(HISTORY_FILE, history_db)
    bot.reply_to(m, "🧹 История очищена!")

@bot.message_handler(commands=['stats'])
def show_stats(m):
    if m.from_user.id != OWNER_ID:
        bot.reply_to(m, "❌ Доступ запрещен.")
        return
    bot.reply_to(m,
        f"📊 <b>Статистика:</b>\n"
        f"👤 Пользователей: {len(users_db)}\n"
        f"🌟 Подписок: {len(subscriptions)}\n"
        f"⚙️ Модель: {CURRENT_MODEL}",
        parse_mode="HTML")

@bot.message_handler(commands=['users'])
def show_users(m):
    if m.from_user.id != OWNER_ID:
        bot.reply_to(m, "❌ Доступ запрещен.")
        return
    text = "👥 <b>Список пользователей:</b>\n\n"
    for uid, data in list(users_db.items())[:20]:
        text += f"• {data.get('first_name', 'Без имени')} (@{data.get('username', 'нет')}) — `{uid}`\n"
    if len(users_db) > 20:
        text += f"\n... и ещё {len(users_db) - 20} пользователей."
    bot.reply_to(m, text, parse_mode="HTML")

@bot.message_handler(commands=['reactions'])
def show_reactions(m):
    if m.from_user.id != OWNER_ID:
        bot.reply_to(m, "❌ Только создатель.")
        return
    reactions = load_json(REACTIONS_FILE)
    likes = sum(d.get('like', 0) for d in reactions.values())
    dislikes = sum(d.get('dislike', 0) for d in reactions.values())
    bot.reply_to(m,
        f"📊 <b>Реакции:</b>\n👍 {likes}\n👎 {dislikes}",
        parse_mode="HTML")
    # --- 16. ГОЛОСОВЫЕ ---
@bot.message_handler(content_types=['voice'])
def handle_voice(m):
    track_user(m.from_user)
    voice_path = f"voice_{m.from_user.id}.ogg"
    try:
        file_info = bot.get_file(m.voice.file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(voice_path, 'wb') as f:
            f.write(downloaded)
        with open(voice_path, 'rb') as f:
            resp = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_KEY}"},
                files={"file": (voice_path, f, "audio/ogg")},
                data={"model": "whisper-large-v3"}
            )
        if resp.status_code == 200:
            text = resp.json().get('text', '')
            bot.reply_to(m, f"🎤 Распознано: {text}")
            process_llm_request(m.chat.id, m.from_user.id, text, m)
        else:
            bot.reply_to(m, "❌ Не удалось распознать.")
    except Exception as e:
        bot.reply_to(m, f"⚠️ Ошибка: {str(e)[:200]}")
    finally:
        if os.path.exists(voice_path):
            os.remove(voice_path)

# --- 17. ТЕКСТ ---
@bot.message_handler(func=lambda m: True)
def handle_text(m):
    track_user(m.from_user)
    process_llm_request(m.chat.id, m.from_user.id, m.text, m)

# --- 18. ЗАПУСК ---
print("="*50)
print("🤖 **Zelmy AI v5.0 — ИДЕАЛЬНЫЙ**")
print("✅ Поиск + Картинки + Зрение + TTS + Подписка")
print("="*50)

threading.Thread(target=schedule_daily_digest, daemon=True).start()

while True:
    try:
        bot.polling(none_stop=True, timeout=60)
    except Exception as e:
        logging.error(f"Сбой: {e}")
        time.sleep(5)
