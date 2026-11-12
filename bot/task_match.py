"""Поиск уже открытой задачи, которую продолжает новое сообщение.

Модель получает ID открытых задач и обычно сама возвращает нужный ID.
Этот модуль — страховка: если модель не вернула ID, ищем задачу по тексту
в том же чате, чтобы не создавать точный дубль.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher


def normalize_text(text: str | None) -> str:
    text = (text or "").casefold().replace("ё", "е")
    text = re.sub(r"[^\wа-я0-9]+", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _get(task, key: str, default=None):
    try:
        return task[key]
    except (KeyError, IndexError, TypeError):
        return default


def _is_open(task) -> bool:
    return not int(_get(task, "done", 0) or 0)


def find_open_task(tasks, chat_id: int, text: str, task_id=None):
    """Вернуть открытую задачу из этого чата или None.

    Приоритет:
    1. ID, который вернула модель (но только если задача из этого чата и открыта);
    2. точное совпадение нормализованного текста;
    3. вложенность текста («проверить парковку» / «проверить парковку на планке»);
    4. очень высокая похожесть (SequenceMatcher >= 0.92).
    """
    wanted_id = _as_int(task_id)
    if wanted_id is not None:
        for task in tasks:
            if (_as_int(_get(task, "id")) == wanted_id
                    and _as_int(_get(task, "chat_id")) == int(chat_id)
                    and _is_open(task)):
                return task

    key = normalize_text(text)
    if not key:
        return None

    candidates: list[tuple[float, object]] = []
    for task in tasks:
        if _as_int(_get(task, "chat_id")) != int(chat_id) or not _is_open(task):
            continue
        other = normalize_text(_get(task, "text", ""))
        if not other:
            continue
        if other == key:
            return task
        if len(key) >= 8 and (key in other or other in key):
            candidates.append((1.0, task))
            continue
        ratio = SequenceMatcher(None, key, other).ratio()
        if ratio >= 0.92:
            candidates.append((ratio, task))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]

def group_similar_tasks(tasks) -> list[list]:
    """Сгруппировать похожие открытые задачи внутри одного чата.

    Возвращает только группы из двух и более задач. Ничего не меняет.
    Группировка транзитивная: A~B и B~C попадут в одну группу.
    """
    tasks = list(tasks)
    parent = list(range(len(tasks)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = root(i), root(j)
        if ri != rj:
            parent[rj] = ri

    for i, first in enumerate(tasks):
        for j in range(i + 1, len(tasks)):
            second = tasks[j]
            if _as_int(_get(first, "chat_id")) != _as_int(_get(second, "chat_id")):
                continue
            if not _is_open(first) or not _is_open(second):
                continue
            if find_open_task([first], _get(second, "chat_id"), _get(second, "text", "")) is first:
                union(i, j)

    groups: dict[int, list] = {}
    for i, task in enumerate(tasks):
        groups.setdefault(root(i), []).append(task)
    return [group for group in groups.values() if len(group) > 1]
