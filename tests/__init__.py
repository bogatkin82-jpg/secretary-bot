"""Тесты запускаются без реальных ключей и без сети."""
import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("LLM_PROVIDER", "local")
os.environ.setdefault("LOCAL_LLM_URL", "http://127.0.0.1:1/v1")
