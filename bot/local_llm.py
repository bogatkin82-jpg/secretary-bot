"""Локальная нейросеть на вашем компьютере (Ollama / LM Studio — OpenAI-совместимый API).

Если компьютер спит или недоступен, запрос падает быстро (5 с на соединение),
бот запоминает это на LOCAL_LLM_RETRY_MINUTES минут и пока ходит в облачную нейросеть.
"""
import asyncio
import logging
import re
import time

import aiohttp

from . import config

log = logging.getLogger(__name__)

_session: aiohttp.ClientSession | None = None
_lock = asyncio.Lock()          # локальная модель обрабатывает запросы по одному
_down_until = 0.0
state = {"last": None, "last_ok_ts": 0.0, "last_error": ""}   # для /settings

_THINK = re.compile(r"<think>.*?</think>", re.S)


def enabled() -> bool:
    return bool(config.LOCAL_LLM_URL)


def available_now() -> bool:
    return enabled() and time.time() >= _down_until


def mark_down(reason: str):
    global _down_until
    _down_until = time.time() + config.LOCAL_LLM_RETRY_MINUTES * 60
    state["last_error"] = reason
    log.warning("Локальная модель недоступна (%s) — %s мин работаю через запасную",
                reason, config.LOCAL_LLM_RETRY_MINUTES)


async def _http() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.LOCAL_LLM_TIMEOUT, sock_connect=5))
    return _session


async def chat(system: str, user: str, max_tokens: int) -> str:
    payload = {
        "model": config.LOCAL_LLM_MODEL,
        # «/no_think» отключает долгие рассуждения у моделей Qwen3 — ответ нужен сразу
        "messages": [{"role": "system", "content": system + "\n/no_think"},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens + 512,
        "temperature": 0.3,
        "stream": False,
    }
    http = await _http()
    async with _lock:
        async with http.post(f"{config.LOCAL_LLM_URL}/chat/completions", json=payload) as r:
            body = await r.text()
            if r.status != 200:
                raise RuntimeError(f"локальная модель ответила {r.status}: {body[:200]}")
            data = await r.json(content_type=None)
    text = data["choices"][0]["message"].get("content") or ""
    text = _THINK.sub("", text).strip()
    state["last"] = "local"
    state["last_ok_ts"] = time.time()
    return text
