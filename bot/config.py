import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name} (см. .env.example)")
    return value


BOT_TOKEN = _req("BOT_TOKEN")
OWNER_ID = int(_req("OWNER_ID"))

# Нейросеть: gigachat (Сбер), yandex (YandexGPT) или claude.
# По умолчанию — та, чей ключ указан.
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
GIGACHAT_BASE_URL = os.getenv("GIGACHAT_BASE_URL", "https://api.giga.chat/v1").rstrip("/")
GIGACHAT_CA_DIR = os.getenv("GIGACHAT_CA_DIR", "certs")  # сертификаты НУЦ Минцифры
GIGACHAT_VERIFY_SSL = os.getenv("GIGACHAT_VERIFY_SSL", "true").lower() != "false"
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
# Локальная модель (Ollama, LM Studio и т.п. — любой OpenAI-совместимый адрес).
# Если задана — бот сначала спрашивает её, а облачную нейросеть использует как запасную,
# пока локальная недоступна (например, ноутбук закрыт).
LOCAL_LLM_URL = os.getenv("LOCAL_LLM_URL", "").strip().rstrip("/")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "qwen3.5:9b").strip()
LOCAL_LLM_TIMEOUT = int(os.getenv("LOCAL_LLM_TIMEOUT", "180"))
LOCAL_LLM_RETRY_MINUTES = int(os.getenv("LOCAL_LLM_RETRY_MINUTES", "5"))

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").strip().lower() or (
    "gigachat" if GIGACHAT_AUTH_KEY else "yandex" if YANDEX_API_KEY
    else "claude" if ANTHROPIC_API_KEY else "local" if LOCAL_LLM_URL else "")

# LLM_MODEL — быстрая модель для разбора каждого сообщения и автоответов,
# SUMMARY_MODEL — более сильная, для сводок.
if LLM_PROVIDER == "gigachat":
    if not GIGACHAT_AUTH_KEY:
        raise SystemExit("Для GigaChat нужен GIGACHAT_AUTH_KEY (см. .env.example)")
    LLM_MODEL = os.getenv("LLM_MODEL", "GigaChat-2")
    SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "GigaChat-2-Pro")
elif LLM_PROVIDER == "yandex":
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        raise SystemExit("Для YandexGPT нужны YANDEX_API_KEY и YANDEX_FOLDER_ID (см. .env.example)")
    LLM_MODEL = os.getenv("LLM_MODEL", "yandexgpt-lite/latest")
    SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "yandexgpt/latest")
elif LLM_PROVIDER == "claude":
    if not ANTHROPIC_API_KEY:
        raise SystemExit("Для Claude нужен ANTHROPIC_API_KEY (см. .env.example)")
    LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
    SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "claude-sonnet-5-5")
elif LLM_PROVIDER == "local":
    if not LOCAL_LLM_URL:
        raise SystemExit("Для локальной модели нужен LOCAL_LLM_URL (см. .env.example)")
    LLM_MODEL = SUMMARY_MODEL = LOCAL_LLM_MODEL
else:
    raise SystemExit("Укажите нейросеть: GIGACHAT_AUTH_KEY, YANDEX_API_KEY, ANTHROPIC_API_KEY "
                     "или LOCAL_LLM_URL")

# Модель для расшифровки голосовых (GigaChat принимает аудиофайлы)
VOICE_MODEL = os.getenv("VOICE_MODEL", "GigaChat-2-Pro")

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
DB_PATH = os.getenv("DB_PATH", "data/secretary.db")

# Сколько секунд ждать после последнего сообщения собеседника,
# прежде чем разбирать пачку (люди часто пишут несколькими сообщениями).
DEBOUNCE_SECONDS = int(os.getenv("DEBOUNCE_SECONDS", "20"))
# Не больше стольких автоответов в один чат за час — защита от зацикливания.
MAX_AUTOREPLIES_PER_HOUR = int(os.getenv("MAX_AUTOREPLIES_PER_HOUR", "2"))
# Сколько сообщений истории отдавать нейросети для контекста.
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "30"))

# Значения настроек по умолчанию (меняются командами в чате с ботом).
DEFAULT_SETTINGS = {
    "mode": "draft",          # off | draft | auto
    "idle_minutes": "15",     # отвечать, только если вы молчите в чате дольше N минут
    "digest_time": "21:00",   # время ежедневной сводки, или "off"
    "persona": (
        "Отвечай коротко, дружелюбно, на «ты» или «вы» — как обращается собеседник. "
        "Скажи, что я сейчас занят и отвечу позже."
    ),
    "wait_hours": "3",        # напомнить «вы не ответили» через N часов, или "off"
    "morning_time": "09:00",  # утренний план, или "off"
    "night": "off",           # ночью отвечать самому: "23:00-08:00" или "off"
    "voice": "on",            # расшифровывать голосовые
    "away_until": "",         # режим «нет на связи»: unix-время окончания
    "away_note": "",
    "away_id": "",
    "last_morning_date": "",
    "owner_name": "",
    "last_digest_date": "",
    "last_digest_ts": "0",
}
