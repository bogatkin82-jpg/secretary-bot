"""Фоновые задачи: напоминания, «вы не ответили», утренний план, вечерняя сводка, конец «нет на связи»."""
import asyncio
import logging
import time
from collections import OrderedDict
from datetime import datetime, timedelta

from aiogram import Bot

from . import ai, config, state
from .db import db
from .util import chats_kb, fmt_ts, notify, reply_kb, task_kb

log = logging.getLogger(__name__)

DAY_HOURS = (9, 22)   # «вы не ответили» напоминаем только днём


async def build_summary(since_ts: float, period: str) -> str | None:
    rows = await db.messages_since(since_ts)
    # Ключ — chat_id, а не имя: тёзки не сливаются, переименование не рвёт историю.
    chats: "OrderedDict[int, list]" = OrderedDict()
    titles: dict[int, str] = {}
    for r in rows:
        chats.setdefault(r["chat_id"], []).append(r)
        titles[r["chat_id"]] = r["chat_title"]
    # чаты, где писали только вы, неинтересны
    chats = OrderedDict((k, v) for k, v in chats.items() if any(not r["from_owner"] for r in v))
    if not chats:
        return None
    # кто реально ждёт ответа — считаем сами, а не доверяем нейросети
    status = {}
    for chat_id, msgs in chats.items():
        last = msgs[-1]
        if last["by_bot"] == 1:
            status[chat_id] = "bot"        # ответил сам бот — владельцу делать нечего
        elif last["from_owner"] or await db.handled_ts(chat_id) >= last["ts"]:
            status[chat_id] = "answered"
        elif last["importance"] == "low":
            status[chat_id] = "optional"
        else:
            status[chat_id] = "waiting"
    text = await ai.summarize(chats, period, status, titles)
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
        await notify(bot, f"⏳ {star}{r['chat_title']} ждёт ответа уже {_ago(r['ts'])}:\n«{r['text'][:300]}»",
                     reply_kb(r["chat_id"]))
        await db.set_nudged(r["chat_id"], r["id"])


_daily_attempts: dict[str, float] = {}


def _today_str() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m-%d")


async def _daily_due(key_time: str, key_date: str) -> bool:
    """Пора ли отправлять ежедневное сообщение.

    Дата «отправлено» ставится только после успешной отправки. Если окно
    (DAILY_WINDOW_HOURS) уже прошло — помечаем день пропущенным, чтобы
    «Доброе утро» не пришло вечером.
    """
    value = await db.get(key_time)
    if value == "off":
        return False
    now = datetime.now(config.TZ)
    try:
        hh, mm = map(int, value.split(":"))
    except ValueError:
        log.error("Некорректное время %s=%r", key_time, value)
        return False
    scheduled = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    if now < scheduled or await db.get(key_date) == today:
        return False
    if now > scheduled + timedelta(hours=config.DAILY_WINDOW_HOURS):
        await db.set(key_date, today)
        log.info("%s: окно отправки прошло — сегодня пропускаю", key_time)
        return False
    last_try = _daily_attempts.get(key_time, 0.0)
    if time.time() - last_try < config.DAILY_RETRY_MINUTES * 60:
        return False
    _daily_attempts[key_time] = time.time()
    return True


async def morning_text() -> str:
    now = datetime.now(config.TZ)
    end_of_day = now.replace(hour=23, minute=59, second=59).timestamp()
    tasks = await db.open_tasks()
    overdue = [t for t in tasks if t["due_ts"] and t["due_ts"] < now.timestamp()]
    today = [t for t in tasks if t["due_ts"] and now.timestamp() <= t["due_ts"] <= end_of_day]
    nodate = [t for t in tasks if not t["due_ts"]]
    from . import skills
    lines = [f"☀️ Доброе утро! План на {now:%d.%m}"]
    try:
        weather = await skills.weather_text()
        if weather:
            lines.append(weather)
    except Exception:
        log.exception("Погода недоступна")
    bdays = await skills.birthdays_text(now)
    if bdays:
        lines += [""] + bdays
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
        lines.append("Ответить — кнопками ниже.")
    shopping = await db.shopping()
    if shopping:
        items = ", ".join(i["item"] for i in shopping[:10]) + ("…" if len(shopping) > 10 else "")
        lines += ["", f"🛒 Купить ({len(shopping)}): {items}  /list"]
    return "\n".join(lines)


async def morning_kb():
    waiting = await waiting_list(1)
    return chats_kb([{"chat_id": r["chat_id"], "chat_title": r["chat_title"], "last_from_owner": 0}
                     for r, _ in waiting])


async def _morning(bot: Bot):
    if not await _daily_due("morning_time", "last_morning_date"):
        return
    try:
        text, markup = await morning_text(), await morning_kb()
    except Exception:
        log.exception("Не удалось собрать утренний план")
        return
    if await notify(bot, text, markup):
        await db.set("last_morning_date", _today_str())


async def _digest(bot: Bot):
    if not await _daily_due("digest_time", "last_digest_date"):
        return
    now = time.time()
    since = float(await db.get("last_digest_ts") or 0) or now - 86400
    try:
        text = await build_summary(since, "время с прошлой сводки")
    except Exception:
        log.exception("Не удалось собрать вечернюю сводку")
        return
    markup = chats_kb(await db.chats_since(since)) if text else None
    if await notify(bot, "🗞 Сводка за день\n\n" + (text or "Новых сообщений не было."),
                    markup):
        await db.set("last_digest_ts", str(now))
        await db.set("last_digest_date", _today_str())


async def end_away(bot: Bot, reason: str = "закончился"):
    a = await state.away()
    if not a:
        return
    started = float(a["id"] or 0) or time.time() - 86400
    await db.set("away_until", "")
    await db.set("away_note", "")
    text = await build_summary(started, "время, пока вас не было")
    await notify(bot, f"👋 Режим «нет на связи» {reason}. С возвращением!\n\n"
                      + (text or "Пока вас не было, никто не писал."),
                 chats_kb(await db.chats_since(started)) if text else None)


async def _away_end(bot: Bot):
    a = await state.away()
    if a and time.time() >= a["until"]:
        await end_away(bot)


async def _retention(bot: Bot):
    """Раз в сутки удаляем переписку старше RETENTION_DAYS (0 — не удалять)."""
    days = config.RETENTION_DAYS
    if days <= 0:
        return
    last = float(await db.get("last_cleanup_ts") or 0)
    if time.time() - last < 86400:
        return
    counts = await db.cleanup(time.time() - days * 86400)
    await db.set("last_cleanup_ts", str(time.time()))
    if any(counts.values()):
        log.info("Ретеншен (%s дн.): %s", days, counts)


async def run(bot: Bot):
    while True:
        for job in (_reminders, _away_end, _unanswered, _morning, _digest, _retention):
            try:
                await job(bot)
            except Exception:
                log.exception("Ошибка в фоновой задаче %s", job.__name__)
        await asyncio.sleep(30)
