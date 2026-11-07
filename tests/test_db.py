import os
import tempfile
import time
import unittest

# config.py требует ключи ещё при импорте — тестам достаточно заглушек.
os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("GIGACHAT_AUTH_KEY", "test-key")
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "secretary-test.db"))

from bot.db import DB, MIGRATIONS  # noqa: E402


class DBTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db", prefix="secretary-test-")
        os.close(fd)
        self.db = DB(self.path)
        await self.db.open()

    async def asyncTearDown(self):
        await self.db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass

    async def add(self, chat_id, msg_id, text, *, owner=0, by_bot=0, processed=1,
                  ts=None, conn="conn1"):
        await self.db.add_message(
            conn_id=conn, chat_id=chat_id, msg_id=msg_id, chat_title=f"Chat {chat_id}",
            sender="Petya", from_owner=owner, by_bot=by_bot, text=text,
            ts=time.time() if ts is None else ts, processed=processed)

    async def test_auto_reply_is_not_waiting(self):
        now = time.time()
        await self.add(1, 1, "привет", ts=now - 100)
        await self.add(1, 2, "автоответ", owner=1, by_bot=1, ts=now - 50)

        waits = {r["chat_id"] for r in await self.db.waiting_chats(now, now - 86400)}
        self.assertNotIn(1, waits)
        chats = {r["chat_id"]: r for r in await self.db.chats_since(now - 3600)}
        self.assertEqual(chats[1]["last_from_owner"], 1)
        self.assertEqual(chats[1]["by_bot"], 1)
        rows = await self.db.messages_since(now - 3600)
        self.assertTrue(any(r["by_bot"] == 1 for r in rows))

    async def test_recovery_cutoff_and_pending_voice(self):
        now = time.time()
        await self.add(10, 1, "old", processed=0, ts=now - 1000)
        cutoff = await self.db.max_message_id()
        await self.add(10, 2, "new", processed=0, ts=now + 1000)
        rows = await self.db.chats_with_unprocessed(cutoff)
        self.assertEqual([(r["chat_id"], r["n"]) for r in rows], [(10, 1)])

        await self.add(11, 1, "[голосовое 0:05] old", processed=2, ts=now - 1000)
        voice_cutoff = await self.db.max_message_id()
        await self.add(11, 2, "[голосовое 0:05] new", processed=2, ts=now + 1000)
        self.assertEqual(await self.db.reset_pending_voice(voice_cutoff), 1)
        states = {r["msg_id"]: r["processed"] for r in await self.db.history(11, 10)}
        self.assertEqual(states, {1: 0, 2: 2})

    async def test_forget_chat_removes_history(self):
        now = time.time()
        await self.add(20, 1, "секрет", ts=now)
        await self.db.add_task(20, "Chat 20", "позвонить", None)
        await self.db.add_draft("conn1", 20, "Chat 20", "черновик")
        await self.db.upsert_contact(20, "Petya", "petya")
        await self.db.set_contact_status(20, "ignore")

        await self.db.forget_chat(20)
        self.assertEqual(await self.db.history(20, 10), [])
        self.assertEqual(await self.db.open_tasks(), [])
        self.assertIsNone(await self.db.get_draft(1))
        # контакт и его статус остаются, чтобы бот помнил «не трогать»
        self.assertEqual(await self.db.contact_status(20), "ignore")
        contact = await self.db.contact(20)
        self.assertEqual(contact["name"], "")
        self.assertEqual(contact["username"], "")

    async def test_ignored_contact_history_is_hidden(self):
        now = time.time()
        await self.add(21, 1, "старое сообщение", ts=now)
        await self.db.upsert_contact(21, "Petya", "petya")
        await self.db.set_contact_status(21, "ignore")

        self.assertEqual(await self.db.messages_since(now - 3600), [])
        self.assertEqual(await self.db.messages_between(now - 3600), [])
        self.assertEqual(await self.db.chats_since(now - 3600), [])
        self.assertEqual(await self.db.waiting_chats(now, now - 86400), [])

    async def test_cleanup_keeps_recent(self):
        now = time.time()
        await self.add(30, 1, "старое", ts=now - 400 * 86400)
        await self.add(30, 2, "новое", ts=now - 10)
        await self.db.add_task(30, "Chat 30", "старая задача", None)
        await self.db.conn.execute("UPDATE tasks SET done=1, created_ts=? WHERE chat_id=30",
                                   (now - 400 * 86400,))
        await self.db.conn.commit()

        counts = await self.db.cleanup(now - 365 * 86400)
        self.assertEqual(counts["messages"], 1)
        texts = [r["text"] for r in await self.db.history(30, 10)]
        self.assertEqual(texts, ["новое"])

    async def test_migrations_and_wal(self):
        async with self.db.conn.execute("PRAGMA user_version") as cur:
            version = (await cur.fetchone())[0]
        self.assertEqual(version, max(MIGRATIONS))
        async with self.db.conn.execute("PRAGMA journal_mode") as cur:
            mode = (await cur.fetchone())[0].lower()
        self.assertEqual(mode, "wal")
        async with self.db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_msg_chat_processed'"
        ) as cur:
            self.assertIsNotNone(await cur.fetchone())


if __name__ == "__main__":
    unittest.main()
