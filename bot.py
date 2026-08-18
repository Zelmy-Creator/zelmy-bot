import time
import logging
import threading
import config
import database as db
from bot_instance import bot
import handlers # noqa: F401 - импорт перситрирует все @bot.message_handler / @bot.callback_query_handler

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def _history_cleanup_loop():
    """Раз в сутки удаляет сообщения истории старше config.HISTORY_RETENTION_DAYS (30 дней)."""
    while True:
        time.sleep(24 * 60 * 60)
        try:
            removed = db.prune_old_history()
            logging.info(f"Очистка старой истории: удалено {removed}")
