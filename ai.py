import io
import re
import json
import time
import random
import logging
import urllib.parse
import requests
from bs4 import BeautifulSoup
from PIL import Image
import pytesseract
from gtts import gTTS
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS
import config
import database as db
from cache import search_cache


# ---------- ФИЛЬТР ЭКСТРЕМИЗМА ----------
EXTREMISM_PATTERNS = [
    r'\bкак\s+(сделать|изготовить|собрать)\s+(бомб|взрывчат|сву)',
    r'\bкак\s+(вступить|попасть|присоединиться)\s+\b\s+(игил|аль-?каид|запрещенн)',
    r'\bпризыв(ы)?\s+\b\s+(терроризм|насильственн|свержени)',
    r'\bоправдани[ея]\s+(терроризм|геноцид)',
    r'\b(вербовк|вербуй|вербую)\b.*(терро|экстрем)',
]
EXTREMISM_RE = [re.compile(p, re.IGNORECASE) for p in EXTREMISM_PATTERNS]
EXTREMISM_REFUSAL = "🙅 Не могу помочь с этим запросом – тема нарушает правила бота."

def is_extremism_related(text):
    lowered = text.lower()
    return any(p.search(lowered) for p in EXTREMISM_RE)


# ---------- ПОИСК (с кэшем) ----------
def _dedupe_by_domain(results, limit):
    seen = set()
    out = []
    for r in results:
        domain = urllib.parse.urlparse(r.get('link', '')).netloc
        if domain and domain in seen:
            continue
        seen.add(domain)
        out.append(r)
        if len(out) >= limit:
            break
    return out

def search_web(query, max_results=6):
    cached = search_cache.get(query.lower().strip())
    if cached is not None:
        return cached

    for region in ("ru-ru", None):
        try:
            with DDGS() as ddgs:
                kwargs = {"max_results": max_results * 2}
                if region:
                    kwargs["region"] = region
                results = []
                for r in ddgs.text(query, **kwargs):
                    snippet = (r.get('body', '') or '').strip()
                    results.append({
                        'title': (r.get('title') or 'Без заголовка').strip(),
                        'link': r.get('link', '').strip(),
                        'snippet': snippet[:300]
                    })
                results = _dedupe_by_domain(results, max_results)
                if results:
                    search_cache.set(query.lower().strip(), results, config.SEARCH_CACHE_TTL)
                    return results
        except Exception as e:
            logging.error(f"Поиск ошибка: {e}")
            db.track_error("search_web", e)
            return None

    # Fallback: поиск через HTML-парсинг (duckduckgo html)
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            results = []
            for row in soup.select('.result'):
                title_tag = row.select_one('.result__a')
                snippet_tag = row.select_one('.result__snippet')
                if title_tag:
                    results.append({
                        'title': title_tag.get_text(strip=True),
                        'link': title_tag.get('href', ''),
                        'snippet': snippet_tag.get_text(strip=True)[:300] if snippet_tag else ''
                    })
            results = _dedupe_by_domain(results, max_results)
            if results:
                search_cache.set(query.lower().strip(), results, config.SEARCH_CACHE_TTL)
                return results
    except Exception as e:
        logging.error(f"Fallback поиск ошибка: {e}")
        db.track_error("search_web", e)
        return None

    return None


# ---------- КАРТИНКИ (временно отключено — см. handlers.py) ----------
def generate_image(prompt, retries=2):
    # Функция временно отключена
    return None


# ---------- TTS ----------
def text_to_speech(text):
    try:
        clean = re.sub(r'[*_#\[\]\(\)]', '', text)[:800]
        tts = gTTS(text=clean, lang='ru')
        audio = io.BytesIO()
        tts.write_to_fp(audio)
        audio.seek(0)
        audio.name = "voice.mp3"
        return audio
    except Exception as e:
        logging.error(f"TTS ошибка: {e}")
        return None


# ---------- ТРАНСКРИБАЦИЯ ГОЛОСА ----------
def transcribe_voice(file_bytes, filename="voice.ogg"):
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {config.GROQ_KEY}"},
            files={'file': (filename, file_bytes)},
            data={'model': config.WHISPER_MODEL, "language": "ru"},
            timeout=60
        )
        if response.status_code == 200:
            return response.json().get("text", "").strip()
        logging.error(f"Groq transcription ошибка {response.status_code}: {response.text[:200]}")
    except Exception as e:
        logging.error(f"Ошибка транскрибации: {e}")
    return None


# ---------- OCR ----------
def extract_text_from_image(image_bytes):
    try:
        image = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(image, lang='rus+eng')
        return text.strip() if text.strip() else "Текст не найден"
    except Exception as e:
        logging.error(f"OCR ошибка: {e}")
        return "Ошибка распознавания"


# ---------- ПОТОКОВАЯ ГЕНЕРАЦИЯ ОТВЕТА (Groq) ----------
def stream_groq_completion(messages, on_delta, max_tokens=2000, temperature=0.5):
    """Стримит ответ от Groq, вызывая on_delta(full_text_so_far) на каждый новый кусок.
    Возвращает полный текст либо None при ошибке."""
    full_text = ""
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.GROQ_KEY}"},
            json={
                "model": config.CURRENT_MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True
            },
            timeout=60,
            stream=True
        )
        if response.status_code != 200:
            logging.error(f"Groq stream ошибка {response.status_code}")
            return None

        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode('utf-8', errors='ignore')
            if not line.startswith('data: '):
                continue
            data_str = line[6:].strip()
            if data_str == '[DONE]':
                break
            try:
                chunk = json.loads(data_str)
                delta = chunk['choices'][0]['delta'].get('content', '')
            except Exception:
                continue
            if not delta:
                continue
            full_text += delta
            if len(full_text) >= config.STREAM_MAX_CHARS:
                full_text = full_text[:config.STREAM_MAX_CHARS] + "..."
                on_delta(full_text)
                break
            on_delta(full_text)
    except Exception as e:
        logging.error(f"Ошибка стриминга Groq: {e}")
        db.track_error("stream_groq_completion", e)
        return None

    return full_text if full_text.strip() else None


def groq_completion_simple(messages, max_tokens=1500, timeout=30):
    """Обычный (не потоковый) запрос – используется для короткого fallback-ответа поиска."""
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.GROQ_KEY}"},
            json={
                "model": config.CURRENT_MODEL,
                "messages": messages,
                "max_tokens": max_tokens
            },
            timeout=timeout
        )
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
    except Exception as e:
        logging.error(f"groq_completion_simple ошибка: {e}")
        db.track_error("groq_completion_simple", e)
    return None
