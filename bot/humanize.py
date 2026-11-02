"""Последняя правка ответа перед отправкой: убираем то, что выдаёт текст нейросети
(длинные тире, «ёлочки», markdown, шаблонные вежливости, лишние эмодзи и восклицания),
и подстраиваемся под то, как пишет сам владелец."""
import re

EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF️‍]+")

# шаблонные вставки, по которым сразу видно нейросеть
STOCK = [
    r"спасибо за (ваше|твоё|твое) сообщение[.!,]?",
    r"спасибо,? что (написал[аи]?|обратил[иа]сь)[.!,]?",
    r"отличный вопрос[.!,]?",
    r"рад(а)? помочь[.!,]?",
    r"надеюсь,? это поможет[.!]?",
    r"если (у вас |у тебя )?(будут|возникнут|появятся) (ещё |еще )?вопросы,?[^.!?]*[.!?]?",
    r"не стесняйтесь [^.!?]*[.!?]?",
    r"важно отметить,? что",
    r"стоит отметить,? что",
    r"в целом,?",
]
OPENERS = [r"конечно[!,.]\s*", r"безусловно[!,.]\s*", r"разумеется[!,.]\s*"]


def _owner_habits(samples: list[str]) -> dict:
    s = [x.strip() for x in samples if x and x.strip()]
    n = len(s) or 1
    return {
        "emoji": any(EMOJI.search(x) for x in s),
        "long_dash": any("—" in x for x in s),
        "guillemets": any("«" in x for x in s),
        "end_period": sum(x.endswith(".") for x in s) / n > 0.5,
        "lower_start": sum(x[0].islower() for x in s if x[0].isalpha()) / n > 0.5 if s else False,
        "exclaim": any("!" in x for x in s),
    }


def humanize(text: str | None, owner_samples: list[str] | None = None) -> str | None:
    if not text:
        return text
    h = _owner_habits(owner_samples or [])
    t = text.strip()

    # markdown и служебная разметка
    t = re.sub(r"(\*\*|__|`)", "", t)
    t = re.sub(r"^\s*[-•*]\s+", "", t, flags=re.M)          # маркеры списков
    t = re.sub(r"\n{2,}", "\n", t)

    # шаблонные фразы
    for pat in STOCK:
        t = re.sub(pat, "", t, flags=re.I)
    for pat in OPENERS:
        t = re.sub(r"^" + pat, "", t, flags=re.I)

    # типографика, которую в мессенджере руками почти не набирают
    if not h["long_dash"]:
        t = re.sub(r"\s*[—–]\s*", " - ", t)
    if not h["guillemets"]:
        t = t.replace("«", "").replace("»", "").replace("“", "").replace("”", "").replace("„", "")
    t = t.replace("…", "...")
    if not h["emoji"]:
        t = EMOJI.sub("", t)

    # восклицания
    t = re.sub(r"!{2,}", "!", t)
    if not h["exclaim"]:
        t = t.replace("!", ".")
    t = re.sub(r"\.{4,}", "...", t)
    t = re.sub(r"(?<!\.)\.\.(?!\.)", ".", t)

    # пробелы и пустые хвосты после вырезания
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\s+([,.!?])", r"\1", t)
    t = re.sub(r"^[\s,.;:-]+", "", t).strip()
    if not t:
        return None

    # регистр первой буквы и точка в конце — как у владельца
    def fix_case(m: re.Match) -> str:
        ch = m.group(2)
        return m.group(1) + (ch.lower() if h["lower_start"] else ch.upper())
    # первая буква текста и каждого предложения; «Я» в начале оставляем как есть у формального стиля
    t = re.sub(r"(^|[.!?]\s+)([A-Za-zА-Яа-яЁё])", fix_case, t)
    if not h["end_period"] and t.endswith(".") and not t.endswith("..."):
        t = t[:-1]
    return t
