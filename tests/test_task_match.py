import unittest

from bot.task_match import find_open_task, group_similar_tasks, normalize_text


class TaskMatchTest(unittest.TestCase):
    def setUp(self):
        self.tasks = [
            {"id": 12, "chat_id": 5, "text": "Проверить парковку", "done": 0, "due_ts": None},
            {"id": 13, "chat_id": 5, "text": "Купить хлеб", "done": 0, "due_ts": None},
            {"id": 14, "chat_id": 6, "text": "Проверить парковку", "done": 0, "due_ts": None},
            {"id": 15, "chat_id": 5, "text": "Старая задача", "done": 1, "due_ts": None},
        ]

    def test_normalize(self):
        self.assertEqual(normalize_text("  Ёлка,  парковка! "), "елка парковка")

    def test_find_by_id_same_chat(self):
        task = find_open_task(self.tasks, 5, "другая формулировка", 12)
        self.assertEqual(task["id"], 12)

    def test_reject_id_from_other_chat(self):
        task = find_open_task(self.tasks, 5, "совсем другой текст", 14)
        self.assertIsNone(task)

    def test_exact_text(self):
        task = find_open_task(self.tasks, 5, "проверить парковку!")
        self.assertEqual(task["id"], 12)

    def test_containment(self):
        task = find_open_task(self.tasks, 5, "проверить парковку на планке")
        self.assertEqual(task["id"], 12)

    def test_unrelated_is_new(self):
        self.assertIsNone(find_open_task(self.tasks, 5, "позвонить в банк"))

    def test_done_is_ignored(self):
        self.assertIsNone(find_open_task(self.tasks, 5, "старая задача"))

    def test_group_similar_tasks(self):
        tasks = [
            {"id": 1, "chat_id": 5, "text": "Проверить парковку", "done": 0},
            {"id": 2, "chat_id": 5, "text": "Проверить парковку на планке", "done": 0},
            {"id": 3, "chat_id": 5, "text": "Купить хлеб", "done": 0},
            {"id": 4, "chat_id": 6, "text": "Проверить парковку", "done": 0},
        ]
        groups = group_similar_tasks(tasks)
        self.assertEqual(len(groups), 1)
        self.assertEqual({t["id"] for t in groups[0]}, {1, 2})

    def test_group_ignores_done(self):
        tasks = [
            {"id": 1, "chat_id": 5, "text": "Проверить парковку", "done": 1},
            {"id": 2, "chat_id": 5, "text": "Проверить парковку", "done": 0},
        ]
        self.assertEqual(group_similar_tasks(tasks), [])


if __name__ == "__main__":
    unittest.main()
