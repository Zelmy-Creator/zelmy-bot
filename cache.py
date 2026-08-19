import time
import threading
import hashlib
from collections import defaultdict, deque

import config


class TTLCache:
    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def _key(self, raw_key):
        return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()

    def get(self, raw_key):
        key = self._key(raw_key)
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del self._data[key]
                return None
            return value

    def set(self, raw_key, value, ttl_seconds):
        key = self._key(raw_key)
        with self._lock:
            self._data[key] = (value, time.time() + ttl_seconds)
            # лёгкая уборка, чтобы словарь не рос бесконечно
            if len(self._data) > 1000:
                now = time.time()
                expired = [k for k, (_, exp) in self._data.items() if exp < now]
                for k in expired:
                    del self._data[k]


search_cache = TTLCache()  # кэш результатов поиска по тексту запроса


class RateLimiter:
    """Скользящее окно: не больше N запросов за Т секунд на пользователя."""

    def __init__(self, max_requests, window_seconds):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, user_id):
        now = time.time()
        with self._lock:
            hits = self._hits[user_id]
            while hits and now - hits[0] > self.window_seconds:
                hits.popleft()
            if len(hits) >= self.max_requests:
                return False
            hits.append(now)
            return True


flood_limiter = RateLimiter(config.FLOOD_MAX_REQUESTS, config.FLOOD_WINDOW_SECONDS)
