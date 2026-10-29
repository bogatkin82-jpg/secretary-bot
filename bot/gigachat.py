"""Минимальный клиент GigaChat API (Сбер): токен по ключу авторизации + chat/completions."""
import asyncio
import glob
import logging
import os
import ssl
import time
import uuid

import aiohttp
import certifi

from . import config

log = logging.getLogger(__name__)

AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"


class GigaChatError(RuntimeError):
    pass


class GigaChat:
    def __init__(self, auth_key: str, scope: str):
        self.auth_key = auth_key
        self.scope = scope
        self._token = None
        self._expires = 0.0
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max(1, int(os.getenv("GIGACHAT_CONCURRENCY", "1"))))
        self._session: aiohttp.ClientSession | None = None

    def _ssl(self):
        if not config.GIGACHAT_VERIFY_SSL:
            log.warning("Проверка сертификата GigaChat отключена (GIGACHAT_VERIFY_SSL=false)")
            return False
        # Сервера Сбера подписаны сертификатом НУЦ Минцифры — добавляем его к обычным.
        ctx = ssl.create_default_context(cafile=certifi.where())
        files = glob.glob(os.path.join(config.GIGACHAT_CA_DIR, "*.crt")) + \
            glob.glob(os.path.join(config.GIGACHAT_CA_DIR, "*.pem"))
        if not files:
            log.warning("Не найдены сертификаты Минцифры в %s — запросы к GigaChat могут не пройти",
                        config.GIGACHAT_CA_DIR)
        for f in files:
            ctx.load_verify_locations(f)
        return ctx

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=self._ssl()),
                timeout=aiohttp.ClientTimeout(total=120),
            )
        return self._session

    async def _get_token(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._expires - 60:
                return self._token
            http = await self._http()
            headers = {
                "Authorization": f"Basic {self.auth_key}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }
            async with http.post(AUTH_URL, headers=headers, data={"scope": self.scope}) as r:
                body = await r.text()
                if r.status != 200:
                    raise GigaChatError(f"Не удалось получить токен GigaChat ({r.status}): {body[:300]}")
                data = await r.json(content_type=None)
            self._token = data["access_token"]
            exp = float(data.get("expires_at") or 0)
            self._expires = exp / 1000 if exp > 1e12 else exp or time.time() + 1500
            return self._token

    async def _request(self, method: str, path: str, form_factory=None, **kwargs) -> dict:
        # Бесплатный тариф физлиц разрешает 1 запрос одновременно — выстраиваем запросы в очередь
        # и при 429 (Too Many Requests) ждём и повторяем.
        async with self._sem:
            http = await self._http()
            auth_retried = False
            for attempt in range(6):
                token = await self._get_token()
                headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
                if form_factory:              # multipart-форму нельзя отправить дважды — собираем заново
                    kwargs["data"] = form_factory()
                async with http.request(method, f"{config.GIGACHAT_BASE_URL}{path}",
                                        headers=headers, **kwargs) as r:
                    if r.status == 401 and not auth_retried:   # токен протух раньше срока
                        self._token = None
                        auth_retried = True
                        continue
                    if r.status == 429 and attempt < 5:
                        try:
                            delay = float(r.headers.get("Retry-After") or 0)
                        except ValueError:
                            delay = 0
                        delay = max(delay, 2 ** (attempt + 1))   # 2, 4, 8, 16, 32 с
                        log.warning("GigaChat 429, повтор через %.0f с", delay)
                        await asyncio.sleep(delay)
                        continue
                    body = await r.text()
                    if r.status != 200:
                        raise GigaChatError(f"GigaChat ответил {r.status}: {body[:300]}")
                    return await r.json(content_type=None) if body else {}
            raise GigaChatError("GigaChat: не удалось выполнить запрос")

    async def chat(self, *, model: str, system: str, user: str, max_tokens: int,
                   temperature: float = 0.3, attachments: list[str] | None = None) -> str:
        user_msg = {"role": "user", "content": user}
        if attachments:
            user_msg["attachments"] = attachments
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, user_msg],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        data = await self._request("POST", "/chat/completions", json=payload)
        return (data["choices"][0]["message"]["content"] or "").strip()

    async def upload_file(self, content: bytes, filename: str, mime: str) -> str:
        def form():
            f = aiohttp.FormData()
            f.add_field("file", content, filename=filename, content_type=mime)
            f.add_field("purpose", "general")
            return f
        data = await self._request("POST", "/files", form_factory=form)
        return data["id"]

    async def delete_file(self, file_id: str):
        try:
            await self._request("POST", f"/files/{file_id}/delete")
        except Exception as e:
            log.warning("Не удалил файл %s: %s", file_id, e)
