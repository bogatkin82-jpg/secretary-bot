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

-- список покупок
CREATE TABLE IF NOT EXISTS shopping(id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT, created_ts REAL);

-- дни рождения (из переписки, профиля Telegram или добавленные вручную)
CREATE TABLE IF NOT EXISTS birthdays(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, chat_id INTEGER,
    month INTEGER, day INTEGER, source TEXT, created_ts REAL);

-- шаблонные ответы: ключевые слова -> готовый текст
CREATE TABLE IF NOT EXISTS templates(
    id INTEGER PRIMARY KEY AUTOINCREMENT, keywords TEXT, answer TEXT, created_ts REAL);
-- чаты, которые вы отметили «уже ответил» (по телефону, лично и т.п.)
CREATE TABLE IF NOT EXISTS handled(chat_id INTEGER PRIMARY KEY, ts REAL);

CREATE TABLE IF NOT EXISTS template_sent(chat_id INTEGER, tpl_id INTEGER, ts REAL,
    PRIMARY KEY(chat_id, tpl_id));
"""

# Версия схемы и миграции для уже существующих баз.
# Добавляя поле/индекс, создай новый номер и SQL здесь: старые базы догонят.
MIGRATIONS: dict[int, str] = {
    1: "CREATE INDEX IF NOT EXISTS ix_msg_chat_processed ON messages(chat_id, processed);",
}
SCHEMA_VERSION = max(MIGRATIONS)


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self):
        import os

        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self):
        async with self.conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        version = int(row[0]) if row else 0
        for target in sorted(MIGRATIONS):
            if version >= target:
                continue
            await self.conn.executescript(MIGRATIONS[target])
            await self.conn.execute(f"PRAGMA user_version={int(target)}")
            version = target

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

    async def update_message_text(self, chat_id: int, msg_id: int, text: str, conn_id: str):
        # conn_id обязателен: у одного и того же собеседника может быть несколько
        # business-подключений, и править чужую строку с тем же msg_id нельзя.
        await self.conn.execute(
            "UPDATE messages SET text=? WHERE chat_id=? AND msg_id=? AND conn_id=?",
            (text, chat_id, msg_id, conn_id))
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

    async def max_message_id(self) -> int:
        """Максимальный id сообщения — граница «до старта процесса»."""
        async with self.conn.execute("SELECT COALESCE(MAX(id), 0) AS mid FROM messages") as cur:
            return (await cur.fetchone())["mid"]

    async def chats_with_unprocessed(self, max_id: int | None = None):
        """Чаты, где остались сообщения после сбоя/перезапуска: для восстановления.

        max_id ограничивает выборку сообщениями, пришедшими до старта процесса,
        чтобы не обрабатывать в режиме восстановления только что пришедшие.
        """
        q = ("SELECT chat_id, COUNT(*) AS n, MIN(ts) AS first_ts "
             "FROM messages WHERE processed=0")
        args: tuple = ()
        if max_id is not None:
            q += " AND id <= ?"
            args = (max_id,)
        q += " GROUP BY chat_id ORDER BY first_ts"
        async with self.conn.execute(q, args) as cur:
            return await cur.fetchall()

    async def reset_pending_voice(self, max_id: int) -> int:
        """Голосовое осталось в статусе «расшифровывается» после рестарта.

        Файл для повторной расшифровки в БД не хранится, поэтому возвращаем
        сообщение в обычную обработку — разберём хотя бы его текст-заглушку,
        вместо того чтобы навсегда оставить его необработанным. Сообщения,
        пришедшие уже после старта процесса (id > max_id), не трогаем:
        их ещё расшифровывают.
        """
        cur = await self.conn.execute(
            "UPDATE messages SET processed=0 WHERE processed=2 AND id <= ?", (max_id,))
        await self.conn.commit()
        return cur.rowcount

    async def mark_processed(self, ids: list[int], importance: str | None):
        if not ids:
            return
        q = ",".join("?" * len(ids))
        await self.conn.execute(
            f"UPDATE messages SET processed=1, importance=? WHERE id IN ({q})", (importance, *ids))
        await self.conn.commit()

    async def owner_samples(self, chat_id: int, limit: int = 20) -> list[str]:
        """Последние сообщения, которые владелец написал САМ: образец его манеры.
        Сначала из этого чата, затем из остальных."""
        q = ("SELECT text FROM messages WHERE from_owner=1 AND by_bot=0 AND text NOT LIKE '[%' "
             "AND LENGTH(text) BETWEEN 2 AND 300 {cond} ORDER BY ts DESC LIMIT ?")
        async with self.conn.execute(q.format(cond="AND chat_id=?"), (chat_id, limit // 2)) as cur:
            own = [r["text"] for r in await cur.fetchall()]
        async with self.conn.execute(q.format(cond="AND chat_id<>?"), (chat_id, limit - len(own))) as cur:
            other = [r["text"] for r in await cur.fetchall()]
        return own + other

    async def chat_info(self, chat_id: int):
        """conn_id и название чата по последнему сообщению — чтобы ответить в него."""
        async with self.conn.execute(
            "SELECT conn_id, chat_title FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 1",
            (chat_id,)) as cur:
            return await cur.fetchone()

    async def set_handled(self, chat_id: int):
        await self.conn.execute(
            "INSERT INTO handled(chat_id, ts) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET ts=excluded.ts",
            (chat_id, time.time()))
        await self.conn.commit()

    async def handled_ts(self, chat_id: int) -> float:
        async with self.conn.execute("SELECT ts FROM handled WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
        return row["ts"] if row else 0.0

    async def forget_chat(self, chat_id: int):
        """Удалить всё содержимое чата, когда владелец ставит «не трогать»."""
        for table in ("messages", "drafts", "tasks", "events", "nudges",
                      "handled", "away_replied", "template_sent", "birthdays"):
            await self.conn.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))
        # Оставляем только chat_id и статус: имя и @ник — тоже персональные данные.
        # Вернуть контакт можно кнопкой в /contacts.
        await self.conn.execute("UPDATE contacts SET name='', username='' WHERE chat_id=?", (chat_id,))
        await self.conn.commit()

    async def chats_since(self, since_ts: float):
        """Чаты с сообщениями собеседников за период: (chat_id, название, ждёт ли ответа).

        Учитываем и автоответы бота (by_bot=1): если последним писал бот,
        человек не ждёт ответа и кнопка «⏳» не нужна.
        """
        async with self.conn.execute(
            "SELECT m.chat_id, m.chat_title, m.ts, m.by_bot, "
            "  CASE WHEN m.from_owner=1 OR (h.ts IS NOT NULL AND h.ts>=m.ts) THEN 1 ELSE 0 END AS last_from_owner "
            "FROM messages m "
            "JOIN (SELECT chat_id, MAX(id) AS mid FROM messages GROUP BY chat_id) x "
            "  ON m.id = x.mid "
            "LEFT JOIN handled h ON h.chat_id = m.chat_id "
            "WHERE m.chat_id IN (SELECT DISTINCT chat_id FROM messages WHERE ts>=? AND from_owner=0) "
            "AND m.chat_id NOT IN (SELECT chat_id FROM contacts WHERE status='ignore') "
            "ORDER BY last_from_owner, m.ts DESC", (since_ts,)) as cur:
            return await cur.fetchall()

    async def last_owner_activity(self, chat_id: int) -> float:
        """Когда владелец сам (не бот) последний раз писал в чат."""
        async with self.conn.execute(
            "SELECT MAX(ts) AS t FROM messages WHERE chat_id=? AND from_owner=1 AND by_bot IN (0, 2)",
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
        # Автоответы бота тоже нужны: по ним сводка понимает, что чат уже отвечен.
        async with self.conn.execute(
            "SELECT * FROM messages WHERE ts>=? "
            "AND chat_id NOT IN (SELECT chat_id FROM contacts WHERE status='ignore') "
            "ORDER BY chat_id, ts, id", (since_ts,)
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
        """Чаты, где последнее сообщение — от собеседника (бот автоответом не считается)."""
        async with self.conn.execute(
            "SELECT m.*, n.msg_row_id AS nudged FROM messages m "
            "JOIN (SELECT chat_id, MAX(id) AS mid FROM messages GROUP BY chat_id) x "
            "  ON m.id = x.mid "
            "LEFT JOIN nudges n ON n.chat_id = m.chat_id "
            "LEFT JOIN handled h ON h.chat_id = m.chat_id "
            "WHERE m.from_owner=0 AND m.ts<=? AND m.ts>=? AND (h.ts IS NULL OR h.ts < m.ts) "
            "AND m.chat_id NOT IN (SELECT chat_id FROM contacts WHERE status='ignore') ORDER BY m.ts",
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
        q = ("SELECT * FROM messages WHERE ts>=? AND by_bot IN (0, 2) "
             "AND chat_id NOT IN (SELECT chat_id FROM contacts WHERE status='ignore')")
        args: list = [since_ts]
        if chat_ids:
            q += f" AND chat_id IN ({','.join('?' * len(chat_ids))})"
            args += chat_ids
        async with self.conn.execute(q + " ORDER BY chat_id, ts", args) as cur:
            return await cur.fetchall()

    # ---------- покупки ----------
    async def add_shopping(self, items: list[str]):
        now = time.time()
        await self.conn.executemany("INSERT INTO shopping(item, created_ts) VALUES(?,?)",
                                    [(i, now) for i in items])
        await self.conn.commit()

    async def shopping(self):
        async with self.conn.execute("SELECT * FROM shopping ORDER BY id") as cur:
            return await cur.fetchall()

    async def delete_shopping(self, item_id: int | None = None):
        if item_id is None:
            await self.conn.execute("DELETE FROM shopping")
        else:
            await self.conn.execute("DELETE FROM shopping WHERE id=?", (item_id,))
        await self.conn.commit()

    # ---------- ретеншен ----------
    async def cleanup(self, before_ts: float) -> dict[str, int]:
        """Удалить переписку и служебные записи старше before_ts."""
        counts: dict[str, int] = {}
        for table, column in (("messages", "ts"), ("drafts", "created_ts"),
                              ("events", "start_ts"), ("handled", "ts"),
                              ("template_sent", "ts"), ("style_examples", "ts")):
            cur = await self.conn.execute(
                f"DELETE FROM {table} WHERE {column} < ?", (before_ts,))
            counts[table] = max(cur.rowcount, 0)
        cur = await self.conn.execute(
            "DELETE FROM tasks WHERE done=1 AND created_ts < ?", (before_ts,))
        counts["tasks_done"] = max(cur.rowcount, 0)
        cur = await self.conn.execute(
            "DELETE FROM nudges WHERE msg_row_id NOT IN (SELECT id FROM messages)")
        counts["nudges"] = max(cur.rowcount, 0)
        await self.conn.commit()
        return counts

    # ---------- дни рождения ----------
    async def add_birthday(self, name: str, month: int, day: int, chat_id: int | None,
                           source: str) -> int | None:
        """None — такой день рождения уже записан."""
        async with self.conn.execute(
            "SELECT id FROM birthdays WHERE month=? AND day=? AND "
            "((chat_id IS NOT NULL AND chat_id=?) OR LOWER(name)=LOWER(?))",
            (month, day, chat_id, name)) as cur:
            if await cur.fetchone():
                return None
        cur = await self.conn.execute(
            "INSERT INTO birthdays(name, chat_id, month, day, source, created_ts) VALUES(?,?,?,?,?,?)",
            (name, chat_id, month, day, source, time.time()))
        await self.conn.commit()
        return cur.lastrowid

    async def birthdays(self):
        async with self.conn.execute("SELECT * FROM birthdays ORDER BY month, day") as cur:
            return await cur.fetchall()

    async def birthdays_on(self, month: int, day: int):
        async with self.conn.execute(
            "SELECT * FROM birthdays WHERE month=? AND day=?", (month, day)) as cur:
            return await cur.fetchall()

    async def delete_birthday(self, bday_id: int):
        await self.conn.execute("DELETE FROM birthdays WHERE id=?", (bday_id,))
        await self.conn.commit()

    # ---------- шаблоны ----------
    async def add_template(self, keywords: str, answer: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO templates(keywords, answer, created_ts) VALUES(?,?,?)",
            (keywords, answer, time.time()))
        await self.conn.commit()
        return cur.lastrowid

    async def templates(self):
        async with self.conn.execute("SELECT * FROM templates ORDER BY id") as cur:
            return await cur.fetchall()

    async def delete_template(self, tpl_id: int):
        await self.conn.execute("DELETE FROM templates WHERE id=?", (tpl_id,))
        await self.conn.execute("DELETE FROM template_sent WHERE tpl_id=?", (tpl_id,))
        await self.conn.commit()

    async def template_sent_ts(self, chat_id: int, tpl_id: int) -> float:
        async with self.conn.execute(
            "SELECT ts FROM template_sent WHERE chat_id=? AND tpl_id=?", (chat_id, tpl_id)) as cur:
            row = await cur.fetchone()
        return row["ts"] if row else 0.0

    async def mark_template_sent(self, chat_id: int, tpl_id: int):
        await self.conn.execute(
            "INSERT INTO template_sent(chat_id, tpl_id, ts) VALUES(?,?,?) "
            "ON CONFLICT(chat_id, tpl_id) DO UPDATE SET ts=excluded.ts", (chat_id, tpl_id, time.time()))
        await self.conn.commit()

    async def expire_drafts(self, chat_id: int):
        """Владелец сам ответил в чате — старые черновики больше не нужны."""
        await self.conn.execute(
            "UPDATE drafts SET status='expired' WHERE chat_id=? AND status='pending'", (chat_id,))
        await self.conn.commit()


db = DB(config.DB_PATH)
