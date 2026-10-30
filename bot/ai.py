"""Всё общение с нейросетью: локальная модель (если есть) + GigaChat, YandexGPT или Claude."""
import json
import logging
import re
from datetime import datetime

from . import config, local_llm

log = logging.getLogger(__name__)

client = None
if config.LLM_PROVIDER == "local":
    pass    # только локальная модель, облачной нет
elif config.LLM_PROVIDER == "gigachat":
    from .gigachat import GigaChat

    client = GigaChat(config.GIGACHAT_AUTH_KEY, config.GIGACHAT_SCOPE)
elif config.LLM_PROVIDER == "yandex":
    from openai import AsyncOpenAI

    # YandexGPT через OpenAI-совместимый API Yandex AI Studio
    client = AsyncOpenAI(api_key=config.YANDEX_API_KEY,
                         base_url="https://llm.api.cloud.yandex.net/v1",
                         project=config.YANDEX_FOLDER_ID)
else:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)


_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _now_str() -> str:
    """Текущее время + календарь на 2 недели вперёд: модели плохо считают дни недели сами."""
    from datetime import timedelta

    now = datetime.now(config.TZ)
    offset = now.strftime("%z")
    days = []
    for i in range(14):
        d = now + timedelta(days=i)
        label = "сегодня" if i == 0 else "завтра" if i == 1 else "послезавтра" if i == 2 else ""
        days.append(f"{_WEEKDAYS[d.weekday()]} {d:%Y-%m-%d}" + (f" ({label})" if label else ""))
    return (f"{_WEEKDAYS[now.weekday()]}, {now:%Y-%m-%d %H:%M}, часовой пояс UTC{offset[:3]}:{offset[3:]}.\n"
            "Календарь на ближайшие дни:\n" + "\n".join(days))


def _extract_json(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели, даже если вокруг есть текст или ```json."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"В ответе нейросети нет JSON: {text[:200]}")


def _clean_str(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return None if s.lower() in ("", "null", "none") else s


def _normalize_tasks(raw) -> list[dict]:
    """Модели иногда отдают задачи строками, а не объектами — приводим к одному виду."""
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    tasks = []
    for t in raw:
        if isinstance(t, str):
            t = {"text": t, "due": None}
        if not isinstance(t, dict):
            continue
        text = _clean_str(t.get("text") or t.get("task") or t.get("title"))
        if text:
            tasks.append({"text": text, "due": _clean_str(t.get("due") or t.get("deadline"))})
    return tasks


async def _ask(system: str, user: str, model: str, max_tokens: int = 1024) -> str:
    # 1) локальная модель, если включена и не падала последние минуты
    if local_llm.available_now() or (config.LLM_PROVIDER == "local" and local_llm.enabled()):
        try:
            return await local_llm.chat(system, user, max_tokens)
        except Exception as e:
            local_llm.mark_down(f"{type(e).__name__}: {e}"[:200])
            if config.LLM_PROVIDER == "local":
                raise
    # 2) облачная нейросеть
    local_llm.state["last"] = config.LLM_PROVIDER
    return await _ask_cloud(system, user, model, max_tokens)


async def _ask_json(system: str, user: str, model: str, max_tokens: int = 1024) -> dict:
    """Запрос, ответ на который должен быть JSON. Если локальная модель ответила
    не по формату — переспрашиваем облачную (когда она есть)."""
    text = await _ask(system, user, model, max_tokens)
    try:
        return _extract_json(text)
    except ValueError:
        if local_llm.state["last"] != "local" or config.LLM_PROVIDER == "local":
            raise
        log.warning("Локальная модель ответила не JSON — переспрашиваю %s", config.LLM_PROVIDER)
        local_llm.state["last"] = config.LLM_PROVIDER
        return _extract_json(await _ask_cloud(system, user, model, max_tokens))


async def _ask_cloud(system: str, user: str, model: str, max_tokens: int) -> str:
    if config.LLM_PROVIDER == "gigachat":
        return await client.chat(model=model, system=system, user=user, max_tokens=max_tokens)
    if config.LLM_PROVIDER == "yandex":
        resp = await client.chat.completions.create(
            model=f"gpt://{config.YANDEX_FOLDER_ID}/{model}",
            max_tokens=max_tokens,
            temperature=0.3,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return (resp.choices[0].message.content or "").strip()
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def format_history(rows, new_ids: set[int] = frozenset()) -> str:
    lines = []
    for r in rows:
        t = datetime.fromtimestamp(r["ts"], config.TZ).strftime("%d.%m %H:%M")
        who = "Я" if r["from_owner"] else "Собеседник"
        mark = "* " if r["id"] in new_ids else ""
        text = (r["text"] or "")[:1500]
        lines.append(f"{mark}[{t}] [{who}]: {text}")
    return "\n".join(lines)


ANALYZE_SYSTEM = """Ты — личный секретарь владельца Telegram-аккаунта (владелец: {owner}).
Сейчас {now}.
Тебе дают переписку владельца с собеседником «{chat}». Реплики помечены [Я] (владелец) и [Собеседник].
Новые, ещё не разобранные сообщения отмечены звёздочкой *.

Сделай четыре вещи:
1. importance — важность НОВЫХ сообщений собеседника для владельца:
   "high" — срочно: деньги, проблема, дедлайн сегодня/завтра, просьба срочно связаться;
   "normal" — обычная переписка, на которую стоит ответить;
   "low" — болтовня, благодарности, стикеры, рассылки, реклама. Если новых сообщений собеседника нет — "low".
2. tasks — конкретные дела ВЛАДЕЛЬЦА из новых сообщений: о чём его попросили и что он сам пообещал
   («пришлю завтра», «перезвоню в 5»). Формулируй коротко, в повелительном наклонении, с именем собеседника
   при необходимости. Не повторяй уже открытые задачи (список ниже). Пустые обещания вида «ок» — не задачи.
   due — срок в формате ISO 8601 с часовым поясом, если он назван или однозначно следует из текста, иначе null.
   Дату бери строго из календаря выше (не вычисляй дни недели сам); если назван только день — ставь 10:00.
3. events — встречи и созвоны, о которых владелец ДОГОВОРИЛСЯ в новых сообщениях (есть конкретные день и время).
   start/end — ISO 8601 с часовым поясом (end можно null), place — место или ссылка, иначе null.
   Событие — только если ОБЕ стороны согласились И время названо в переписке явно («в 15:00», «завтра в 7 вечера»).
   Предложение без ответа («давай сегодня встретимся?») — НЕ событие. Время не названо — НЕ событие,
   не подставляй текущее время. В сомнениях — []. Если таких нет — [].
4. reply — {reply_rule}

Открытые задачи владельца:
{open_tasks}

Верни ТОЛЬКО JSON без пояснений и markdown:
{{"importance": "low|normal|high", "summary": "одна короткая фраза: чего хочет собеседник", "tasks": [{{"text": "...", "due": "ISO-8601 или null"}}], "events": [{{"title": "...", "start": "ISO-8601", "end": null, "place": null}}], "reply": "текст или null"}}"""

REPLY_RULE_ON = """владелец сейчас не на связи. Напиши короткий ответ собеседнику от имени владельца,
   от первого лица, на языке собеседника. Указание владельца о стиле ответа: «{persona}».
   {away}
   Жёсткие правила: не выдумывай фактов о владельце; не соглашайся на встречи, сроки, оплату и не давай
   обещаний — вместо этого скажи, что владелец посмотрит/ответит позже; не упоминай, что ты ИИ или бот,
   если указание владельца этого не требует. Если ответ не нужен (собеседник попрощался, поблагодарил,
   прислал стикер, это рассылка) — верни null.
   {facts}
   {style}"""

REPLY_RULE_OFF = "всегда null."


def _facts_block(facts: list[str]) -> str:
    if not facts:
        return ""
    return ("Проверенные факты от владельца — на вопросы о них отвечай по существу, точно, "
            "не меняя цифр и формулировок; чего тут нет — не выдумывай:\n"
            + "\n".join(f"   • {f}" for f in facts[:50]))


def _style_block(examples) -> str:
    if not examples:
        return ""
    lines = ["Как владелец правил твои прошлые черновики — пиши так, как в «стало»:"]
    for e in examples:
        lines.append(f"   было: {e['draft'][:300]}\n   стало: {e['final'][:300]}")
    return "\n".join(lines)


def _normalize_events(raw) -> list[dict]:
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        title, start = _clean_str(e.get("title")), parse_due(_clean_str(e.get("start")))
        if title and start:
            out.append({"title": title, "start": start, "end": parse_due(_clean_str(e.get("end"))),
                        "place": _clean_str(e.get("place"))})
    return out


async def analyze_chat(*, chat_title: str, history_rows, new_ids: set[int], want_reply: bool,
                       persona: str, owner_name: str, open_tasks: list[str],
                       facts: list[str] = (), style=(), away_note: str = "") -> dict:
    reply_rule = REPLY_RULE_OFF
    if want_reply:
        reply_rule = REPLY_RULE_ON.format(
            persona=persona,
            away=(f"ВАЖНО: владелец сейчас недоступен — {away_note}. Обязательно сообщи это собеседнику."
                  if away_note else ""),
            facts=_facts_block(list(facts)), style=_style_block(list(style)))
    system = ANALYZE_SYSTEM.format(
        owner=owner_name or "владелец",
        now=_now_str(),
        chat=chat_title,
        reply_rule=reply_rule,
        open_tasks="\n".join(f"- {t}" for t in open_tasks[:40]) or "(нет)",
    )
    raw = await _ask_json(system, format_history(history_rows, new_ids), config.LLM_MODEL)
    importance = str(raw.get("importance") or "normal").strip().lower()
    return {
        "importance": importance if importance in ("low", "normal", "high") else "normal",
        "summary": _clean_str(raw.get("summary")) or "",
        "tasks": _normalize_tasks(raw.get("tasks")),
        "events": _normalize_events(raw.get("events")),
        "reply": _clean_str(raw.get("reply")) if want_reply else None,
    }


SUMMARY_SYSTEM = """Ты — личный секретарь. Сейчас {now}.
Составь для владельца сводку его личных переписок в Telegram за {period}.
Формат — простой текст для Telegram (без markdown и HTML), на русском:
• сначала «Главное» — 1–5 пунктов, что требует внимания или ответа (с именем собеседника);
• затем по каждому чату 1–2 строки: имя — суть разговора, ждёт ли собеседник ответа.
Чаты без смысловой нагрузки (стикеры, «ок», рассылки) объединяй в одну строку в конце.
Пиши сжато, без воды и без вступлений."""


async def summarize(chats: dict[str, list], period: str) -> str:
    parts = []
    for title, rows in chats.items():
        parts.append(f"=== Чат: {title} ===\n{format_history(rows[-60:])}")
    user = "\n\n".join(parts)[:150_000]
    return await _ask(SUMMARY_SYSTEM.format(now=_now_str(), period=period), user,
                      config.SUMMARY_MODEL, max_tokens=2000)


DUE_RULES = """Правила для срока:
- дату бери строго из календаря выше, не вычисляй дни недели сам;
- формат: YYYY-MM-DDTHH:MM:00{tz};
- указано только время, и оно сегодня уже прошло — значит завтра;
- указан только день без времени — ставь 10:00;
- «через N минут/часов» — прибавь к текущему времени;
- срока нет — null."""

PARSE_TASK_SYSTEM = """Сейчас {now}

Владелец пишет своему секретарю заметку или просьбу напомнить.
Выдели суть дела (коротко, без слов о времени) и срок.
{rules}

Пример: «во вторник в 15 показать работу бота» →
{{"text": "Показать работу бота", "due": "<дата вторника из календаря>T15:00:00{tz}"}}

Верни ТОЛЬКО JSON вида {{"text": "...", "due": "..." или null}}"""

PARSE_WHEN_SYSTEM = """Сейчас {now}

Владелец говорит, когда напомнить о деле. Переведи это в точное время.
{rules}

Верни ТОЛЬКО JSON вида {{"due": "..." или null}}"""


def _tz() -> str:
    o = datetime.now(config.TZ).strftime("%z")
    return f"{o[:3]}:{o[3:]}"


async def parse_task(text: str) -> dict:
    system = PARSE_TASK_SYSTEM.format(now=_now_str(), tz=_tz(), rules=DUE_RULES.format(tz=_tz()))
    data = (await _ask_json(system, text, config.LLM_MODEL, 300))
    return {"text": _clean_str(data.get("text")) or text, "due": _clean_str(data.get("due"))}


async def parse_when(text: str) -> float | None:
    """«завтра в 15», «в пятницу», «через 2 часа» -> unix-время."""
    system = PARSE_WHEN_SYSTEM.format(now=_now_str(), rules=DUE_RULES.format(tz=_tz()))
    data = (await _ask_json(system, text, config.LLM_MODEL, 100))
    return parse_due(_clean_str(data.get("due")))


def parse_due(value) -> float | None:
    """ISO-строка от нейросети -> unix-время (или None)."""
    if not value or value == "null":
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        log.warning("Не разобрал срок %r", value)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return dt.timestamp()


# ---------- режим «нет на связи» ----------

PARSE_AWAY_SYSTEM = """Сейчас {now}

Владелец включает режим «нет на связи» и пишет, до какого времени и почему.
{rules}
Если дата окончания не названа — until = null.
note — коротко, что сообщать собеседникам, от первого лица (например: «в отпуске, вернусь 5 ноября»);
если причина не названа — «сейчас не на связи, вернусь <дата>».

Верни ТОЛЬКО JSON вида {{"until": "..." или null, "note": "..."}}"""


async def parse_away(text: str) -> dict:
    system = PARSE_AWAY_SYSTEM.format(now=_now_str(), rules=DUE_RULES.format(tz=_tz()))
    data = (await _ask_json(system, text, config.LLM_MODEL, 200))
    return {"until": parse_due(_clean_str(data.get("until"))), "note": _clean_str(data.get("note")) or ""}


# ---------- поиск по переписке ----------

PLAN_SEARCH_SYSTEM = """Сейчас {now}

Владелец задаёт вопрос о своих переписках в Telegram. Помоги найти нужные сообщения.
Верни ТОЛЬКО JSON:
{{"contact": "имя или @ник собеседника, если вопрос про конкретного человека, иначе null",
  "days": сколько дней назад искать (по умолчанию 14, максимум 90),
  "keywords": ["2–6 ключевых слов для поиска, в начальной форме"]}}"""

ANSWER_SYSTEM = """Ты — личный секретарь. Сейчас {now}
Ответь на вопрос владельца, опираясь ТОЛЬКО на фрагменты его переписок ниже.
Называй собеседников и даты. Если ответа во фрагментах нет — так и скажи, не выдумывай.
Простой текст для Telegram, без markdown, коротко."""


async def plan_search(question: str) -> dict:
    data = (await _ask_json(PLAN_SEARCH_SYSTEM.format(now=_now_str()), question,
                                    config.LLM_MODEL, 200))
    try:
        days = max(1, min(90, int(data.get("days") or 14)))
    except (TypeError, ValueError):
        days = 14
    kws = data.get("keywords") or []
    if isinstance(kws, str):
        kws = [kws]
    return {"contact": _clean_str(data.get("contact")), "days": days,
            "keywords": [str(k).strip() for k in kws if str(k).strip()]}


async def answer_question(question: str, chats: dict[str, list]) -> str:
    parts = [f"=== Чат: {title} ===\n{format_history(rows)}" for title, rows in chats.items()]
    user = f"Вопрос: {question}\n\nФрагменты переписок:\n\n" + "\n\n".join(parts)[:120_000]
    return await _ask(ANSWER_SYSTEM.format(now=_now_str()), user, config.SUMMARY_MODEL, 1500)
