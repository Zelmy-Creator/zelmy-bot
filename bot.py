import json
import os
import time
import requests
import telebot
from telebot import types
import logging
import re
from datetime import datetime, timedelta

# --- 1. ЛОГИРОВАНИЕ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 2. КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
APIFY_KEY = os.getenv("APIFY_KEY")  # твой ключ от Apify
OWNER_ID = 8482782819  # ТВОЙ ID

HISTORY_FILE = "chat_history.json"
USERS_FILE = "users.json"
MEMORY_FILE = "memory.json"
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
memory_db = load_json(MEMORY_FILE)
subscriptions = load_json(SUBSCRIPTIONS_FILE)
usage_db = load_json(USAGE_FILE)

def track_user(user):
    str_id = str(user.id)
    users_db[str_id] = {
        "username": user.username or "нет_юзернейма",
        "first_name": user.first_name or "Без имени"
    }
    save_json(USERS_FILE, users_db)

def get_memory():
    return memory_db

def save_memory(key, value):
    memory_db[key.lower()] = value
    save_json(MEMORY_FILE, memory_db)
    logging.info(f"🧠 Запомнил: {key} → {value}")

def search_memory(query):
    results = []
    query_lower = query.lower()
    for key, value in memory_db.items():
        if query_lower in key:
            results.append((key, value))
    return results[:3]

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
    if is_premium(user_id):
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

# --- 5. ПОИСК ЧЕРЕЗ APIFY BRAVE ---
def search_apify_brave(query):
    try:
        url = "https://api.apify.com/v2/acts/miroslav~brave-search/runs"
        params = {"token": APIFY_KEY}
        payload = {"query": query, "count": 5}
        response = requests.post(url, json=payload, params=params, timeout=15)
        
        if response.status_code != 200:
            logging.error(f"Apify вернул {response.status_code}")
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

# --- 6. ГЕНЕРАЦИЯ КАРТИНОК ---
def generate_image(prompt):
    try:
        url = f"https://image.pollinations.ai/prompt/{prompt.replace(' ', '%20')}"
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            return response.content
        return None
    except Exception as e:
        logging.error(f"Генерация картинки ошибка: {e}")
        return None

# --- 7. ОСНОВНАЯ ЛОГИКА ---
def process_llm_request(chat_id, user_id, text, original_message=None):
    str_chat_id = str(chat_id)
    
    # Проверка лимита для обычных пользователей
    if not check_usage_limit(user_id) and not is_premium(user_id):
        reply = "❌ Бесплатный лимит (5 запросов/день) исчерпан. Купи подписку: /premium"
        if original_message:
            bot.reply_to(original_message, reply)
        else:
            bot.send_message(chat_id, reply)
        return
    
    try:
        bot.send_chat_action(chat_id, 'typing')
        
        # --- ОТВЕТЫ ПРО СОЗДАТЕЛЯ ---
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

        # --- ПОИСК ---
        if any(word in text.lower() for word in ['найди', 'поищи', 'найти', 'поиск']):
            search_results = search_apify_brave(text)
            if search_results:
                reply = "🔍 **Результаты поиска:**\n\n"
                for res in search_results:
                    reply += f"• **{res['title']}**\n{res['snippet']}\n[Источник]({res['link']})\n\n"
                if original_message:
                    bot.reply_to(original_message, reply)
                else:
                    bot.send_message(chat_id, reply)
                return
            else:
                reply = "🌐 Ничего не нашёл. Попробуй переформулировать запрос."
                if original_message:
                    bot.reply_to(original_message, reply)
                else:
                    bot.send_message(chat_id, reply)
                return

        # --- ГЕНЕРАЦИЯ КАРТИНКИ ---
        if text.lower().startswith('нарисуй') or text.lower().startswith('сгенерируй'):
            # Проверка: если это не владелец И нет подписки — блокируем
            if user_id != OWNER_ID and not is_premium(user_id):
                reply = "❌ Генерация картинок доступна только по подписке! /premium"
                if original_message:
                    bot.reply_to(original_message, reply)
                else:
                    bot.send_message(chat_id, reply)
                return

            prompt = text[8:].strip()
            if not prompt:
                reply = "❌ Напиши, что нарисовать: `нарисуй кота`"
                if original_message:
                    bot.reply_to(original_message, reply)
                else:
                    bot.send_message(chat_id, reply)
                return

            bot.send_message(chat_id, "🎨 Генерирую картинку... Подожди 5-10 секунд.")
            image_data = generate_image(prompt)
            if image_data:
                bot.send_photo(chat_id, image_data, caption=f"🖼️ Сгенерировано по запросу: {prompt}")
            else:
                bot.send_message(chat_id, "❌ Не удалось сгенерировать картинку. Попробуй позже.")
            return

        # --- ЗАПОМИНАНИЕ ---
        if text.lower().startswith('запомни') and user_id == OWNER_ID:
            content = text[7:].strip()
            separators = [' — ', ' - ', ', ', ': ']
            key, value = None, None
            for sep in separators:
                if sep in content:
                    parts = content.split(sep, 1)
                    key = parts[0].strip()
                    value = parts[1].strip()
                    break
            if key is None:
                key = content
                value = "✅ Запомнено"
            save_memory(key, value)
            reply = f"🧠 Запомнил: **{key}** → {value}"
            if original_message:
                bot.reply_to(original_message, reply)
            else:
                bot.send_message(chat_id, reply)
            return

        if text.lower().startswith('запомни') and user_id != OWNER_ID:
            reply = "🧠 Я принимаю знания только от своего создателя."
            if original_message:
                bot.reply_to(original_message, reply)
            else:
                bot.send_message(chat_id, reply)
            return

        # --- ПАМЯТЬ ---
        memory_results = search_memory(text)
        if memory_results:
            reply = "📚 **Я помню:**\n"
            for key, value in memory_results[:3]:
                reply += f"• **{key}** → {value}\n"
            if original_message:
                bot.reply_to(original_message, reply)
            else:
                bot.send_message(chat_id, reply)
            return

        # --- GROQ ---
        if str_chat_id not in history_db:
            history_db[str_chat_id] = []

        text = re.sub(r'основан на', 'основал', text, flags=re.IGNORECASE)
        text = re.sub(r'кто основан', 'кто основал', text, flags=re.IGNORECASE)

        sys_prompt = {"role": "system", "content": (
            "Ты — Zelmy AI, мощный ИИ-ассистент.\n"
            "Отвечай развернуто, используя свои знания.\n"
            "Если не знаешь — честно скажи 'я не знаю'."
        )}

        history_db[str_chat_id].append({"role": "user", "content": text})
        if len(history_db[str_chat_id]) > 50:
            history_db[str_chat_id] = history_db[str_chat_id][-50:]

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

# --- 8. ПЛАТЕЖИ ---
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
    bot.send_message(message.chat.id, f"✅ Подписка **{plan}** активирована на 30 дней! Спасибо!")

# --- 9. КОМАНДЫ ---
@bot.message_handler(commands=['start'])
def start_cmd(message):
    logging.info(f"Start от {message.from_user.id}")
    track_user(message.from_user)
    str_chat_id = str(message.chat.id)
    history_db[str_chat_id] = []
    save_json(HISTORY_FILE, history_db)

    if message.from_user.id == OWNER_ID:
        welcome = (
            "🔥 **Zelmy AI — PLATINUM**\n\n"
            "📌 **Что я умею:**\n"
            "• Искать в интернете: `найди ...`\n"
            "• Генерировать картинки: `нарисуй ...`\n"
            "• Запоминать факты: `запомни: ...`\n"
            "• Отвечать на любые вопросы (Groq)\n\n"
            "💰 **Подписка:**\n"
            "• Бесплатно: 5 запросов/день\n"
            "• Premium: 30 Stars/мес — безлимит\n"
            "• Pro: 50 Stars/мес — + генерация картинок\n\n"
            "📌 **Команды:**\n"
            "/premium, /reset, /model, /stats, /forget, /show_memory"
        )
    else:
        welcome = (
            "🌱 **Zelmy AI**\n\n"
            "Я отвечаю на вопросы, ищу в интернете и генерирую картинки.\n\n"
            "💰 **Бесплатно:** 5 запросов/день\n"
            "🌟 **Премиум:** безлимит за 30 Stars/мес\n\n"
            "📌 **Команды:**\n"
            "/premium — купить подписку"
        )
    bot.reply_to(message, welcome)

@bot.message_handler(commands=['help'])
def show_help(message):
    text = (
        "🤖 **Команды Zelmy AI:**\n\n"
        "/start — перезапустить\n"
        "/premium — тарифы и подписка\n\n"
        "`найди ...` — поиск в интернете\n"
        "`нарисуй ...` — генерация картинки (Premium)\n"
        "`запомни: ...` — запомнить факт (только владелец)\n\n"
        "/reset, /model, /stats, /forget, /show_memory — админские"
    )
    bot.reply_to(message, text)

@bot.message_handler(commands=['premium'])
def premium_cmd(message):
    user_id = message.from_user.id
    plan = get_user_plan(user_id)
    if plan != "free":
        bot.reply_to(message, f"🌟 У тебя уже есть подписка **{plan}**")
        return
    text = "🌟 **Zelmy AI Premium**\n\n💰 **Тарифы:**\n• 30 Stars/мес — Premium\n• 50 Stars/мес — Pro\n\n📌 Нажми на кнопку ниже для оплаты."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💎 30 Stars — Premium", callback_data="buy_premium"))
    markup.add(types.InlineKeyboardButton("🌟 50 Stars — Pro", callback_data="buy_pro"))
    bot.reply_to(message, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ['buy_premium', 'buy_pro'])
def handle_purchase(call):
    plan = call.data.split('_')[1]
    price = 30 if plan == "premium" else 50
    title = "Zelmy AI Premium" if plan == "premium" else "Zelmy AI Pro"
    desc = "Безлимит запросов и поиска" if plan == "premium" else "Всё из Premium + генерация картинок"
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

@bot.message_handler(commands=['forget'])
def forget_memory(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Только создатель.")
        return
    global memory_db
    memory_db = {}
    save_json(MEMORY_FILE, memory_db)
    bot.reply_to(message, "🧠 Вся память удалена!")

@bot.message_handler(commands=['show_memory'])
def show_memory(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Только создатель.")
        return
    if not memory_db:
        bot.reply_to(message, "📭 Память пуста.")
        return
    text = "🧠 **Вся память:**\n\n"
    for key, value in list(memory_db.items())[:50]:
        text += f"• **{key}** → {value}\n"
    if len(memory_db) > 50:
        text += f"\n... и ещё {len(memory_db) - 50} фактов."
    bot.reply_to(message, text)

@bot.message_handler(commands=['stats'])
def show_stats(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Доступ запрещен.")
        return
    total_users = len(users_db)
    total_memory = len(memory_db)
    total_subs = len(subscriptions)
    bot.reply_to(message,
        f"📊 **Статистика:**\n"
        f"👤 Пользователей: {total_users}\n"
        f"🧠 Фактов в памяти: {total_memory}\n"
        f"🌟 Подписок: {total_subs}\n"
        f"⚙️ Модель: {CURRENT_MODEL}"
    )

# --- 10. ФОТО ---
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    track_user(message.from_user)
    if not is_premium(message.from_user.id):
        bot.reply_to(message, "❌ Обработка фото доступна только по подписке! /premium")
        return
    bot.reply_to(message, "📸 Фото получено. Обработка будет добавлена позже.")

# --- 11. ГОЛОСОВЫЕ ---
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

# --- 12. ТЕКСТ ---
@bot.message_handler(func=lambda message: True)
def handle_text(message):
    logging.info(f"Текст от {message.from_user.id}: {message.text[:50] if message.text else 'пусто'}")
    track_user(message.from_user)
    process_llm_request(message.chat.id, message.from_user.id, message.text, message)

# --- 13. ЗАПУСК ---
print("="*50)
print("🤖 **Zelmy AI — PLATINUM (APIFY)**")
print("✅ Поиск через Apify Brave")
print("="*50)

while True:
    try:
        bot.polling(none_stop=True, timeout=60)
    except Exception as e:
        logging.error(f"Сбой: {e}")
        print(f"⚠️ Переподключение через 5 секунд...")
        time.sleep(5)    
                                                   
