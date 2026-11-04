"""/settings — все основные настройки кнопками: режим, время плана и сводки, пауза, ночь, голос."""
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from . import config, state
from .db import db
from .util import MODE_NAMES, kb

router = Router(name="settings_menu")
router.message.filter(F.chat.type == "private", F.from_user.id == config.OWNER_ID)
router.callback_query.filter(F.from_user.id == config.OWNER_ID)

awaiting: dict[int, str] = {}     # владелец -> какую настройку он вводит текстом

TIME_RE = re.compile(r"\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*")

OPTIONS = {
    "morning_time": ("☀️ Утренний план — во сколько присылать?",
                     ["06:00", "07:00", "08:00", "09:00", "10:00", "11:00", "off"], True),
    "digest_time": ("🗞 Вечерняя сводка — во сколько присылать?",
                    ["18:00", "19:00", "20:00", "21:00", "22:00", "23:00", "off"], True),
    "idle_minutes": ("💤 Через сколько минут вашего молчания в чате готовить ответ?",
                     ["0", "5", "15", "30", "60"], False),
    "wait_hours": ("⏳ Через сколько часов напоминать, что человек ждёт ответа?",
                   ["1", "2", "3", "6", "12", "off"], False),
    "night": ("🌙 Ночной режим — в это время бот отвечает сам, без черновиков",
              ["22:00-07:00", "23:00-08:00", "00:00-09:00", "off"], True),
}


def _label(key: str, value: str) -> str:
    if value == "off":
        return "выкл"
    return {"idle_minutes": f"{value} мин", "wait_hours": f"{value} ч"}.get(key, value)


async def _main_view():
    from .owner import _status
    g = db.get
    mode = await g("mode")
    mark = {m: ("● " if m == mode else "") for m in MODE_NAMES}
    rows = [
        [(f"{mark['off']}🔕 Выкл", "cfg:mode:off"), (f"{mark['draft']}📝 Черновик", "cfg:mode:draft"),
         (f"{mark['auto']}🤖 Авто", "cfg:mode:auto")],
        [(f"☀️ План: {_label('morning_time', await g('morning_time'))}", "cfg:open:morning_time"),
         (f"🗞 Сводка: {_label('digest_time', await g('digest_time'))}", "cfg:open:digest_time")],
        [(f"💤 Пауза: {_label('idle_minutes', await g('idle_minutes'))}", "cfg:open:idle_minutes"),
         (f"⏳ Ждут ответа: {_label('wait_hours', await g('wait_hours'))}", "cfg:open:wait_hours")],
        [(f"🌙 Ночь: {_label('night', await g('night'))}", "cfg:open:night"),
         (f"🎤 Голосовые: {'вкл' if await g('voice') == 'on' else 'выкл'}", "cfg:voice")],
        [("🗞 Сводка сейчас", "summary:24"), ("☀️ План сейчас", "cfg:plan")],
    ]
    return "⚙️ Настройки\n\n" + await _status(), kb(*rows)


async def _option_view(key: str):
    title, values, custom = OPTIONS[key]
    current = await db.get(key)
    buttons = [(("● " if v == current else "") + _label(key, v), f"cfg:set:{key}:{v}") for v in values]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    tail = [("⬅️ Назад", "cfg:back")]
    if custom:
        tail.insert(0, ("✏️ Своё время", f"cfg:custom:{key}"))
    rows.append(tail)
    return f"{title}\nСейчас: {_label(key, current)}", kb(*rows)


@router.message(Command("settings"))
async def cmd_settings(m: Message):
    text, markup = await _main_view()
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("cfg:"))
async def cb_cfg(c: CallbackQuery, bot: Bot):
    parts = c.data.split(":", 3)
    action = parts[1]
    if action == "mode":
        await db.set("mode", parts[2])
        await c.answer(f"Режим: {MODE_NAMES[parts[2]]}")
    elif action == "voice":
        await db.set("voice", "off" if await db.get("voice") == "on" else "on")
        await c.answer("Готово")
    elif action == "open":
        text, markup = await _option_view(parts[2])
        await c.message.edit_text(text, reply_markup=markup)
        return await c.answer()
    elif action == "set":
        key, value = parts[2], parts[3]
        await db.set(key, value)
        await c.answer(f"Сохранено: {_label(key, value)}")
    elif action == "custom":
        awaiting[c.from_user.id] = parts[2]
        hint = "например 23:30-07:30" if parts[2] == "night" else "например 08:30"
        await c.message.answer(f"Напишите время, {hint}. Передумали — «отмена».")
        return await c.answer()
    elif action == "plan":
        from .jobs import morning_kb, morning_text
        await c.message.answer(await morning_text(), reply_markup=await morning_kb())
        return await c.answer()
    # back / после изменения — главный экран
    text, markup = await _main_view()
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass   # текст не изменился
    if action == "back":
        await c.answer()


async def handle_custom_input(m: Message, key: str) -> None:
    """Владелец прислал своё время для настройки."""
    text = m.text.strip()
    if key == "night":
        rng = state.parse_range(text)
        if not rng:
            awaiting[m.from_user.id] = key
            return await m.answer("Не понял 🤔 Пример: 23:30-07:30")
        value = f"{rng[0][0]:02d}:{rng[0][1]:02d}-{rng[1][0]:02d}:{rng[1][1]:02d}"
    else:
        t = TIME_RE.fullmatch(text)
        if not t:
            awaiting[m.from_user.id] = key
            return await m.answer("Не понял 🤔 Пример: 08:30")
        value = f"{int(t[1]):02d}:{t[2]}"
    await db.set(key, value)
    text, markup = await _main_view()
    await m.answer(f"✅ Сохранено: {value}\n\n{text}", reply_markup=markup)
