"""Расшифровка голосовых и кружков через GigaChat (аудиофайл во вложении)."""
import asyncio
import io
import logging

from aiogram import Bot
from aiogram.types import Message

from . import ai, config

log = logging.getLogger(__name__)

TRANSCRIBE_SYSTEM = """Ты — сервис расшифровки речи. Во вложении голосовое сообщение.
Запиши дословно, что говорится, на языке оригинала, с пунктуацией.
Не добавляй ничего от себя: ни пояснений, ни заголовков, ни оценок.
Если речи нет или ничего не разобрать — ответь ровно: [неразборчиво]"""


def media_of(m: Message):
    """(file_id, длительность, подпись) для голосового/кружка/аудио, иначе None."""
    if m.voice:
        return m.voice.file_id, m.voice.duration, "голосовое"
    if m.video_note:
        return m.video_note.file_id, m.video_note.duration, "кружок"
    if m.audio and (m.audio.duration or 0) <= 600:
        return m.audio.file_id, m.audio.duration, "аудио"
    return None


def fmt_duration(sec: int | None) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


async def _to_mp3(data: bytes) -> bytes:
    """ffmpeg: любое аудио/видео -> mp3 моно 16 кГц (компактно и понятно модели)."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", "-f", "mp3", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(data)
    if proc.returncode != 0 or not out:
        raise RuntimeError(f"ffmpeg: {err.decode(errors='ignore')[:200]}")
    return out


def available() -> bool:
    return config.LLM_PROVIDER == "gigachat"


async def transcribe(bot: Bot, file_id: str) -> str:
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    mp3 = await _to_mp3(buf.getvalue())
    client = ai.client
    file_ref = await client.upload_file(mp3, "voice.mp3", "audio/mpeg")
    try:
        text = await client.chat(model=config.VOICE_MODEL, system=TRANSCRIBE_SYSTEM,
                                 user="Расшифруй это голосовое сообщение.",
                                 max_tokens=2000, temperature=0.0, attachments=[file_ref])
    finally:
        await client.delete_file(file_ref)
    return text.strip()
