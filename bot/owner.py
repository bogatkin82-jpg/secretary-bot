"""Ваш личный чат с ботом: команды, настройки, черновики, задачи."""
import logging
import re
import time
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from . import ai, config, state
from .business import send_reply
from .db import db
from .jobs import build_summary
from .util import MODE_NAMES, fmt_ts, kb, notify, task_kb

log = logging.getLogger(__name__)
router = Router(name="owner")
router.message.filter(F.chat.type == "private")

_editing: dict[int, int] = {}   # владелец -> id черновика, который он сейчас переписывает
_retiming: dict[int, int] = {}  # владелец -> id задачи, которой он сейчас меняет время

HELP = """Я ваш секретарь в Telegram.

Что я делаю в ваших личных чатах:
• сообщаю о важном 🔴 и расшифровываю голосовые 🎤
• выписываю задачи и встречи, напоминаю о сроках ⏰
• напоминаю, если кто-то давно ждёт вашего ответа ⏳
• пока вас нет, готовлю ответ (или отвечаю сам)
• утром присылаю план дня ☀️, вечером — сводку 🗞

Просто напишите мне:
«завтра в 10 позвонить в банк» — запишу задачу
«что Петя говорил про отчёт?» — поищу в переписке (вопрос со знаком ?)

Ответы
/settings — режим: выкл / черновик / авто
/persona текст — как отвечать от вашего имени
/faq текст — факт для ответов (цены, адрес…), /faq — список
/idle 15 — отвечать, если вы молчите в чате > N минут
/away до 5.11 в отпуске — режим «нет на связи», /back — я вернулся
/night 23:00-08:00 | off — ночью отвечать самому

Задачи
/tasks — список с кнопками, /time N когда — перенести
/done N — закрыть, /del N — удалить

Контакты
/vip имя — важный, /ignore имя — не трогать, /normal имя — обычный
/contacts — кто важный и кого не трогать

Сводки и напоминания
/summary [часы] — сводка сейчас, /plan — план дня сейчас
/digest 21:00 | off, /morning 09:00 | off — время сводки и плана
/wait 3 | off — напоминать о неотвеченных через N часов
/ask вопрос — поиск по переписке
/voice on | off — расшифровка голосовых"""


async def _is_owner(m: Message) -> bool:
    if m.from_user and m.from_user.id == config.OWNER_ID:
        return True
    await m.answer(f"Это приватный бот. Ваш Telegram ID: {m.from_user.id}")
    return False


async def _status() -> str:
    lines = [f"Режим ответов: {MODE_NAMES.get(await db.get('mode'))}"]
    a = await state.away()
    if a:
        lines.append(f"🏝 Нет на связи до {fmt_ts(a['until'])}: {a['note']}")
    night = await db.get("night")
    if night != "off":
        lines.append(f"🌙 Ночью ({night}) отвечаю сам")
    lines += [
        f"Отвечаю, если вы молчите в чате > {await db.get('idle_minutes')} мин",
        f"Утренний план: {await db.get('morning_time')}, вечерняя сводка: {await db.get('digest_time')}",
        f"Напоминать о неотвеченных: " + (
            "выкл" if await db.get("wait_hours") == "off" else f"через {await db.get('wait_hours')} ч"),
        f"Голосовые: {'расшифровываю' if await db.get('voice') == 'on' else 'выкл'}",
        f"Фактов для ответов: {len(await db.facts())} (/faq)",
        f"Как отвечать: {await db.get('persona')}",
    ]
    return "\n".join(lines)


def _settings_kb():
    return kb([("🔕 Выкл", "mode:off"), ("📝 Черновик", "mode:draft"), ("🤖 Авто", "mode:auto")],
              [("🗞 Сводка сейчас", "summary:24")])


@router.message(Command("start", "help"))
async def cmd_start(m: Message):
    if not await _is_owner(m):
        return
    await m.answer(HELP + "\n\n" + await _status())


@router.message(Command("settings"))
async def cmd_settings(m: Message):
    if await _is_owner(m):
        await m.answer(await _status(), reply_markup=_settings_kb())


@router.message(Command("mode"))
async def cmd_mode(m: Message, command: CommandObject):
    if not await _is_owner(m):
        return
    mode = (command.args or "").strip().lower()
    if mode not in MODE_NAMES:
        return await m.answer("Выберите режим:", reply_markup=_settings_kb())
    await db.set("mode", mode)
    await m.answer(f"Режим ответов: {MODE_NAMES[mode]}")


@router.message(Command("idle"))
async def cmd_idle(m: Message, command: CommandObject):
    if not await _is_owner(m):
        return
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Пример: /idle 15")
    await db.set("idle_minutes", command.args.strip())
    await m.answer(f"Буду отвечать, только если вы молчите в чате дольше {command.args.strip()} мин.")


@router.message(Command("persona"))
async def cmd_persona(m: Message, command: CommandObject):
    if not await _is_owner(m):
        return
    if not command.args:
        return await m.answer(f"Сейчас: {await db.get('persona')}\n\n"
                              "Изменить: /persona Я Алексей, дизайнер. Пиши на «ты», коротко, "
                              "про работу скажи, что отвечу вечером.")
    await db.set("persona", command.args.strip())
    await m.answer("Запомнил, буду отвечать так.")


@router.message(Command("digest"))
async def cmd_digest(m: Message, command: CommandObject):
    if not await _is_owner(m):
        return
    arg = (command.args or "").strip().lower()
    if arg != "off" and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", arg):
        return await m.answer("Пример: /digest 21:00 или /digest off")
    if arg != "off":
        hh, mm = arg.split(":")
        arg = f"{int(hh):02d}:{mm}"
    await db.set("digest_time", arg)
    await m.answer("Ежедневная сводка выключена." if arg == "off" else f"Сводка каждый день в {arg}.")


async def _send_summary(bot: Bot, hours: float):
    await notify(bot, "Собираю сводку…")
    text = await build_summary(time.time() - hours * 3600, f"последние {hours:g} ч")
    await notify(bot, f"🗞 Сводка за {hours:g} ч\n\n" + (text or "Новых сообщений не было."))


@router.message(Command("summary"))
async def cmd_summary(m: Message, command: CommandObject, bot: Bot):
    if not await _is_owner(m):
        return
    try:
        hours = float((command.args or "24").replace(",", "."))
    except ValueError:
        return await m.answer("Пример: /summary 8")
    await _send_summary(bot, hours)


@router.message(Command("tasks"))
async def cmd_tasks(m: Message):
    if not await _is_owner(m):
        return
    tasks = await db.open_tasks()
    if not tasks:
        return await m.answer("Открытых задач нет 🎉")
    text, markup = await _render_task_list()
    await m.answer(text, reply_markup=markup)


def _task_ids(command: CommandObject) -> list[int]:
    return [int(i) for i in re.findall(r"\d+", command.args or "")]


@router.message(Command("done"))
async def cmd_done(m: Message, command: CommandObject):
    if not await _is_owner(m):
        return
    ids = _task_ids(command)
    if not ids:
        return await m.answer("Пример: /done 3")
    for i in ids:
        await db.set_task(i, done=1)
    await m.answer("Готово ✅")


@router.message(Command("del"))
async def cmd_del(m: Message, command: CommandObject):
    if not await _is_owner(m):
        return
    ids = _task_ids(command)
    if not ids:
        return await m.answer("Пример: /del 3")
    for i in ids:
        await db.delete_task(i)
    await m.answer("Удалил 🗑")


async def _retime(m: Message, task_id: int, when: str):
    t = await db.get_task(task_id)
    if not t:
        return await m.answer(f"Задачи {task_id} нет. Список: /tasks")
    try:
        due = await ai.parse_when(when)
    except Exception:
        log.exception("parse_when")
        due = None
    if due is None:
        return await m.answer("Не понял время 🤔 Напишите, например: «завтра в 15:00», «в пятницу в 12», "
                              "«через 2 часа» или «30.10 18:00».")
    await db.set_task(task_id, due_ts=due, reminded=0)
    note = "\n⚠️ Это время уже прошло — напомню сразу." if due < time.time() else ""
    await m.answer(f"🕒 {t['text']}\nНапомню {fmt_ts(due)}{note}", reply_markup=task_kb(task_id))


@router.message(Command("time"))
async def cmd_time(m: Message, command: CommandObject):
    if not await _is_owner(m):
        return
    match = re.match(r"\s*(\d+)\s+(.+)", command.args or "", re.S)
    if not match:
        return await m.answer("Пример: /time 2 завтра в 15:00")
    await _retime(m, int(match.group(1)), match.group(2))


def _task_id_from_reply(m: Message) -> int | None:
    """Если владелец ответил на сообщение бота с задачей — достаём её id из кнопок."""
    r = m.reply_to_message
    if not r or not r.reply_markup:
        return None
    for row in r.reply_markup.inline_keyboard:
        for b in row:
            if b.callback_data and b.callback_data.startswith("task:done:"):
                return int(b.callback_data.rsplit(":", 1)[1])
    return None


@router.message(F.text & ~F.text.startswith("/"))
async def free_text(m: Message, bot: Bot):
    if not await _is_owner(m):
        return
    # 1) владелец переписывает черновик
    draft_id = _editing.pop(m.from_user.id, None)
    if draft_id:
        d = await db.get_draft(draft_id)
        if d and d["status"] == "pending":
            await send_reply(bot, d["conn_id"], d["chat_id"], d["chat_title"], m.text)
            await db.set_draft_status(draft_id, "sent")
            if m.text.strip() != (d["text"] or "").strip():
                await db.add_style_example(d["text"], m.text)   # учимся на ваших правках
            return await m.answer(f"Отправил «{d['chat_title']}» ✅")
    # 2) владелец меняет время задачи (после кнопки 🕒 или ответом на сообщение с задачей)
    task_id = _retiming.pop(m.from_user.id, None) or _task_id_from_reply(m)
    if task_id:
        return await _retime(m, task_id, m.text)
    # 3) вопрос — ищем в переписке
    if m.text.strip().endswith("?"):
        from .extras import answer_search
        return await answer_search(m, m.text.strip())
    # 4) иначе — это новая задача
    try:
        data = await ai.parse_task(m.text)
    except Exception:
        log.exception("parse_task")
        data = {"text": m.text, "due": None}
    due = ai.parse_due(data.get("due"))
    task_id = await db.add_task(None, None, data.get("text") or m.text, due)
    await m.answer(f"📝 Записал: {data.get('text') or m.text}\n🕒 {fmt_ts(due)}",
                   reply_markup=task_kb(task_id))


# ---------- кнопки ----------

@router.callback_query(F.from_user.id != config.OWNER_ID)
async def cb_foreign(c: CallbackQuery):
    await c.answer("Это приватный бот", show_alert=True)


@router.callback_query(F.data.startswith("mode:"))
async def cb_mode(c: CallbackQuery):
    mode = c.data.split(":")[1]
    await db.set("mode", mode)
    await c.message.edit_text(await _status(), reply_markup=_settings_kb())
    await c.answer(f"Режим: {MODE_NAMES[mode]}")


@router.callback_query(F.data.startswith("summary:"))
async def cb_summary(c: CallbackQuery, bot: Bot):
    await c.answer()
    await _send_summary(bot, float(c.data.split(":")[1]))


@router.callback_query(F.data.startswith("draft:"))
async def cb_draft(c: CallbackQuery, bot: Bot):
    _, action, draft_id = c.data.split(":")
    d = await db.get_draft(int(draft_id))
    if not d or d["status"] != "pending":
        await c.answer("Черновик уже неактуален (вы ответили сами или пришло новое сообщение)",
                       show_alert=True)
        return await c.message.edit_reply_markup(reply_markup=None)
    if action == "send":
        await send_reply(bot, d["conn_id"], d["chat_id"], d["chat_title"], d["text"])
        await db.set_draft_status(d["id"], "sent")
        await c.message.edit_text(c.message.text + "\n\n✅ Отправлено")
    elif action == "skip":
        await db.set_draft_status(d["id"], "skipped")
        await c.message.edit_text(c.message.text + "\n\n❌ Без ответа")
    elif action == "edit":
        _retiming.pop(c.from_user.id, None)
        _editing[c.from_user.id] = d["id"]
        await c.message.answer("Пришлите текст ответа — отправлю его от вашего имени.")
    await c.answer()


async def _render_task_list():
    tasks = (await db.open_tasks())[:20]
    if not tasks:
        return "Открытых задач нет 🎉", None
    lines = [f"{t['id']}. {t['text']} — {fmt_ts(t['due_ts'])}"
             + (f" ({t['chat_title']})" if t["chat_title"] else "") for t in tasks]
    buttons = [[(f"✅ {t['id']}", f"task:done:{t['id']}"), (f"🕒 {t['id']}", f"task:time:{t['id']}"),
                (f"🗑 {t['id']}", f"task:del:{t['id']}")] for t in tasks]
    return ("📝 Открытые задачи:\n" + "\n".join(lines) +
            "\n\n✅ — сделано, 🕒 — другое время, 🗑 — удалить"), kb(*buttons)


@router.callback_query(F.data.startswith("task:"))
async def cb_task(c: CallbackQuery):
    _, action, task_id = c.data.split(":")
    t = await db.get_task(int(task_id))
    if not t:
        return await c.answer("Задача не найдена")
    is_list = (c.message.text or "").startswith("📝 Открытые задачи")

    if action == "time":
        _editing.pop(c.from_user.id, None)
        _retiming[c.from_user.id] = t["id"]
        await c.message.answer(f"🕒 «{t['text']}»\nКогда напомнить? Например: «завтра в 15:00», "
                               "«в пятницу в 12», «через 2 часа», «30.10 18:00».")
        return await c.answer()

    if action in ("done", "del"):
        if action == "done":
            await db.set_task(t["id"], done=1)
        else:
            await db.delete_task(t["id"])
        mark = "✅ Сделано" if action == "done" else "🗑 Удалено"
        if is_list:
            text, markup = await _render_task_list()
            await c.message.edit_text(text, reply_markup=markup)
            return await c.answer(f"{mark}: {t['text']}")
        await c.message.edit_text(c.message.text + f"\n\n{mark}")
    else:
        if action == "snooze":
            due = time.time() + 3600
        else:  # tomorrow — завтра в 10:00
            tomorrow = datetime.now(config.TZ) + timedelta(days=1)
            due = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0).timestamp()
        await db.set_task(t["id"], due_ts=due, reminded=0)
        await c.message.edit_text(c.message.text + f"\n\n⏰ Напомню {fmt_ts(due)}")
    await c.answer()
