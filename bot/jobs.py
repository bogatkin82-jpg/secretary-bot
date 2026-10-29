"""Фоновые задачи: напоминания, «вы не ответили», утренний план, вечерняя сводка, конец «нет на связи»."""
import asyncio
import logging
import time
from collections import OrderedDict
from datetime import datetime, timedelta

from aiogram import Bot

from . import ai, config, state
from .db import db
from .util import fmt_ts, notify, task_kb

log = logging.getLogger(__name__)

DAY_HOURS = (9, 22)   # «вы не ответили» напоминаем только днём


async def build_summary(since_ts: float, period: str) -> str | None:
    rows = await db.messages_since(since_ts)
    chats: "OrderedDict[str, list]" = OrderedDict()
    for r in rows:
        chats.setdefault(r["chat_title"], []).append(r)
    # чаты, где писали только вы, неинтересны
    chats = OrderedDict((k, v) for k, v in chats.items() if any(not r["from_owner"] for r in v))
    if not chats:
        return None
    text = await ai.summarize(chats, period)
    tasks = await db.open_tasks()
    if tasks:
        text += "\n\n📝 Открытые задачи:\n" + "\n".join(
            f"{t['id']}. {t['text']} — {fmt_ts(t['due_ts'])}" for t in tasks[:15])
        if len(tasks) > 15:
            text += f"\n…и ещё {len(tasks) - 15} (/tasks)"
    return text


def _ago(ts: float) -> str:
    mins = int((time.time() - ts) / 60)
    if mins < 60:
        return f"{mins} мин"
    hours = mins // 60
    return f"{hours} ч" if hours < 48 else f"{hours // 24} дн"


async def waiting_list(min_age_hours: float = 0) -> list:
    """Кто ждёт вашего ответа (последнее сообщение — от собеседника, не мелочь)."""
    now = time.time()
    rows = await db.waiting_chats(now - min_age_hours * 3600, now - 3 * 86400)
    out = []
    for r in rows:
        if r["importance"] == "low":
            continue
        status = await db.contact_status(r["chat_id"])
        if status == "ignore":
            continue
        out.append((r, status))
    return out


async def _reminders(bot: Bot):
    for t in await db.due_tasks(time.time()):
        src = f"\n(из чата «{t['chat_title']}»)" if t["chat_title"] else ""
        await notify(bot, f"⏰ Напоминание: {t['text']}{src}", task_kb(t["id"]))
        await db.set_task(t["id"], reminded=1)


async def _unanswered(bot: Bot):
    wait = await db.get("wait_hours")
    if wait == "off" or await state.effective_mode() == "away":
        return
    now = datetime.now(config.TZ)
    if not (DAY_HOURS[0] <= now.hour < DAY_HOURS[1]):
        return
    wait_h = float(wait)
    for r, status in await waiting_list(min(wait_h, 1.0)):
        limit = min(wait_h, 1.0) if status == "vip" else wait_h   # важных — через час
        if time.time() - r["ts"] < limit * 3600 or r["nudged"] == r["id"]:
            continue
        star = "⭐ " if status == "vip" else ""
        await notify(bot, f"⏳ {star}{r['chat_title']} ждёт ответа уже {_ago(r['ts'])}:\n«{r['text'][:300]}»")
        await db.set_nudged(r["chat_id"], r["id"])


async def _daily(bot: Bot, key_time: str, key_date: str) -> bool:
    """True, если пора отправить ежедневное сообщение (и отметить, что отправили)."""
    value = await db.get(key_time)
    if value == "off":
        return False
    now = datetime.now(config.TZ)
    hh, mm = map(int, value.split(":"))
    today = now.strftime("%Y-%m-%d")
    if (now.hour, now.minute) < (hh, mm) or await db.get(key_date) == today:
        return False
    await db.set(key_date, today)
    return True


async def morning_text() -> str:
    now = datetime.now(config.TZ)
    end_of_day = now.replace(hour=23, minute=59, second=59).timestamp()
    tasks = await db.open_tasks()
    overdue = [t for t in tasks if t["due_ts"] and t["due_ts"] < now.timestamp()]
    today = [t for t in tasks if t["due_ts"] and now.timestamp() <= t["due_ts"] <= end_of_day]
    nodate = [t for t in tasks if not t["due_ts"]]
    lines = [f"☀️ Доброе утро! План на {now:%d.%m}"]
    if overdue:
        lines += ["", "⚠️ Просрочено:"] + [f"{t['id']}. {t['text']} — {fmt_ts(t['due_ts'])}" for t in overdue]
    lines += ["", "📝 Сегодня:"]
    lines += [f"{t['id']}. {t['text']} — {fmt_ts(t['due_ts'])[-5:]}" for t in today] or ["задач на сегодня нет"]
    if nodate:
        lines += ["", f"Без срока: {len(nodate)} (/tasks)"]
    waiting = await waiting_list(1)
    if waiting:
        lines += ["", "⏳ Ждут вашего ответа:"]
        lines += [f"{'⭐ ' if s == 'vip' else ''}{r['chat_title']} — {_ago(r['ts'])}: «{r['text'][:80]}»"
                  for r, s in waiting[:10]]
    return "\n".join(lines)


async def _morning(bot: Bot):
    if await _daily(bot, "morning_time", "last_morning_date"):
        await notify(bot, await morning_text())


async def _digest(bot: Bot):
    if not await _daily(bot, "digest_time", "last_digest_date"):
        return
    since = float(await db.get("last_digest_ts") or 0) or time.time() - 86400
    await db.set("last_digest_ts", str(time.time()))
    text = await build_summary(since, "время с прошлой сводки")
    await notify(bot, "🗞 Сводка за день\n\n" + (text or "Новых сообщений не было."))


async def end_away(bot: Bot, reason: str = "закончился"):
    a = await state.away()
    if not a:
        return
    started = float(a["id"] or 0) or time.time() - 86400
    await db.set("away_until", "")
    await db.set("away_note", "")
    text = await build_summary(started, "время, пока вас не было")
    await notify(bot, f"👋 Режим «нет на связи» {reason}. С возвращением!\n\n"
                      + (text or "Пока вас не было, никто не писал."))


async def _away_end(bot: Bot):
    a = await state.away()
    if a and time.time() >= a["until"]:
        await end_away(bot)


async def run(bot: Bot):
    while True:
        for job in (_reminders, _away_end, _unanswered, _morning, _digest):
            try:
                await job(bot)
            except Exception:
                log.exception("Ошибка в фоновой задаче %s", job.__name__)
        await asyncio.sleep(30)
