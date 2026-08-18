import logging
import telebot
import config

bot = telebot.TeleBot(config.BOT_TOKEN, threaded=True)

_bot_username_cache = {"value": None}

def get_bot_username():
    if not _bot_username_cache["value"]:
        try:
            _bot_username_cache["value"] = bot.get_me().username
        except Exception as e:
            logging.error(f"Не удалось получить username бота: {e}")
            return "ZelmyAIBot"
    return _bot_username_cache["value"]
