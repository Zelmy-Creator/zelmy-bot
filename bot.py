import time
import logging
import threading
import config
import database as db
from bot_instance import bot
import handlers  # noqa: F401

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def _history_cleanup_loop():
    while True:
        time.sleep(24 * 60 * 60)
        try:
            removed = db.prune_old_history()
            logging.info(f"Очистка старой истории: удалено {removed}")
        except Exception as e:
            logging.error(f"Ошибка очистки истории: {e}")


if __name__ == "__main__":
    # Инициализация базы данных
    db.init_db()
    logging.info("База данных инициализирована")

    # Удаляем старый вебхук на случай конфликта
    try:
        bot.delete_webhook()
        logging.info("Webhook удалён")
    except Exception as e:
        logging.warning(f"Не удалось удалить вебхук: {e}")

    # Запускаем фоновую очистку истории
    cleanup_thread = threading.Thread(target=_history_cleanup_loop, daemon=True)
    cleanup_thread.start()

    logging.info("Zelmy AI бот запускается...")

    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        logging.error(f"Критическая ошибка polling: {e}")
