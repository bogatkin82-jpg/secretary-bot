"""Текущий режим работы: обычный / ночной / «нет на связи»."""
import re
import time
from datetime import datetime

from . import config
from .db import db


def parse_range(value: str):
    """'23:00-08:00' -> ((23, 0), (8, 0)) или None."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})\s*", value or "")
    if not m:
        return None
    a, b = (int(m[1]), int(m[2])), (int(m[3]), int(m[4]))
    if not (a[0] < 24 and b[0] < 24 and a[1] < 60 and b[1] < 60):
        return None
    return a, b


def in_range(rng, now: datetime | None = None) -> bool:
    now = now or datetime.now(config.TZ)
    cur = (now.hour, now.minute)
    start, end = rng
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end      # через полночь


async def away() -> dict | None:
    """Если включён режим «нет на связи» — {'until': ts, 'note': ..., 'id': ...}."""
    until = await db.get("away_until")
    if not until:
        return None
    until_ts = float(until)
    return {"until": until_ts, "note": await db.get("away_note"), "id": await db.get("away_id")}


async def effective_mode() -> str:
    """off | draft | auto | away"""
    a = await away()
    if a and time.time() < a["until"]:
        return "away"
    rng = parse_range(await db.get("night"))
    if rng and in_range(rng):
        return "auto"
    return await db.get("mode")
