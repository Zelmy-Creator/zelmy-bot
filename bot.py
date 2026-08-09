import json
import os
import time
import requests
import telebot
from telebot import types
import logging
import re

# --- 1. ЛОГИРОВАНИЕ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 2. КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
OWNER_ID = 8482782819  # Твой Telegram ID

HISTORY_FILE = "chat_history.json"
USERS_FILE = "users.json"
MEMORY_FILE = "memory.json"

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
    """Ищет ТОЧНОЕ совпадение в памяти"""
    results = []
    query_lower = query.lower()
    
    for key, value in memory_db.items():
        if query_lower in key:
            results.append((key, value))
    
    return results[:3]

# --- 4. ОСНОВНАЯ ЛОГИКА ---
def process_llm_request(chat_id, user_id, text, original_message=None):
    str_chat_id = str(chat_id)
    try:
        bot.send_chat_action(chat_id, 'typing')
        
        # ============================================================
        # 1. ЖЁСТКИЙ ОТВЕТ НА "КТО Я"
        # ============================================================
        if any(phrase in text.lower() for phrase in ['кто я', 'кто я?', 'я кто', 'я твой создатель', 'я создатель']):
            if user_id == OWNER_ID:
                reply = "Ты — Zelmy Create, мой создатель. Я всегда буду помнить это."
            else:
                reply = "Ты — пользователь. Я создан Zelmy Create, и он мой единственный создатель."
            if original_message:
                bot.reply_to(original_message, reply)
            else:
                bot.send_message(chat_id, reply)
            return
        # ============================================================
        
        # --- ЗАПОМИНАНИЕ ДЛЯ ВЛАДЕЛЬЦА ---
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

        # --- ПОИСК В ПАМЯТИ ---
        memory_results = search_memory(text)
        memory_context = ""
        if memory_results:
            memory_context = "\n\n📚 **Я помню:**\n"
            for key, value in memory_results[:3]:
                memory_context += f"• **{key}** → {value}\n"

        # --- ЕСЛИ ЕСТЬ В ПАМЯТИ ---
        if memory_results:
            if original_message:
                bot.reply_to(original_message, memory_context)
            else:
                bot.send_message(chat_id, memory_context)
            return

        # --- ЕСЛИ НЕТ В ПАМЯТИ, ИСПОЛЬЗУЕМ GROQ ---
        if str_chat_id not in history_db:
            history_db[str_chat_id] = []

        # Исправляем грамматику
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

# --- 5. КОМАНДЫ ---
@bot.message_handler(commands=['start'])
def start_cmd(message):
    logging.info(f"Start от {message.from_user.id}")
    track_user(message.from_user)
    str_chat_id = str(message.chat.id)
    history_db[str_chat_id] = []
    save_json(HISTORY_FILE, history_db)

    if message.from_user.id == OWNER_ID:
        welcome = (
            "🔥 **Zelmy AI — ГИБРИДНЫЙ РЕЖИМ**\n\n"
            "📌 **Как я работаю:**\n"
            "1. Сначала ищу в твоей памяти\n"
            "2. Если не нахожу — использую ИИ (Groq)\n\n"
            "📌 **Ты можешь:**\n"
            "• Запоминать факты: `запомни: вопрос — ответ`\n"
            "• Получать ответы от ИИ на любые вопросы\n\n"
            "📌 **Команды:**\n"
            "/reset — очистить историю\n"
            "/model — сменить модель\n"
            "/stats — статистика\n"
            "/forget — удалить ВСЮ память\n"
            "/show_memory — показать твою память"
        )
    else:
        welcome = (
            "🌱 **Zelmy AI**\n\n"
            "Я отвечаю на вопросы, используя мощный ИИ.\n"
            "Мой создатель постоянно учит меня новому.\n\n"
            "📌 **Просто задай вопрос** — я постараюсь ответить."
        )

    bot.reply_to(message, welcome)

@bot.message_handler(commands=['help'])
def show_help(message):
    if message.from_user.id == OWNER_ID:
        text = (
            "🤖 **Команды владельца:**\n"
            "/start — перезапустить\n"
            "/reset — очистить историю\n"
            "/model — сменить модель (8B/70B)\n"
            "/stats — статистика\n"
            "/show_memory — показать всю память\n"
            "/forget — удалить ВСЮ память\n\n"
            "📌 **Как запомнить:**\n"
            "`запомни: вопрос — ответ`"
        )
    else:
        text = (
            "🤖 **Команды:**\n"
            "/start — перезапустить\n\n"
            "📌 **Как это работает:**\n"
            "Я использую мощный ИИ, чтобы отвечать на твои вопросы."
        )
    bot.reply_to(message, text)

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
    bot.reply_to(message,
        f"📊 **Статистика:**\n"
        f"👤 Пользователей: {total_users}\n"
        f"🧠 Фактов в памяти: {total_memory}\n"
        f"⚙️ Модель: {CURRENT_MODEL}"
    )

# --- 6. ГОЛОСОВЫЕ ---
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

# --- 7. ТЕКСТ ---
@bot.message_handler(func=lambda message: True)
def handle_text(message):
    logging.info(f"Текст от {message.from_user.id}: {message.text[:50] if message.text else 'пусто'}")
    track_user(message.from_user)
    process_llm_request(message.chat.id, message.from_user.id, message.text, message)

# --- 8. ЗАПУСК ---
print("="*50)
print("🤖 **Zelmy AI — ГИБРИДНЫЙ РЕЖИМ**")
print("✅ Сначала точная память, потом мощный ИИ")
print("="*50)

while True:
    try:
        bot.polling(none_stop=True, timeout=60)
    except Exception as e:
        logging.error(f"Сбой: {e}")
        print(f"⚠️ Переподключение через 5 секунд...")
        time.sleep(5)
