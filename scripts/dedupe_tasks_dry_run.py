#!/usr/bin/env python3
"""Dry-run: показать похожие открытые задачи, ничего не меняя в базе.

Запуск на сервере из папки проекта:

    python scripts/dedupe_tasks_dry_run.py
    python scripts/dedupe_tasks_dry_run.py --chat 123456789
    python scripts/dedupe_tasks_dry_run.py --json

Скрипт только читает SQLite. Для слияния задач нужен отдельный --apply,
которого здесь сознательно нет.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import config  # noqa: E402
from bot.db import DB  # noqa: E402
from bot.task_match import group_similar_tasks  # noqa: E402


def _one_line(value, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _fmt_due(due_ts) -> str:
    if not due_ts:
        return "без срока"
    return datetime.fromtimestamp(float(due_ts), config.TZ).strftime("%d.%m %H:%M")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Показать похожие открытые задачи (dry-run)")
    parser.add_argument("--chat", type=int, help="только один chat_id")
    parser.add_argument("--json", action="store_true", help="вывести JSON для разбора")
    args = parser.parse_args()

    db = DB(config.DB_PATH)
    await db.open()
    try:
        tasks = list(await db.open_tasks())
    finally:
        await db.close()

    if args.chat is not None:
        tasks = [t for t in tasks if int(t["chat_id"]) == args.chat]

    groups = group_similar_tasks(tasks)
    groups.sort(key=lambda group: min(int(t["id"]) for t in group))

    if args.json:
        payload = []
        for group in groups:
            payload.append([
                {
                    "id": int(t["id"]),
                    "chat_id": int(t["chat_id"]),
                    "chat_title": t["chat_title"],
                    "text": t["text"],
                    "due_ts": t["due_ts"],
                }
                for t in sorted(group, key=lambda row: int(row["id"]))
            ])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Открытых задач: {len(tasks)}")
    print(f"Групп похожих задач: {len(groups)}")
    print("Режим: dry-run, база не изменяется.\n")

    if not groups:
        print("Похожих задач не найдено.")
        return 0

    for number, group in enumerate(groups, 1):
        ordered = sorted(group, key=lambda row: int(row["id"]))
        title = _one_line(ordered[0]["chat_title"], 100)
        print(f"Группа {number} · чат «{title}» · задач: {len(ordered)}")
        for task in ordered:
            print(f"  #{int(task['id'])} · {_fmt_due(task['due_ts'])} · {_one_line(task['text'])}")
        print()

    print("Ничего не изменено. Для слияния сначала удали лишние вручную через /tasks")
    print("или попроси подготовить отдельный --apply с подтверждением.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
