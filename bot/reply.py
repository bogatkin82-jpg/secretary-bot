"""Ответить собеседнику прямо из бота: из сводки, уведомления о важном или «ждёт ответа».

Кнопки:
  ✍️ Ответить  — вы пишете текст, бот отправляет его в чат от вашего имени;
  🤖 Предложи  — нейросеть готовит черновик, вы отправляете или правите.
Можно и просто ответить свайпом на уведомление с такими кнопками — текст уйдёт собеседнику.
"""
import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from . import ai, config
from .db import db
from .util import chats_kb, draft_kb, kb, reply_row  # noqa: F401

log = logging.getLogger(__name__)
router = Router(name="reply")
router.callback_query.filter(F.from_user.id == config.OWNER_ID)

writing: dict[int, int] = {}      # владелец -> chat_id, кому он сейчас пишет ответ


def chat_id_from_reply(m: Message) -> int | None:
    """Владелец ответил свайпом на уведомление с кнопками «ответить» — в какой чат писать."""
    r = m.reply_to_message
    if not r or not r.reply_markup:
        return None
    for row in r.reply_markup.inline_keyboard:
        for b in row:
            d = b.callback_data or ""
            if d.startswith(("rp:write:", "rp:open:", "rp:ai:")):
                return int(d.rsplit(":", 1)[1])
    return None


async def send_owner_text(m: Message, bot: Bot, chat_id: int):
    from .business import send_reply
    info = await db.chat_info(chat_id)
    if not info:
        return await m.answer("Не нашёл этот чат 🤔")
    try:
        await send_reply(bot, info["conn_id"], chat_id, info["chat_title"], m.text, kind="owner")
    except Exception as e:
        log.exception("send owner text")
        return await m.answer(f"⚠️ Не получилось отправить: {e}")
    await m.answer(f"Отправил «{info['chat_title']}» ✅")


def _fmt_last(rows) -> str:
    lines = []
    for r in rows:
        t = datetime.fromtimestamp(r["ts"], config.TZ).strftime("%d.%m %H:%M")
        who = "Вы" if r["from_owner"] else "Он(а)"
        lines.append(f"{t} {who}: {(r['text'] or '')[:300]}")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("rp:"))
async def cb_reply(c: CallbackQuery, bot: Bot):
    _, action, chat_id = c.data.split(":")
    chat_id = int(chat_id)
    info = await db.chat_info(chat_id)
    if not info:
        return await c.answer("Чат не найден", show_alert=True)
    title = info["chat_title"]

    if action == "open":
        last = (await db.history(chat_id, 6))
        await c.message.answer(f"💬 {title}\n\n{_fmt_last(last)}", reply_markup=kb(reply_row(chat_id)))
        return await c.answer()

    if action == "write":
        from . import owner
        owner._editing.pop(c.from_user.id, None)
        owner._retiming.pop(c.from_user.id, None)
        writing[c.from_user.id] = chat_id
        await c.message.answer(f"✍️ Напишите ответ для «{title}» — отправлю от вашего имени.\n"
                               "Передумали — напишите «отмена».")
        return await c.answer()

    if action == "ai":
        await c.answer("Готовлю черновик…")
        try:
            draft = await suggest(chat_id, title)
        except Exception as e:
            log.exception("suggest")
            return await c.message.answer(f"⚠️ Не получилось подготовить ответ: {e}")
        if not draft:
            return await c.message.answer(f"Похоже, «{title}» ответ не нужен. Если всё же хотите ответить — "
                                          "нажмите «✍️ Ответить».", reply_markup=kb(reply_row(chat_id)))
        await db.expire_drafts(chat_id)
        draft_id = await db.add_draft(info["conn_id"], chat_id, title, draft)
        await c.message.answer(f"💬 «{title}»\n\nЧерновик ответа:\n{draft}", reply_markup=draft_kb(draft_id))


async def suggest(chat_id: int, title: str) -> str | None:
    """Черновик ответа по последней переписке — независимо от режима и паузы."""
    history = await db.history(chat_id, config.HISTORY_LIMIT)
    last_contact = [r["id"] for r in history if not r["from_owner"]][-3:]
    result = await ai.analyze_chat(
        chat_title=title, history_rows=history, new_ids=set(last_contact), want_reply=True,
        persona=await db.get("persona"), owner_name=await db.get("owner_name"),
        open_tasks=[], facts=[f["text"] for f in await db.facts()],
        style=await db.style_examples(), owner_samples=await db.owner_samples(chat_id),
    )
    return result["reply"]
