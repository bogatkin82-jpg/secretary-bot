"""Дополнительные возможности: факты для ответов, контакты, «нет на связи», ночь,
неотвеченные, утренний план, поиск по переписке, голосовые, встречи в календарь."""
import logging
import re
import time
from collections import OrderedDict

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from . import ai, config, jobs, state, voice
from .db import db
from .util import fmt_ts, kb, make_ics, notify

log = logging.getLogger(__name__)
router = Router(name="extras")
router.message.filter(F.chat.type == "private", F.from_user.id == config.OWNER_ID)
router.callback_query.filter(F.from_user.id == config.OWNER_ID)

TIME_RE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


# ---------- /faq: готовые ответы ----------

def _faq_view(facts):
    if not facts:
        return ("Фактов пока нет.\n\nДобавьте то, о чём вас часто спрашивают, — я буду отвечать точно:\n"
                "/faq Стрижка стоит 1500 ₽, борода — 700 ₽\n"
                "/faq Адрес: ул. Ленина, 5, вход со двора\n"
                "/faq Работаю пн–пт с 10 до 19"), None
    text = "📚 Факты для ответов:\n" + "\n".join(f"{f['id']}. {f['text']}" for f in facts)
    text += "\n\nДобавить: /faq текст. Удалить — кнопкой 🗑."
    return text, kb(*[[(f"🗑 {f['id']}", f"faq:del:{f['id']}")] for f in facts[:30]])


@router.message(Command("faq"))
async def cmd_faq(m: Message, command: CommandObject):
    if command.args and command.args.strip():
        fid = await db.add_fact(command.args.strip())
        return await m.answer(f"📚 Запомнил (№{fid}). Теперь на такие вопросы буду отвечать по факту.")
    text, markup = _faq_view(await db.facts())
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("faq:del:"))
async def cb_faq_del(c: CallbackQuery):
    await db.delete_fact(int(c.data.rsplit(":", 1)[1]))
    text, markup = _faq_view(await db.facts())
    await c.message.edit_text(text, reply_markup=markup)
    await c.answer("Удалено")


# ---------- контакты: важные / не трогать ----------

STATUS_NAMES = {"vip": "⭐ важный", "ignore": "🚫 не трогать", "": "обычный"}


async def _find_contacts(query: str):
    q = query.strip().lstrip("@").lower()
    rows = await db.all_contacts()
    exact = [r for r in rows if (r["username"] or "").lower() == q]
    if exact:
        return exact
    return [r for r in rows if q and (q in (r["name"] or "").lower() or q in (r["username"] or "").lower())]


async def _set_status(m: Message, command: CommandObject, status: str):
    if not command.args:
        cmd = {"vip": "vip", "ignore": "ignore", "": "normal"}[status]
        return await m.answer(f"Напишите имя или @ник: /{cmd} Петя")
    found = await _find_contacts(command.args)
    if not found:
        return await m.answer("Не нашёл такого собеседника. Я знаю только тех, кто писал вам "
                              "после подключения бота. Можно ещё нажать кнопку ⭐/🚫 под уведомлением.")
    if len(found) > 1:
        return await m.answer("Нашлось несколько — выберите:", reply_markup=kb(*[
            [(f"{r['name']}" + (f" @{r['username']}" if r["username"] else ""),
              f"ct:{status or 'normal'}:{r['chat_id']}")] for r in found[:10]]))
    r = found[0]
    await db.set_contact_status(r["chat_id"], status)
    await m.answer(f"{r['name']} — теперь {STATUS_NAMES[status]}." + _status_hint(status))


def _status_hint(status: str) -> str:
    if status == "vip":
        return "\nЕго сообщения всегда помечаю 🔴, о неотвеченных напомню уже через час."
    if status == "ignore":
        return "\nЕго сообщения не читаю, не храню и не отвечаю."
    return ""


@router.message(Command("vip"))
async def cmd_vip(m: Message, command: CommandObject):
    await _set_status(m, command, "vip")


@router.message(Command("ignore"))
async def cmd_ignore(m: Message, command: CommandObject):
    await _set_status(m, command, "ignore")


@router.message(Command("normal"))
async def cmd_normal(m: Message, command: CommandObject):
    await _set_status(m, command, "")


@router.message(Command("contacts"))
async def cmd_contacts(m: Message):
    rows = [r for r in await db.all_contacts() if r["status"]]
    if not rows:
        return await m.answer("Особых контактов нет.\n/vip имя — важный, /ignore имя — не трогать.")
    lines = [f"{STATUS_NAMES[r['status']]}: {r['name']}" + (f" @{r['username']}" if r["username"] else "")
             for r in rows]
    await m.answer("👥 Контакты:\n" + "\n".join(lines) + "\n\nСбросить: /normal имя")


@router.callback_query(F.data.startswith("ct:"))
async def cb_contact(c: CallbackQuery):
    _, action, chat_id = c.data.split(":")
    status = "" if action == "normal" else action
    await db.set_contact_status(int(chat_id), status)
    row = await db.contact(int(chat_id))
    name = row["name"] if row else "Контакт"
    await c.message.answer(f"{name} — теперь {STATUS_NAMES[status]}." + _status_hint(status))
    await c.answer()


# ---------- «нет на связи» и ночной режим ----------

@router.message(Command("away"))
async def cmd_away(m: Message, command: CommandObject):
    if not command.args:
        a = await state.away()
        if a:
            return await m.answer(f"🏝 Сейчас «нет на связи» до {fmt_ts(a['until'])}: {a['note']}\n/back — выключить")
        return await m.answer("Пример: /away до 5.11 в отпуске\nили: /away до завтра 10:00 на конференции")
    try:
        data = await ai.parse_away(command.args)
    except Exception:
        log.exception("parse_away")
        data = {"until": None, "note": ""}
    if not data["until"] or data["until"] <= time.time():
        return await m.answer("Не понял, до какого времени 🤔 Например: /away до 5.11 в отпуске")
    await db.set("away_until", str(data["until"]))
    await db.set("away_note", data["note"] or f"сейчас не на связи, вернусь {fmt_ts(data['until'])}")
    await db.set("away_id", str(time.time()))
    await m.answer(f"🏝 Режим «нет на связи» до {fmt_ts(data['until'])}.\n"
                   f"Каждому, кто напишет, один раз отвечу: «{await db.get('away_note')}».\n"
                   "Задачи и важное по-прежнему присылаю. Когда режим закончится — пришлю сводку.\n"
                   "Вернуться раньше: /back")


@router.message(Command("back"))
async def cmd_back(m: Message, bot: Bot):
    if not await state.away():
        return await m.answer("Режим «нет на связи» и так выключен.")
    await m.answer("Собираю, что было, пока вас не было…")
    await jobs.end_away(bot, reason="выключен")


@router.message(Command("night"))
async def cmd_night(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg == "off":
        await db.set("night", "off")
        return await m.answer("🌙 Ночной режим выключен.")
    rng = state.parse_range(arg)
    if not rng:
        return await m.answer("Пример: /night 23:00-08:00 — в это время отвечаю сам, без черновиков.\n"
                              "Выключить: /night off")
    value = f"{rng[0][0]:02d}:{rng[0][1]:02d}-{rng[1][0]:02d}:{rng[1][1]:02d}"
    await db.set("night", value)
    await m.answer(f"🌙 С {value.replace('-', ' до ')} буду отвечать сам (как в режиме «Авто»).")


# ---------- неотвеченные, утренний план ----------

@router.message(Command("wait"))
async def cmd_wait(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower().replace(",", ".")
    if arg == "off":
        await db.set("wait_hours", "off")
        return await m.answer("⏳ Напоминания о неотвеченных выключены.")
    try:
        hours = float(arg)
        assert 0 < hours <= 72
    except (ValueError, AssertionError):
        return await m.answer("Пример: /wait 3 — напомню, если человек ждёт ответа дольше 3 часов. /wait off — выключить")
    await db.set("wait_hours", f"{hours:g}")
    await m.answer(f"⏳ Напомню, если кто-то ждёт вашего ответа дольше {hours:g} ч (днём, с 9 до 22).")


async def _set_daily_time(m: Message, command: CommandObject, key: str, what: str):
    arg = (command.args or "").strip().lower()
    if arg == "off":
        await db.set(key, "off")
        return await m.answer(f"{what} выключен(а).")
    t = TIME_RE.fullmatch(arg)
    if not t:
        return await m.answer(f"Пример: /{m.text.split()[0][1:]} 09:00 или off")
    value = f"{int(t[1]):02d}:{t[2]}"
    await db.set(key, value)
    await m.answer(f"{what}: каждый день в {value}.")


@router.message(Command("morning"))
async def cmd_morning(m: Message, command: CommandObject):
    await _set_daily_time(m, command, "morning_time", "☀️ Утренний план")


@router.message(Command("plan"))
async def cmd_plan(m: Message):
    await m.answer(await jobs.morning_text())


# ---------- голосовые ----------

@router.message(Command("voice"))
async def cmd_voice(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        return await m.answer("Пример: /voice on или /voice off")
    if arg == "on" and not voice.available():
        return await m.answer("Расшифровка голосовых работает только с GigaChat.")
    await db.set("voice", arg)
    await m.answer("🎤 Расшифровываю голосовые и кружки." if arg == "on" else "🎤 Расшифровка выключена.")


# ---------- встречи → календарь ----------

@router.callback_query(F.data.startswith("ev:ics:"))
async def cb_ics(c: CallbackQuery):
    e = await db.get_event(int(c.data.rsplit(":", 1)[1]))
    if not e:
        return await c.answer("Встреча не найдена")
    data = make_ics(e["id"], e["title"], e["start_ts"], e["end_ts"], e["place"],
                    f"Из переписки с {e['chat_title']}")
    await c.message.answer_document(
        BufferedInputFile(data, filename="vstrecha.ics"),
        caption="Откройте файл — телефон предложит добавить встречу в календарь 📅")
    await c.answer()


# ---------- поиск по переписке ----------

def _stem(word: str) -> str:
    w = word.lower().strip(".,!?«»\"'()")
    return w[:max(4, len(w) - 2)] if len(w) > 4 else w


async def answer_search(m: Message, question: str):
    await m.answer("🔎 Ищу в переписке…")
    try:
        plan = await ai.plan_search(question)
    except Exception:
        log.exception("plan_search")
        plan = {"contact": None, "days": 14, "keywords": []}
    since = time.time() - plan["days"] * 86400
    chat_ids = None
    if plan["contact"]:
        found = await _find_contacts(plan["contact"])
        chat_ids = [r["chat_id"] for r in found] or None
    rows = await db.messages_between(since, chat_ids)
    if not rows:
        return await m.answer("За этот период ничего не нашёл. Я вижу только сообщения, "
                              "пришедшие после подключения бота.")

    chats: "OrderedDict[str, list]" = OrderedDict()
    if chat_ids:
        for r in rows:
            chats.setdefault(r["chat_title"], []).append(r)
        chats = OrderedDict((k, v[-300:]) for k, v in chats.items())
    else:
        stems = [_stem(k) for k in plan["keywords"] if k]
        by_chat: "OrderedDict[int, list]" = OrderedDict()
        for r in rows:
            by_chat.setdefault(r["chat_id"], []).append(r)
        for msgs in by_chat.values():
            hits = {i for i, r in enumerate(msgs)
                    if stems and any(s in (r["text"] or "").lower() for s in stems)}
            keep = sorted({j for i in hits for j in range(max(0, i - 3), min(len(msgs), i + 4))})
            if keep:
                chats[msgs[0]["chat_title"]] = [msgs[j] for j in keep]
        if not chats:   # ключевые слова не нашлись — отдаём свежее
            for msgs in by_chat.values():
                chats[msgs[0]["chat_title"]] = msgs[-40:]
    try:
        answer = await ai.answer_question(question, chats)
    except Exception as e:
        log.exception("answer_question")
        return await m.answer(f"⚠️ Не получилось найти ответ: {e}")
    await m.answer("🔎 " + answer)


@router.message(Command("ask"))
async def cmd_ask(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("Пример: /ask что Петя говорил про отчёт?\n"
                              "Или просто напишите мне вопрос со знаком «?» в конце.")
    await answer_search(m, command.args.strip())
