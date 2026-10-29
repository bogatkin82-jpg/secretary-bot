"""Обработка ваших личных чатов через Telegram Business."""
import asyncio
import logging
import time

from aiogram import Bot, Router
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from . import ai, config, state, voice
from .db import db
from .util import (chat_title, contact_kb, draft_kb, event_kb, fmt_ts, message_text, notify,
                   task_kb)

log = logging.getLogger(__name__)
router = Router(name="business")

_waiting: dict[int, asyncio.Task] = {}        # отложенный разбор чата (debounce)
_locks: dict[int, asyncio.Lock] = {}          # не разбирать один чат параллельно

PENDING_VOICE = 2   # messages.processed: голосовое ещё расшифровывается


async def _owner_conn(conn_id: str, bot: Bot):
    """Подключение и проверка, что это ваш аккаунт, а не чужой."""
    row = await db.get_connection(conn_id)
    if row is None:
        bc = await bot.get_business_connection(conn_id)
        await _save_conn(bc)
        row = await db.get_connection(conn_id)
    if row["user_id"] != config.OWNER_ID or not row["enabled"]:
        return None
    return row


async def _save_conn(bc: BusinessConnection):
    can_reply = bc.rights.can_reply if bc.rights else bool(bc.can_reply)
    await db.save_connection(bc.id, bc.user.id, bc.is_enabled, can_reply)
    return can_reply


@router.business_connection()
async def on_connection(bc: BusinessConnection, bot: Bot):
    can_reply = await _save_conn(bc)
    if bc.user.id != config.OWNER_ID:
        log.warning("Чужой аккаунт %s подключил бота — игнорирую", bc.user.id)
        return
    await db.set("owner_name", bc.user.full_name)
    if bc.is_enabled:
        can_read = bc.rights.can_read_messages if bc.rights else False
        lines = ["✅ Бот подключён к вашему аккаунту.",
                 "Ответ в чатах: " + ("разрешён" if can_reply else "ЗАПРЕЩЁН — включите «Отвечать на сообщения» в настройках бота в Telegram для бизнеса")]
        if bc.rights is not None:
            lines.append("Отметка «прочитано»: " + ("разрешена" if can_read else "не разрешена (не страшно)"))
        await notify(bot, "\n".join(lines))
    else:
        await notify(bot, "⏸ Бот отключён от вашего аккаунта.")


@router.business_message()
async def on_message(m: Message, bot: Bot):
    conn = await _owner_conn(m.business_connection_id, bot)
    if conn is None:
        return
    title = chat_title(m)
    await db.upsert_contact(m.chat.id, m.chat.full_name or title, m.chat.username)
    if await db.contact_status(m.chat.id) == "ignore":
        return                                   # «не трогать»: ничего не читаем и не храним

    from_owner = m.from_user is not None and m.from_user.id == config.OWNER_ID
    by_bot = m.sender_business_bot is not None
    media = voice.media_of(m)
    want_voice = (media is not None and not by_bot and voice.available()
                  and await db.get("voice") == "on")
    text = message_text(m)
    if media:
        text = f"[{media[2]} {voice.fmt_duration(media[1])}] {m.caption or ''}".strip()
    added = await db.add_message(
        conn_id=m.business_connection_id, chat_id=m.chat.id, msg_id=m.message_id,
        chat_title=title, sender=m.from_user.full_name if m.from_user else "",
        from_owner=from_owner or by_bot, by_bot=by_bot, text=text,
        ts=m.date.timestamp(), processed=1 if by_bot else (PENDING_VOICE if want_voice else 0),
    )
    if not added or by_bot:
        return
    if from_owner:
        await db.expire_drafts(m.chat.id)
    if want_voice:
        asyncio.create_task(_transcribe_then_schedule(m, media, title, from_owner, bot))
    else:
        schedule(m.chat.id, m.business_connection_id, bot)


async def _transcribe_then_schedule(m: Message, media, title: str, from_owner: bool, bot: Bot):
    file_id, duration, kind = media
    label = f"[{kind} {voice.fmt_duration(duration)}]"
    try:
        transcript = await voice.transcribe(bot, file_id)
        await db.update_message_text(m.chat.id, m.message_id, f"{label} {transcript}")
        if not from_owner:
            await notify(bot, f"🎤 {title} ({kind}, {voice.fmt_duration(duration)}):\n{transcript}")
    except Exception as e:
        log.exception("Не удалось расшифровать %s", kind)
        await _report_error(bot, e, what=f"расшифровать {kind}")
    finally:
        await db.mark_pending_voice(m.chat.id, m.message_id, 0)
        schedule(m.chat.id, m.business_connection_id, bot)


@router.edited_business_message()
async def on_edit(m: Message):
    await db.update_message_text(m.chat.id, m.message_id, message_text(m))


@router.deleted_business_messages()
async def on_delete(_: BusinessMessagesDeleted):
    pass  # удалённые сообщения оставляем в истории как есть


# ---------- разбор чата ----------

def schedule(chat_id: int, conn_id: str, bot: Bot):
    task = _waiting.get(chat_id)
    if task and not task.done():
        task.cancel()
    _waiting[chat_id] = asyncio.create_task(_delayed(chat_id, conn_id, bot))


RETRY_DELAYS = (60, 180, 600)   # если нейросеть не ответила — пробуем ещё раз позже


async def _delayed(chat_id: int, conn_id: str, bot: Bot, attempt: int = 0):
    try:
        await asyncio.sleep(config.DEBOUNCE_SECONDS if attempt == 0 else RETRY_DELAYS[attempt - 1])
    except asyncio.CancelledError:
        return
    if attempt == 0:
        _waiting.pop(chat_id, None)  # дальше не отменяем — идёт работа
    lock = _locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        try:
            await process_chat(chat_id, conn_id, bot)
        except Exception as e:
            log.exception("Ошибка при разборе чата %s (попытка %s)", chat_id, attempt + 1)
            if attempt < len(RETRY_DELAYS):
                asyncio.create_task(_delayed(chat_id, conn_id, bot, attempt + 1))
            else:
                await _report_error(bot, e)


_last_error_ts = 0.0


async def _report_error(bot: Bot, e: Exception, what: str = "разобрать сообщение"):
    """Сообщить владельцу об ошибке нейросети — не чаще раза в час."""
    global _last_error_ts
    if time.time() - _last_error_ts > 3600:
        _last_error_ts = time.time()
        await notify(bot, f"⚠️ Не получилось {what}: {e}\n"
                          "Сообщения сохранены — разберу их вместе со следующим сообщением в этом чате.")


async def _should_reply(chat_id: int, new_rows, conn, mode: str, away) -> bool:
    if mode == "off" or not conn["can_reply"]:
        return False
    if new_rows[-1]["from_owner"]:          # последнее слово за вами
        return False
    if mode == "away":                       # «нет на связи»: один раз каждому за период
        return not await db.away_replied_to(chat_id, away["id"])
    idle = int(await db.get("idle_minutes")) * 60
    now = time.time()
    if now - await db.last_owner_activity(chat_id) < idle:
        return False                         # вы недавно были в этом чате
    if await db.bot_replies_since(chat_id, now - 3600) >= config.MAX_AUTOREPLIES_PER_HOUR:
        return False
    return True


async def process_chat(chat_id: int, conn_id: str, bot: Bot):
    new_rows = await db.unprocessed(chat_id)
    if not new_rows:
        return
    conn = await db.get_connection(conn_id)
    title = new_rows[-1]["chat_title"]
    status = await db.contact_status(chat_id)
    mode = await state.effective_mode()
    away = await state.away() if mode == "away" else None
    want_reply = await _should_reply(chat_id, new_rows, conn, mode, away)

    result = await ai.analyze_chat(
        chat_title=title,
        history_rows=await db.history(chat_id, config.HISTORY_LIMIT),
        new_ids={r["id"] for r in new_rows},
        want_reply=want_reply,
        persona=await db.get("persona"),
        owner_name=await db.get("owner_name"),
        open_tasks=[t["text"] for t in await db.open_tasks()],
        facts=[f["text"] for f in await db.facts()],
        style=await db.style_examples(),
        away_note=away["note"] if away else "",
    )
    importance = result["importance"]
    if status == "vip" and any(not r["from_owner"] for r in new_rows):
        importance = "high"                  # важный контакт — всегда важно
    await db.mark_processed([r["id"] for r in new_rows], importance)
    log.info("Чат %s: %s", title, result)

    # 1) задачи
    for t in result["tasks"]:
        due = ai.parse_due(t.get("due"))
        task_id = await db.add_task(chat_id, title, t["text"], due)
        await notify(bot, f"📝 Задача из чата «{title}»:\n{t['text']}\n🕒 {fmt_ts(due)}", task_kb(task_id))

    # 2) встречи — только настоящие договорённости (модели любят выдумывать время)
    for e in _real_events(result["events"], new_rows):
        ev_id = await db.add_event(chat_id, title, e["title"], e["start"], e["end"], e["place"])
        if ev_id is None:
            continue   # эту встречу уже присылали
        place = f"\n📍 {e['place']}" if e["place"] else ""
        await notify(bot, f"📅 Встреча из чата «{title}»:\n{e['title']}\n🕒 {fmt_ts(e['start'])}{place}",
                     event_kb(ev_id))

    # 3) важное
    if importance == "high":
        star = "⭐ " if status == "vip" else ""
        await notify(bot, f"🔴 {star}Важное от «{title}»:\n{result['summary'] or new_rows[-1]['text']}",
                     contact_kb(chat_id, status))

    # 4) ответ
    reply = result["reply"]
    if not reply:
        return
    if mode in ("auto", "away"):
        await send_reply(bot, conn_id, chat_id, title, reply)
        if mode == "away":
            await db.set_away_replied(chat_id, away["id"])
        await notify(bot, f"🤖 Ответил «{title}»:\n{reply}")
    elif mode == "draft":
        await db.expire_drafts(chat_id)  # старый черновик в этом чате устарел
        draft_id = await db.add_draft(conn_id, chat_id, title, reply)
        await notify(bot, f"💬 «{title}»: {result['summary']}\n\nЧерновик ответа:\n{reply}",
                     draft_kb(draft_id))


def _real_events(events: list[dict], new_rows) -> list[dict]:
    """Отсекаем «встречи», которые на деле лишь предложения или с выдуманным временем."""
    now = time.time()
    msg_times = [r["ts"] for r in new_rows]
    out = []
    for e in events:
        if e["start"] < now - 300:
            continue                      # в прошлом
        if any(abs(e["start"] - t) < 180 for t in msg_times):
            continue                      # модель подставила время самого сообщения
        out.append(e)
    return out


async def send_reply(bot: Bot, conn_id: str, chat_id: int, title: str, text: str):
    sent = await bot.send_message(chat_id, text, business_connection_id=conn_id, parse_mode=None)
    await db.add_message(
        conn_id=conn_id, chat_id=chat_id, msg_id=sent.message_id, chat_title=title,
        sender="бот", from_owner=1, by_bot=1, text=text, ts=time.time(), processed=1,
    )
