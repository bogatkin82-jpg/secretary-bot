import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import config

log = logging.getLogger(__name__)

MODE_NAMES = {
    "off": "выключены",
    "draft": "черновик — пришлю вам на одобрение",
    "auto": "отвечаю сам",
}


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "без срока"
    return datetime.fromtimestamp(ts, config.TZ).strftime("%d.%m %H:%M")


def message_text(m: Message) -> str:
    """Текст сообщения или краткое описание вложения."""
    body = m.text or m.caption or ""
    kind = None
    if m.photo:
        kind = "фото"
    elif m.video:
        kind = "видео"
    elif m.video_note:
        kind = "видеосообщение"
    elif m.voice:
        kind = "голосовое сообщение"
    elif m.audio:
        kind = "аудио"
    elif m.document:
        kind = f"файл {m.document.file_name or ''}".strip()
    elif m.sticker:
        kind = f"стикер {m.sticker.emoji or ''}".strip()
    elif m.animation:
        kind = "GIF"
    elif m.location:
        kind = "геопозиция"
    elif m.contact:
        kind = "контакт"
    elif m.poll:
        kind = f"опрос: {m.poll.question}"
    if kind:
        return f"[{kind}] {body}".strip()
    return body or "[сообщение]"


def chat_title(m: Message) -> str:
    c = m.chat
    name = c.full_name or c.title or str(c.id)
    return f"{name} (@{c.username})" if c.username else name


async def notify(bot: Bot, text: str, kb: InlineKeyboardMarkup | None = None):
    try:
        await bot.send_message(config.OWNER_ID, text, reply_markup=kb, parse_mode=None)
    except Exception as e:  # владелец ещё не нажал /start у бота и т.п.
        log.error("Не смог написать владельцу: %s", e)


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def task_kb(task_id: int) -> InlineKeyboardMarkup:
    return kb([("✅ Сделано", f"task:done:{task_id}"), ("🕒 Другое время", f"task:time:{task_id}")],
              [("⏰ +1 час", f"task:snooze:{task_id}"), ("🌅 Завтра", f"task:tomorrow:{task_id}"),
               ("🗑", f"task:del:{task_id}")])


def draft_kb(draft_id: int) -> InlineKeyboardMarkup:
    return kb([("✅ Отправить", f"draft:send:{draft_id}"), ("✏️ Изменить", f"draft:edit:{draft_id}")],
              [("❌ Не отвечать", f"draft:skip:{draft_id}")])


def contact_kb(chat_id: int, status: str = "") -> InlineKeyboardMarkup:
    if status == "vip":
        return kb([("☆ Убрать из важных", f"ct:normal:{chat_id}"), ("🚫 Не трогать", f"ct:ignore:{chat_id}")])
    return kb([("⭐ Важный контакт", f"ct:vip:{chat_id}"), ("🚫 Не трогать", f"ct:ignore:{chat_id}")])


def event_kb(event_id: int) -> InlineKeyboardMarkup:
    return kb([("📅 Добавить в календарь", f"ev:ics:{event_id}")])


def make_ics(event_id: int, title: str, start_ts: float, end_ts: float | None,
             place: str | None, description: str = "") -> bytes:
    """Файл .ics — телефон откроет его и предложит добавить встречу в календарь."""
    def ts(t: float) -> str:
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def esc(s: str) -> str:
        for a, b in (("\\", "\\\\"), (";", r"\;"), (",", r"\,"), ("\n", r"\n")):
            s = s.replace(a, b)
        return s

    end_ts = end_ts if end_ts and end_ts > start_ts else start_ts + 3600
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//secretary-bot//RU", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:secretary-{event_id}-{int(start_ts)}@secretary-bot",
        f"DTSTAMP:{ts(datetime.now().timestamp())}",
        f"DTSTART:{ts(start_ts)}", f"DTEND:{ts(end_ts)}",
        f"SUMMARY:{esc(title)}",
    ]
    if place:
        lines.append(f"LOCATION:{esc(place)}")
    if description:
        lines.append(f"DESCRIPTION:{esc(description)}")
    lines += ["BEGIN:VALARM", "TRIGGER:-PT30M", "ACTION:DISPLAY", f"DESCRIPTION:{esc(title)}",
              "END:VALARM", "END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(lines) + "\r\n").encode()
