"""Навыки: погода, список покупок, дни рождения, шаблонные ответы."""
import logging
import re
import time
from datetime import datetime, timedelta

import aiohttp
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from . import config
from .db import db
from .util import kb

log = logging.getLogger(__name__)
router = Router(name="skills")
router.message.filter(F.chat.type == "private", F.from_user.id == config.OWNER_ID)
router.callback_query.filter(F.from_user.id == config.OWNER_ID)

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]


# =====================================================================
# Погода (Open-Meteo — бесплатно, без ключа)
# =====================================================================

WMO = {0: "☀️ ясно", 1: "🌤 малооблачно", 2: "⛅️ переменная облачность", 3: "☁️ пасмурно",
       45: "🌫 туман", 48: "🌫 туман", 51: "🌦 морось", 53: "🌦 морось", 55: "🌦 морось",
       56: "🌧 ледяная морось", 57: "🌧 ледяная морось", 61: "🌧 небольшой дождь", 63: "🌧 дождь",
       65: "🌧 сильный дождь", 66: "🌧 ледяной дождь", 67: "🌧 ледяной дождь",
       71: "🌨 небольшой снег", 73: "🌨 снег", 75: "❄️ сильный снег", 77: "🌨 снежная крупа",
       80: "🌦 ливни", 81: "🌧 ливни", 82: "⛈ сильные ливни", 85: "🌨 снегопад", 86: "❄️ сильный снегопад",
       95: "⛈ гроза", 96: "⛈ гроза с градом", 99: "⛈ гроза с градом"}


UA = {"User-Agent": "secretary-bot/1.0 github.com/bogatkin82-jpg/secretary-bot"}


async def _get_json(url: str, params: dict) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=UA) as s:
        async with s.get(url, params=params) as r:
            r.raise_for_status()
            return await r.json(content_type=None)


async def geocode(name: str) -> dict | None:
    data = await _get_json("https://geocoding-api.open-meteo.com/v1/search",
                           {"name": name, "count": 1, "language": "ru", "format": "json"})
    res = (data.get("results") or [None])[0]
    if not res:
        return None
    label = ", ".join(x for x in (res.get("name"), res.get("admin1"), res.get("country")) if x)
    return {"name": res.get("name") or name, "label": label,
            "lat": res["latitude"], "lon": res["longitude"]}


async def _open_meteo(lat: str, lon: str) -> dict:
    data = await _get_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
        "current": "temperature_2m,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,wind_speed_10m_max",
    })
    d, c = data["daily"], data.get("current") or {}
    wind = d.get("wind_speed_10m_max", [None])[0]
    return {"desc": WMO.get(int(d["weather_code"][0]), "🌡"),
            "tmin": d["temperature_2m_min"][0], "tmax": d["temperature_2m_max"][0],
            "now": c.get("temperature_2m"),
            "rain": d.get("precipitation_probability_max", [None])[0],
            "wind": wind / 3.6 if wind else None}      # км/ч -> м/с


# запасной источник — норвежский метеоинститут (бесплатно, без ключа)
MET_SYMBOLS = [("thunder", "⛈ гроза"), ("heavysnow", "❄️ сильный снег"), ("snow", "🌨 снег"),
               ("sleet", "🌨 мокрый снег"), ("heavyrain", "🌧 сильный дождь"), ("lightrain", "🌧 небольшой дождь"),
               ("rain", "🌧 дождь"), ("fog", "🌫 туман"), ("cloudy", "☁️ пасмурно"),
               ("partlycloudy", "⛅️ переменная облачность"), ("fair", "🌤 малооблачно"), ("clearsky", "☀️ ясно")]


async def _met_no(lat: str, lon: str) -> dict:
    data = await _get_json("https://api.met.no/weatherapi/locationforecast/2.0/compact",
                           {"lat": round(float(lat), 3), "lon": round(float(lon), 3)})
    series = data["properties"]["timeseries"]
    today = datetime.now(config.TZ).date()
    temps, winds, rain_mm, symbols = [], [], 0.0, []
    for p in series:
        t = datetime.fromisoformat(p["time"].replace("Z", "+00:00")).astimezone(config.TZ)
        if t.date() != today:
            continue
        det = p["data"]["instant"]["details"]
        temps.append(det.get("air_temperature"))
        winds.append(det.get("wind_speed") or 0)
        nxt = p["data"].get("next_1_hours") or p["data"].get("next_6_hours") or {}
        rain_mm += (nxt.get("details") or {}).get("precipitation_amount", 0) if "next_1_hours" in p["data"] else 0
        if nxt.get("summary", {}).get("symbol_code") and 7 <= t.hour <= 21:
            symbols.append(nxt["summary"]["symbol_code"])
    if not temps:   # поздний вечер — берём ближайшие часы
        p = series[0]
        temps = [p["data"]["instant"]["details"]["air_temperature"]]
    worst = next((label for key, label in MET_SYMBOLS if any(key in sym for sym in symbols)), "🌡")
    now = series[0]["data"]["instant"]["details"].get("air_temperature")
    return {"desc": worst, "tmin": min(temps), "tmax": max(temps), "now": now,
            "rain": 70 if rain_mm >= 1 else (40 if rain_mm > 0.2 else None),
            "wind": max(winds) if winds else None}


async def weather_text() -> str | None:
    """Строка погоды на сегодня для утреннего плана, или None, если город не задан."""
    lat, lon, city = await db.get("city_lat"), await db.get("city_lon"), await db.get("city_name")
    if not (lat and lon):
        return None
    w, errors = None, []
    for source in (_open_meteo, _open_meteo, _met_no):   # Open-Meteo дважды, затем запасной
        try:
            w = await source(lat, lon)
            break
        except Exception as e:
            errors.append(f"{source.__name__}: {e}")
            log.warning("Погода: %s недоступен: %s", source.__name__, e)
    if w is None:
        raise RuntimeError("все источники погоды недоступны")
    tmin, tmax = round(w["tmin"]), round(w["tmax"])
    parts = [f"{w['desc']}, {tmin:+d}…{tmax:+d}°"]
    if w["now"] is not None:
        parts.append(f"сейчас {round(w['now']):+d}°")
    if w["rain"]:
        parts.append(f"осадки {w['rain']}%")
    if w["wind"] and w["wind"] >= 7:
        parts.append(f"ветер до {round(w['wind'])} м/с")
    tip = ""
    if w["rain"] and w["rain"] >= 50:
        tip = " — возьмите зонт ☂️"
    elif tmax <= -10:
        tip = " — одевайтесь теплее 🧣"
    return f"{city}: " + ", ".join(parts) + tip


@router.message(Command("city"))
async def cmd_city(m: Message, command: CommandObject):
    if not command.args:
        cur = await db.get("city_name")
        return await m.answer((f"Город для погоды: {cur}\n" if cur else "") +
                              "Указать город: /city Москва")
    try:
        place = await geocode(command.args.strip())
    except Exception as e:
        log.exception("geocode")
        return await m.answer("⚠️ Сервис поиска городов сейчас не отвечает, попробуйте чуть позже.")
    if not place:
        return await m.answer("Не нашёл такой город 🤔 Попробуйте написать иначе: /city Санкт-Петербург")
    await db.set("city_name", place["name"])
    await db.set("city_lat", str(place["lat"]))
    await db.set("city_lon", str(place["lon"]))
    try:
        text = f"Сейчас: {await weather_text()}"
    except Exception:
        text = "Прогноз сейчас не загрузился — попробуйте /weather чуть позже."
    await m.answer(f"📍 Город: {place['label']}\nПогода будет в утреннем плане.\n\n{text}")


@router.message(Command("weather"))
async def cmd_weather(m: Message):
    try:
        text = await weather_text()
    except Exception as e:
        log.exception("weather")
        return await m.answer("⚠️ Сервисы погоды сейчас не отвечают, попробуйте через несколько минут.")
    await m.answer(text or "Сначала укажите город: /city Москва")


# =====================================================================
# Список покупок
# =====================================================================

def split_items(text: str) -> list[str]:
    parts = re.split(r"[,;\n]+|\s+и\s+", text)
    return [p.strip(" .-—•+").strip() for p in parts if p.strip(" .-—•+").strip()]


def _shop_view(items):
    if not items:
        return ("🛒 Список покупок пуст.\nДобавить: /buy молоко, хлеб — или просто напишите мне «+молоко».",
                None)
    text = "🛒 Купить:\n" + "\n".join(f"• {i['item']}" for i in items) + \
           "\n\nНажмите на товар, когда купите."
    rows = [[(f"✅ {i['item'][:40]}", f"shop:del:{i['id']}")] for i in items[:40]]
    rows.append([("🗑 Очистить весь список", "shop:clear")])
    return text, kb(*rows)


async def add_shopping_items(m: Message, text: str):
    items = split_items(text)
    if not items:
        return await m.answer("Пример: /buy молоко, хлеб, яйца")
    await db.add_shopping(items)
    total = len(await db.shopping())
    await m.answer(f"🛒 Добавил: {', '.join(items)}. Всего в списке: {total} (/list)")


@router.message(Command("buy"))
async def cmd_buy(m: Message, command: CommandObject):
    await add_shopping_items(m, command.args or "")


@router.message(Command("list"))
async def cmd_list(m: Message):
    text, markup = _shop_view(await db.shopping())
    await m.answer(text, reply_markup=markup)


@router.message(F.text.startswith("+"))
async def plus_item(m: Message):
    await add_shopping_items(m, m.text[1:])


@router.callback_query(F.data.startswith("shop:"))
async def cb_shop(c: CallbackQuery):
    parts = c.data.split(":")
    if parts[1] == "clear":
        await db.delete_shopping()
    else:
        await db.delete_shopping(int(parts[2]))
    text, markup = _shop_view(await db.shopping())
    await c.message.edit_text(text, reply_markup=markup)
    await c.answer("Готово")


# =====================================================================
# Дни рождения
# =====================================================================

def fmt_bday(month: int, day: int) -> str:
    return f"{day} {MONTHS[month - 1]}"


def parse_day_month(text: str) -> tuple[int, int] | None:
    """'15.03', '15/3', '15 марта' -> (месяц, день)."""
    text = text.strip().lower()
    m = re.search(r"(\d{1,2})[./-](\d{1,2})", text)
    if m:
        day, month = int(m[1]), int(m[2])
    else:
        m = re.search(r"(\d{1,2})\s+([а-яё]+)", text)
        if not m:
            return None
        day = int(m[1])
        month = next((i + 1 for i, name in enumerate(MONTHS) if m[2].startswith(name[:3])), 0)
    try:
        datetime(2024, month, day)      # 2024 — високосный, 29.02 допустим
    except ValueError:
        return None
    return month, day


async def save_birthday(bot: Bot, name: str, month: int, day: int, chat_id: int | None, source: str):
    """Записывает день рождения и сообщает владельцу (если новый)."""
    from .util import notify
    bid = await db.add_birthday(name, month, day, chat_id, source)
    if bid:
        await notify(bot, f"🎂 Запомнил день рождения: {name} — {fmt_bday(month, day)} ({source}).\n"
                          "Напомню накануне и в сам день. Все дни рождения: /bdays",
                     kb([("🗑 Не нужно", f"bday:del:{bid}")]))


async def birthdays_text(today: datetime) -> list[str]:
    """Строки для утреннего плана: ДР сегодня и завтра."""
    lines = []
    tomorrow = today + timedelta(days=1)
    for b in await db.birthdays_on(today.month, today.day):
        lines.append(f"🎂 Сегодня день рождения: {b['name']} — не забудьте поздравить!")
    for b in await db.birthdays_on(tomorrow.month, tomorrow.day):
        lines.append(f"🎁 Завтра день рождения: {b['name']}")
    return lines


async def check_profile_birthday(bot: Bot, chat_id: int, name: str):
    """Если собеседник указал день рождения в профиле Telegram — запоминаем."""
    try:
        chat = await bot.get_chat(chat_id)
        bd = getattr(chat, "birthdate", None)
        if bd and bd.month and bd.day:
            await save_birthday(bot, name, bd.month, bd.day, chat_id, "из профиля Telegram")
    except Exception as e:
        log.debug("Нет данных профиля %s: %s", chat_id, e)


def _bdays_view(rows):
    if not rows:
        return ("🎂 Дней рождения пока нет.\nЯ запоминаю их из переписки и профилей Telegram.\n"
                "Добавить вручную: /bday Маша 15.03"), None
    text = "🎂 Дни рождения:\n" + "\n".join(f"• {fmt_bday(r['month'], r['day'])} — {r['name']}" for r in rows)
    return text, kb(*[[(f"🗑 {r['name']} ({r['day']:02d}.{r['month']:02d})", f"bday:del:{r['id']}")]
                      for r in rows[:40]])


@router.message(Command("bday"))
async def cmd_bday(m: Message, command: CommandObject, bot: Bot):
    args = (command.args or "").strip()
    md = parse_day_month(args)
    name = re.sub(r"\d{1,2}([./-]\d{1,2}|\s+[а-яё]+)\S*", "", args, flags=re.I).strip(" ,—-")
    if not md or not name:
        return await m.answer("Пример: /bday Маша 15.03 или /bday Петя 3 мая")
    if not await db.add_birthday(name, md[0], md[1], None, "добавлено вручную"):
        return await m.answer("Этот день рождения уже записан.")
    await m.answer(f"🎂 Записал: {name} — {fmt_bday(*md)}. Напомню накануне и в сам день.")


@router.message(Command("bdays"))
async def cmd_bdays(m: Message):
    text, markup = _bdays_view(await db.birthdays())
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("bday:del:"))
async def cb_bday_del(c: CallbackQuery):
    await db.delete_birthday(int(c.data.rsplit(":", 1)[1]))
    if (c.message.text or "").startswith("🎂 Дни рождения"):
        text, markup = _bdays_view(await db.birthdays())
        await c.message.edit_text(text, reply_markup=markup)
    else:
        await c.message.edit_text((c.message.text or "") + "\n\n🗑 Удалено")
    await c.answer("Удалено")


# =====================================================================
# Шаблонные ответы
# =====================================================================

TEMPLATE_COOLDOWN = 12 * 3600   # один и тот же шаблон одному человеку — не чаще раза в 12 часов


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def template_matches(keywords: str, text: str) -> bool:
    t = _norm(text)
    return any(k and k in t for k in (_norm(k).strip() for k in keywords.split(",")))


async def find_template(chat_id: int, texts: list[str]):
    """Первый шаблон, подходящий под новые сообщения собеседника и не отправленный недавно."""
    joined = "\n".join(texts)
    for tpl in await db.templates():
        if template_matches(tpl["keywords"], joined):
            if time.time() - await db.template_sent_ts(chat_id, tpl["id"]) > TEMPLATE_COOLDOWN:
                return tpl
    return None


def _tpl_view(rows):
    if not rows:
        return ("📋 Шаблонов пока нет.\n\nШаблон — готовый ответ на типовой вопрос. Если в сообщении "
                "есть одно из ключевых слов, я отвечу этим текстом (в режиме «Черновик» — пришлю на одобрение).\n\n"
                "Добавить: /tpl цена, стоимость, сколько стоит | Стрижка — 1500 ₽, борода — 700 ₽\n"
                "Слева от | — ключевые слова через запятую, справа — ответ."), None
    text = "📋 Шаблоны:\n" + "\n".join(f"{r['id']}. [{r['keywords']}] → {r['answer']}" for r in rows)
    text += "\n\nДобавить: /tpl слова | ответ"
    return text, kb(*[[(f"🗑 {r['id']}", f"tpl:del:{r['id']}")] for r in rows[:40]])


@router.message(Command("tpl"))
async def cmd_tpl(m: Message, command: CommandObject):
    args = command.args or ""
    if "|" not in args:
        text, markup = _tpl_view(await db.templates())
        return await m.answer(text, reply_markup=markup)
    keywords, answer = (p.strip() for p in args.split("|", 1))
    keywords = ", ".join(k.strip() for k in keywords.split(",") if k.strip())
    if not keywords or not answer:
        return await m.answer("Пример: /tpl адрес, где вы | Мы на ул. Ленина, 5, вход со двора")
    tid = await db.add_template(keywords, answer)
    await m.answer(f"📋 Шаблон №{tid} сохранён.\nНа сообщения со словами «{keywords}» отвечу:\n{answer}")


@router.callback_query(F.data.startswith("tpl:del:"))
async def cb_tpl_del(c: CallbackQuery):
    await db.delete_template(int(c.data.rsplit(":", 1)[1]))
    text, markup = _tpl_view(await db.templates())
    await c.message.edit_text(text, reply_markup=markup)
    await c.answer("Удалено")
