import time

import aiosqlite

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS connections(
    id TEXT PRIMARY KEY, user_id INTEGER, enabled INTEGER, can_reply INTEGER, updated REAL);

CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conn_id TEXT, chat_id INTEGER, msg_id INTEGER, chat_title TEXT, sender TEXT,
    from_owner INTEGER, by_bot INTEGER DEFAULT 0,
    text TEXT, ts REAL, processed INTEGER DEFAULT 0, importance TEXT,
    UNIQUE(chat_id, msg_id));
CREATE INDEX IF NOT EXISTS ix_msg_chat_ts ON messages(chat_id, ts);
CREATE INDEX IF NOT EXISTS ix_msg_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER, chat_title TEXT, text TEXT, due_ts REAL,
    done INTEGER DEFAULT 0, reminded INTEGER DEFAULT 0, created_ts REAL);

CREATE TABLE IF NOT EXISTS drafts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conn_id TEXT, chat_id INTEGER, chat_title TEXT, text TEXT,
    status TEXT DEFAULT 'pending', created_ts REAL);

-- собеседники: status = '' | 'vip' | 'ignore'
CREATE TABLE IF NOT EXISTS contacts(
    chat_id INTEGER PRIMARY KEY, name TEXT, username TEXT, status TEXT DEFAULT '');

-- готовые ответы / факты владельца
CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, created_ts REAL);

-- о каком последнем сообщении уже напоминали «вы не ответили»
CREATE TABLE IF NOT EXISTS nudges(chat_id INTEGER PRIMARY KEY, msg_row_id INTEGER);

-- как владелец правил черновики (для обучения стилю)
CREATE TABLE IF NOT EXISTS style_examples(
    id INTEGER PRIMARY KEY AUTOINCREMENT, draft TEXT, final TEXT, ts REAL);

-- встречи, найденные в переписке
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, chat_title TEXT,
    title TEXT, start_ts REAL, end_ts REAL, place TEXT, created_ts REAL);

-- кому уже ответили в режиме «нет на связи» (away_id — метка периода)
CREATE TABLE IF NOT EXISTS away_replied(chat_id INTEGER PRIMARY KEY, away_id TEXT);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self):
        import os

        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    # ---------- settings ----------
    async def get(self, key: str) -> str:
        async with self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else config.DEFAULT_SETTINGS.get(key, "")

    async def set(self, key: str, value: str):
        await self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        await self.conn.commit()

    # ---------- business connections ----------
    async def save_connection(self, conn_id: str, user_id: int, enabled: bool, can_reply: bool):
        await self.conn.execute(
            "INSERT INTO connections(id, user_id, enabled, can_reply, updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id, enabled=excluded.enabled, "
            "can_reply=excluded.can_reply, updated=excluded.updated",
            (conn_id, user_id, int(enabled), int(can_reply), time.time()),
        )
        await self.conn.commit()

    async def get_connection(self, conn_id: str):
        async with self.conn.execute("SELECT * FROM connections WHERE id=?", (conn_id,)) as cur:
            return await cur.fetchone()

    # ---------- messages ----------
    async def add_message(self, *, conn_id, chat_id, msg_id, chat_title, sender,
                          from_owner, by_bot, text, ts, processed=0) -> bool:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO messages(conn_id, chat_id, msg_id, chat_title, sender, "
            "from_owner, by_bot, text, ts, processed) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (conn_id, chat_id, msg_id, chat_title, sender, int(from_owner), int(by_bot),
             text, ts, processed),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def update_message_text(self, chat_id: int, msg_id: int, text: str):
        await self.conn.execute(
            "UPDATE messages SET text=? WHERE chat_id=? AND msg_id=?", (text, chat_id, msg_id))
        await self.conn.commit()

    async def history(self, chat_id: int, limit: int):
        async with self.conn.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY ts DESC, id DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return list(reversed(rows))

    async def unprocessed(self, chat_id: int):
        async with self.conn.execute(
            "SELECT * FROM messages WHERE chat_id=? AND processed=0 ORDER BY ts, id", (chat_id,)
        ) as cur:
            return await cur.fetchall()

    async def mark_processed(self, ids: list[int], importance: str | None):
        if not ids:
            return
        q = ",".join("?" * len(ids))
        await self.conn.execute(
            f"UPDATE messages SET processed=1, importance=? WHERE id IN ({q})", (importance, *ids))
        await self.conn.commit()

    async def last_owner_activity(self, chat_id: int) -> float:
        """Когда владелец сам (не бот) последний раз писал в чат."""
        async with self.conn.execute(
            "SELECT MAX(ts) AS t FROM messages WHERE chat_id=? AND from_owner=1 AND by_bot=0",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        return row["t"] or 0

    async def bot_replies_since(self, chat_id: int, since_ts: float) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id=? AND by_bot=1 AND ts>=?",
            (chat_id, since_ts),
        ) as cur:
            return (await cur.fetchone())["n"]

    async def messages_since(self, since_ts: float):
        async with self.conn.execute(
            "SELECT * FROM messages WHERE ts>=? AND by_bot=0 ORDER BY chat_id, ts", (since_ts,)
        ) as cur:
            return await cur.fetchall()

    # ---------- tasks ----------
    async def add_task(self, chat_id, chat_title, text, due_ts) -> int:
        cur = await self.conn.execute(
            "INSERT INTO tasks(chat_id, chat_title, text, due_ts, created_ts) VALUES(?,?,?,?,?)",
            (chat_id, chat_title, text, due_ts, time.time()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def open_tasks(self):
        async with self.conn.execute(
            "SELECT * FROM tasks WHERE done=0 "
            "ORDER BY due_ts IS NULL, due_ts, created_ts"
        ) as cur:
            return await cur.fetchall()

    async def due_tasks(self, now_ts: float):
        async with self.conn.execute(
            "SELECT * FROM tasks WHERE done=0 AND reminded=0 AND due_ts IS NOT NULL AND due_ts<=?",
            (now_ts,),
        ) as cur:
            return await cur.fetchall()

    async def get_task(self, task_id: int):
        async with self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)) as cur:
            return await cur.fetchone()

    async def delete_task(self, task_id: int):
        await self.conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        await self.conn.commit()

    async def set_task(self, task_id: int, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))
        await self.conn.commit()

    # ---------- drafts ----------
    async def add_draft(self, conn_id, chat_id, chat_title, text) -> int:
        cur = await self.conn.execute(
            "INSERT INTO drafts(conn_id, chat_id, chat_title, text, created_ts) VALUES(?,?,?,?,?)",
            (conn_id, chat_id, chat_title, text, time.time()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_draft(self, draft_id: int):
        async with self.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)) as cur:
            return await cur.fetchone()

    async def set_draft_status(self, draft_id: int, status: str):
        await self.conn.execute("UPDATE drafts SET status=? WHERE id=?", (status, draft_id))
        await self.conn.commit()

    async def mark_pending_voice(self, chat_id: int, msg_id: int, processed: int):
        await self.conn.execute(
            "UPDATE messages SET processed=? WHERE chat_id=? AND msg_id=?", (processed, chat_id, msg_id))
        await self.conn.commit()

    # ---------- contacts ----------
    async def upsert_contact(self, chat_id: int, name: str, username: str | None):
        await self.conn.execute(
            "INSERT INTO contacts(chat_id, name, username) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name, username=excluded.username",
            (chat_id, name, username or ""),
        )
        await self.conn.commit()

    async def contact(self, chat_id: int):
        async with self.conn.execute("SELECT * FROM contacts WHERE chat_id=?", (chat_id,)) as cur:
            return await cur.fetchone()

    async def contact_status(self, chat_id: int) -> str:
        row = await self.contact(chat_id)
        return (row["status"] or "") if row else ""

    async def set_contact_status(self, chat_id: int, status: str):
        await self.conn.execute("UPDATE contacts SET status=? WHERE chat_id=?", (status, chat_id))
        await self.conn.commit()

    async def all_contacts(self):
        async with self.conn.execute("SELECT * FROM contacts ORDER BY name") as cur:
            return await cur.fetchall()

    # ---------- facts ----------
    async def add_fact(self, text: str) -> int:
        cur = await self.conn.execute("INSERT INTO facts(text, created_ts) VALUES(?,?)", (text, time.time()))
        await self.conn.commit()
        return cur.lastrowid

    async def facts(self):
        async with self.conn.execute("SELECT * FROM facts ORDER BY id") as cur:
            return await cur.fetchall()

    async def delete_fact(self, fact_id: int):
        await self.conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))
        await self.conn.commit()

    # ---------- unanswered ----------
    async def waiting_chats(self, older_than_ts: float, newer_than_ts: float):
        """Чаты, где последнее (не от бота) сообщение — от собеседника."""
        async with self.conn.execute(
            "SELECT m.*, n.msg_row_id AS nudged FROM messages m "
            "JOIN (SELECT chat_id, MAX(id) AS mid FROM messages WHERE by_bot=0 GROUP BY chat_id) x "
            "  ON m.id = x.mid "
            "LEFT JOIN nudges n ON n.chat_id = m.chat_id "
            "WHERE m.from_owner=0 AND m.ts<=? AND m.ts>=? ORDER BY m.ts",
            (older_than_ts, newer_than_ts),
        ) as cur:
            return await cur.fetchall()

    async def set_nudged(self, chat_id: int, msg_row_id: int):
        await self.conn.execute(
            "INSERT INTO nudges(chat_id, msg_row_id) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET msg_row_id=excluded.msg_row_id", (chat_id, msg_row_id))
        await self.conn.commit()

    # ---------- style ----------
    async def add_style_example(self, draft: str, final: str):
        await self.conn.execute("INSERT INTO style_examples(draft, final, ts) VALUES(?,?,?)",
                                (draft, final, time.time()))
        await self.conn.commit()

    async def style_examples(self, limit: int = 6):
        async with self.conn.execute(
            "SELECT * FROM style_examples ORDER BY id DESC LIMIT ?", (limit,)) as cur:
            return list(reversed(await cur.fetchall()))

    # ---------- events ----------
    async def add_event(self, chat_id, chat_title, title, start_ts, end_ts, place) -> int | None:
        """Добавляет встречу; None — если такая (тот же чат и время) уже есть."""
        async with self.conn.execute(
            "SELECT id FROM events WHERE chat_id=? AND ABS(start_ts-?)<60", (chat_id, start_ts)) as c:
            if await c.fetchone():
                return None
        cur = await self.conn.execute(
            "INSERT INTO events(chat_id, chat_title, title, start_ts, end_ts, place, created_ts) "
            "VALUES(?,?,?,?,?,?,?)", (chat_id, chat_title, title, start_ts, end_ts, place, time.time()))
        await self.conn.commit()
        return cur.lastrowid

    async def get_event(self, event_id: int):
        async with self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)) as cur:
            return await cur.fetchone()

    # ---------- away ----------
    async def away_replied_to(self, chat_id: int, away_id: str) -> bool:
        async with self.conn.execute("SELECT away_id FROM away_replied WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
        return bool(row) and row["away_id"] == away_id

    async def set_away_replied(self, chat_id: int, away_id: str):
        await self.conn.execute(
            "INSERT INTO away_replied(chat_id, away_id) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET away_id=excluded.away_id", (chat_id, away_id))
        await self.conn.commit()

    # ---------- search ----------
    async def messages_between(self, since_ts: float, chat_ids: list[int] | None = None):
        q = "SELECT * FROM messages WHERE ts>=? AND by_bot=0"
        args: list = [since_ts]
        if chat_ids:
            q += f" AND chat_id IN ({','.join('?' * len(chat_ids))})"
            args += chat_ids
        async with self.conn.execute(q + " ORDER BY chat_id, ts", args) as cur:
            return await cur.fetchall()

    async def expire_drafts(self, chat_id: int):
        """Владелец сам ответил в чате — старые черновики больше не нужны."""
        await self.conn.execute(
            "UPDATE drafts SET status='expired' WHERE chat_id=? AND status='pending'", (chat_id,))
        await self.conn.commit()


db = DB(config.DB_PATH)
