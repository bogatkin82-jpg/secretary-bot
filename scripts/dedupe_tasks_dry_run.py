#!/usr/bin/env python3
"""Поиск и безопасное слияние дублей открытых задач.

По умолчанию ничего не меняет:

    python scripts/dedupe_tasks_dry_run.py
    python scripts/dedupe_tasks_dry_run.py --chat 123456789
    python scripts/dedupe_tasks_dry_run.py --json

Применение (с обязательным подтверждением):

    python scripts/dedupe_tasks_dry_run.py --apply --yes

Правила слияния:
- остаётся самая старая задача группы (минимальный id);
- берётся самый длинный текст (обычно он подробнее);
- срок — ближайший будущий; если будущих нет — самый поздний прошлый;
- перед изменениями создаётся резервная копия SQLite в data/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aiosqlite  # noqa: E402

from bot import config  # noqa: E402
from bot.db import DB  # noqa: E402
from bot.task_match import group_similar_tasks  # noqa: E402


def _one_line(value, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _fmt_due(due_ts) -> str:
    if not due_ts:
        return "без срока"
    return datetime.fromtimestamp(float(due_ts), config.TZ).strftime("%d.%m %H:%M")


def _best_text(group) -> str:
    best = max(
        group,
        key=lambda t: (len(str(t["text"] or "")), float(t["created_ts"] or 0)),
    )
    return str(best["text"] or "")


def _best_due(group, now: float):
    dues = [float(t["due_ts"]) for t in group if t["due_ts"]]
    if not dues:
        return None
    future = [d for d in dues if d >= now]
    if future:
        return min(future)
    return max(dues)


async def _backup_db(db: DB) -> Path:
    source = Path(config.DB_PATH)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    target = source.with_name(f"{source.stem}-before-dedupe-{stamp}{source.suffix}")
    dst = await aiosqlite.connect(target)
    try:
        await db.conn.backup(dst)
    finally:
        await dst.close()
    os.chmod(target, 0o600)
    return target


async def main() -> int:
    parser = argparse.ArgumentParser(description="Поиск и слияние дублей открытых задач")
    parser.add_argument("--chat", type=int, help="только один chat_id")
    parser.add_argument("--json", action="store_true", help="вывести JSON для разбора")
    parser.add_argument("--apply", action="store_true", help="применить слияние")
    parser.add_argument("--yes", action="store_true", help="подтвердить --apply без вопроса")
    args = parser.parse_args()

    if args.apply and args.json:
        parser.error("--apply и --json несовместимы")
    if args.apply and not args.yes:
        parser.error("для изменения базы добавь --yes")

    db = DB(config.DB_PATH)
    await db.open()
    try:
        tasks = list(await db.open_tasks())
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
        print("Режим: " + ("APPLY — база будет изменена" if args.apply else "dry-run — база не изменяется"))
        print()

        if not groups:
            print("Похожих задач не найдено.")
            return 0

        now = time.time()
        for number, group in enumerate(groups, 1):
            ordered = sorted(group, key=lambda row: int(row["id"]))
            title = _one_line(ordered[0]["chat_title"], 100)
            print(f"Группа {number} · чат «{title}» · задач: {len(ordered)}")
            for task in ordered:
                print(f"  #{int(task['id'])} · {_fmt_due(task['due_ts'])} · {_one_line(task['text'])}")
            print()

        if not args.apply:
            print("Ничего не изменено. Для слияния запусти с --apply --yes.")
            return 0

        backup = await _backup_db(db)
        print(f"Резервная копия базы: {backup}\n")

        updated = deleted = 0
        for group in groups:
            keep = min(group, key=lambda t: int(t["id"]))
            due = _best_due(group, now)
            fields = {"text": _best_text(group)}
            if due is not None and due != keep["due_ts"]:
                fields["due_ts"] = due
                fields["reminded"] = 0
            await db.set_task(int(keep["id"]), **fields)
            updated += 1

            removed = []
            for task in group:
                if int(task["id"]) == int(keep["id"]):
                    continue
                await db.delete_task(int(task["id"]))
                deleted += 1
                removed.append(f"#{int(task['id'])}")
            print(f"#{int(keep['id'])} ← удалены {', '.join(removed)} · срок {_fmt_due(due)}")

        remaining = len(await db.open_tasks())
        print(f"\nГотово: обновлено {updated}, удалено {deleted}, осталось открытых {remaining}.")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
