from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from capri_di.capri import Capri, schedule, time_quiet
from capri_di.context import conversation_context, recall
from capri_di.config import Config
from capri_di.main import handle_update
from capri_di.network import RemoteError
from capri_di.store import Store
from capri_di.worker import Worker, error_result
from test_core import config


class Provider:
    def __init__(self, answer="Поняла."):
        self.answer = answer
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append((messages, tools))
        return {"content": self.answer}


class MemoryTodoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.config = config(self.temp.name)
        self.provider = Provider()
        self.capri = Capri(self.config, self.store, self.provider)
        self.now = datetime.now(ZoneInfo("Europe/Moscow"))

    def finish_job(self, kind="lms", records=None):
        job_id = self.store.enqueue(kind, "Учебные задачи", "owner", "result:" + str(len(self.store.jobs())))
        claimed = self.store.claim_job()
        self.assertEqual(claimed["id"], job_id)
        result = error_result("completed", "Observed source data")
        result["collected_at"] = self.now.isoformat()
        result["records"] = records or []
        self.store.finish(job_id, result)
        return self.store.job(job_id)

    def test_legacy_style_migrates_once_and_does_not_resurrect(self):
        with self.store.connect() as db:
            db.execute("DELETE FROM settings WHERE key='memory_migrated'")
            db.execute("INSERT INTO settings VALUES('preference','Без эмодзи')")
        reloaded = Store(self.temp.name)
        self.assertEqual(reloaded.memories(), [{"key": "style", "content": "Без эмодзи"}])
        reloaded.forget("style")
        self.assertEqual(Store(self.temp.name).memories(), [])
        self.assertEqual(reloaded.setting("preference"), "")

    def test_notes_survive_restart_and_updates_only_replace_same_key(self):
        self.capri.tool("remember", {"key": "address", "preference": "Не называть по имени"}, 1)
        self.capri.tool("remember", {"key": "digest", "preference": "Короткие дайджесты"}, 2)
        self.capri.tool("remember", {"key": "digest", "preference": "Ссылки в дайджестах"}, 3)
        self.capri.reply("/style Без эмодзи", 4)
        notes = {r["key"]: r["content"] for r in Store(self.temp.name).memories()}
        self.assertEqual(len(notes), 3)
        self.assertEqual(notes["address"], "Не называть по имени")
        self.assertEqual(notes["digest"], "Ссылки в дайджестах")
        self.capri.reply("/forget digest", 5)
        self.assertNotIn("digest", {r["key"] for r in self.store.memories()})
        self.assertIn("/forget digest", str(self.store.history()))  # Not an erase-history claim.

    def test_memory_budget_failure_does_not_destroy_existing_notes(self):
        for i in range(4):
            self.store.remember(f"note-{i}", "x" * 2000)
        with self.assertRaises(ValueError):
            self.store.remember("overflow", "new")
        self.assertEqual(len(self.store.memories()), 4)

    def test_old_conversation_is_summarized_and_recent_messages_retained(self):
        self.store.set_setting("conversation_summary", "Раньше обсуждали NLP.")
        for i in range(64):
            self.store.remember_message("user" if i % 2 == 0 else "assistant", str(i))
        self.provider.answer = "Уточнение пользователя: домашка NLP, не RecSys. Пока не сдавать."
        self.capri.summarize_history()
        self.assertIn("не RecSys", Store(self.temp.name).setting("conversation_summary"))
        messages, tools = self.provider.calls[0]
        self.assertIsNone(tools)
        self.assertIn("Раньше обсуждали NLP", messages[1]["content"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 44)
            self.assertEqual(db.execute("SELECT content FROM messages ORDER BY id LIMIT 1").fetchone()[0], "20")
        self.assertEqual(len(self.store.history()), 40)

    def test_summary_failure_retains_original_messages_and_reply_still_works(self):
        for i in range(62):
            self.store.remember_message("user", str(i))
        self.store.set_setting("conversation_summary", "Previous summary")
        with patch.object(self.provider, "chat", side_effect=RemoteError("OpenRouter")):
            self.capri.summarize_history()
        self.assertEqual(self.store.setting("conversation_summary"), "Previous summary")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 62)
        self.assertEqual(self.capri.reply("Ты тут?", 9), "Поняла.")
        self.assertEqual(len(self.provider.calls), 1)  # Failed summary is not retried for each reply.

    def test_summary_input_is_bounded_without_discarding_unread_messages(self):
        for _ in range(64):
            self.store.remember_message("user", "x" * 12000)
        batch = self.store.summary_batch()
        self.assertEqual(len(batch), 2)
        self.store.save_summary("Short summary", batch[-1]["id"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 62)

    def test_commands_are_remembered_and_invented_commands_never_reach_model(self):
        for n, command in enumerate(("/delegate", "/remember test", "/shell ls")):
            self.assertIn("Такой команды", self.capri.reply(command, n))
        self.assertFalse(self.provider.calls)
        self.assertFalse(self.store.jobs())
        self.capri.reply("/digest", 10)
        self.assertIn("/digest", str(self.store.history()))
        self.assertEqual(len(self.store.jobs()), 1)

    def test_followup_receives_exact_delivered_report_not_unsent_draft(self):
        job = self.finish_job()
        text = "1. Agile\n2. NLP — до 22 ноября\nhttps://my.centraluniversity.ru/learn/courses/view/actual/1"
        self.store.queue_text("job:" + job["id"], text)
        self.assertEqual(conversation_context(self.store)["recent_delivered_reports"], [])
        item = self.store.claim_message()
        self.store.delivery(item["id"], "sent", 123)
        self.capri.reply("Что со второй домашкой?", 10)
        supplied = self.provider.calls[-1][0][1]["content"]
        self.assertIn("2. NLP", supplied)
        self.assertIn("sent_at", supplied)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_recall_searches_late_records_and_filters_obsolete_tasks(self):
        record = {"source": "lms", "course": "NLP", "title": "Домашка", "status": "Задано", "due_at": "22 ноября",
                  "url": "https://my.centraluniversity.ru/learn/courses/view/actual/1"}
        records = [{**record, "title": "Обычная домашка " + str(i) + " x" * 500} for i in range(30)]
        records += [{**record, "title": "Уникальный трансформер"}, {**record, "title": "Архивный секрет", "url": "https://my.centraluniversity.ru/learn/courses/view/archived/1"}]
        job = self.finish_job(records=records)
        found = recall(self.store, "трансформер")
        self.assertEqual(found[0]["id"], job["id"])
        self.assertEqual(found[0]["result"]["records"][0]["title"], "Уникальный трансформер")
        self.assertTrue(found[0]["records_truncated"])
        self.assertEqual(recall(self.store, "Архивный секрет"), [])

    def test_di_receives_fixed_snapshot_even_if_conversation_changes(self):
        self.store.remember_message("user", "Нужна NLP ДЗ 3.2, только подготовить")
        queued = self.capri.delegate("homework", "Подготовь эту домашку https://my.centraluniversity.ru/learn/courses/view/actual/1/themes/2/longreads/3", "snapshot")
        self.store.remember_message("user", "Теперь обсуждаем другое")
        job = self.store.claim_job()
        captured = json.loads(job["context"])
        self.assertIn("NLP ДЗ 3.2", str(captured))
        self.assertNotIn("Теперь обсуждаем", str(captured))
        root = Path(self.temp.name)
        executable = root / "fake-codex"
        executable.write_text("#!/usr/bin/env python3\nimport json,sys,pathlib\np=json.load(sys.stdin)\na=sys.argv\nw=pathlib.Path(a[a.index('--cd')+1])\n(w/'captured.json').write_text(json.dumps(p))\nr=" + repr(error_result("blocked", "Test only")) + "\npathlib.Path(a[a.index('--output-last-message')+1]).write_text(json.dumps(r))\n")
        executable.chmod(0o700)
        result = Worker(self.store, root / "work", str(executable), timeout=10).run(job)
        prompt = json.loads((root / "work" / queued["job_id"] / "captured.json").read_text())
        self.assertEqual(prompt["reference_context_data"], captured)
        self.assertEqual(result["status"], "blocked")

    def test_plain_todo_has_no_implicit_reminder_or_di_job(self):
        self.capri.reply("/todo Подготовить NLP", 1)
        item = self.store.todos()[0]
        self.assertEqual(item["title"], "Подготовить NLP")
        self.assertIsNone(item["remind_at"])
        self.assertFalse(self.store.jobs())
        self.store.schedule_reminders(self.now + timedelta(days=10))
        self.assertIsNone(self.store.claim_message())

    def test_natural_language_tool_creates_reminder_and_preserves_other_memories(self):
        when = (self.now + timedelta(hours=2)).isoformat()
        calls = [{"id": "call1", "type": "function", "function": {"name": "todo", "arguments": json.dumps({"action": "add", "title": "Разобрать ДЗ", "remind_at": when, "repeat_hours": 24})}}]
        with patch.object(self.provider, "chat", side_effect=[{"tool_calls": calls}, {"content": "Записала и напомню."}]):
            answer = self.capri.reply("Напомни через два часа про ДЗ, потом каждые сутки", 100)
        self.assertEqual(answer, "Записала и напомню.")
        todo = Store(self.temp.name).todos()[0]
        self.assertEqual(todo["remind_at"], when)
        self.assertEqual(todo["repeat_hours"], 24)
        self.assertFalse(self.store.jobs())

    def test_duplicate_tool_add_is_idempotent_and_times_require_timezone(self):
        future = (self.now + timedelta(hours=1)).isoformat()
        one = self.capri.tool("todo", {"action": "add", "title": "ДЗ", "remind_at": future}, 1)
        two = self.capri.tool("todo", {"action": "add", "title": "Другая ДЗ", "remind_at": future}, 1)
        self.assertEqual(one["id"], two["id"])
        self.assertEqual(len(self.store.todos()), 1)
        invalid = self.capri.tool("todo", {"action": "add", "title": "Wrong", "remind_at": "2099-01-01T12:00:00"}, 2)
        self.assertIn("error", invalid)
        self.assertEqual(len(self.store.todos()), 1)

    def test_reminders_survive_restart_and_run_without_study_schedules_or_llm(self):
        when = self.now.timestamp() + 60
        self.store.change_todo("add", "reminder", title="Проверить NLP", remind_at=when)
        reloaded = Store(self.temp.name)
        disabled = replace(self.config, schedules=False)
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: schedule(disabled, reloaded, self.now + timedelta(minutes=2)), range(2)))
        notice = reloaded.claim_message()
        self.assertIn("Проверить NLP", notice["text"])
        reloaded.delivery(notice["id"], "sent", 1)
        self.assertIsNone(reloaded.claim_message())
        self.assertIsNone(reloaded.todos()[0]["remind_at"])
        self.assertFalse(self.provider.calls)
        self.assertFalse(reloaded.jobs())

    def test_repeating_reminder_catches_up_once_and_done_cancels_pending_notice(self):
        first = self.now - timedelta(days=5)
        item = self.store.change_todo("add", "repeat", title="Домашка", remind_at=first.timestamp(), repeat_hours=24)
        self.store.schedule_reminders(self.now)
        self.store.schedule_reminders(self.now)
        following = datetime.fromisoformat(self.store.todos()[0]["remind_at"])
        self.assertGreater(following, self.now)
        self.assertLessEqual(following, self.now + timedelta(days=1))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE state='pending'").fetchone()[0], 1)
        self.capri.reply("/done " + item["id"], 2)
        self.assertIsNone(self.store.claim_message())
        self.store.schedule_reminders(self.now + timedelta(days=100))
        self.assertIsNone(self.store.claim_message())
        self.assertEqual(self.store.todos(), [])

    def test_rescheduling_cancels_old_pending_reminder(self):
        item = self.store.change_todo("add", "reschedule", title="ДЗ", remind_at=self.now.timestamp() - 60)
        self.store.schedule_reminders(self.now)
        tomorrow = (self.now + timedelta(days=1)).isoformat()
        self.capri.tool("todo", {"action": "update", "id": item["id"], "remind_at": tomorrow}, 3)
        self.assertIsNone(self.store.claim_message())
        self.assertEqual(self.store.todos()[0]["remind_at"], tomorrow)

    def test_reminder_queue_and_next_time_are_one_transaction(self):
        self.store.change_todo("add", "atomic", title="ДЗ", remind_at=self.now.timestamp() - 1)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_todo_update BEFORE UPDATE ON todos BEGIN SELECT RAISE(ABORT,'simulated crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.schedule_reminders(self.now)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
            db.execute("DROP TRIGGER reject_todo_update")
        self.store.schedule_reminders(self.now)
        self.assertIsNotNone(self.store.claim_message())

    def test_non_owner_cannot_add_todo(self):
        update = {"update_id": 1, "message": {"text": "/todo чужое", "from": {"id": 1}, "chat": {"id": 42, "type": "private"}}}
        handle_update(self.config, self.store, self.capri, update)
        self.assertEqual(self.store.todos(), [])

    def test_daily_digest_hours_and_night_exclusion_across_full_day(self):
        cfg = replace(self.config, proactive_tests=False)
        for hour in range(24):
            now = datetime(2026, 11, 12, hour, 15, tzinfo=ZoneInfo("Europe/Moscow"))
            schedule(cfg, self.store, now)
            schedule(cfg, self.store, now)  # Every polling pass remains idempotent.
        jobs = self.store.jobs(20)
        self.assertEqual(sum(j["kind"] == "lms" for j in jobs), 1)
        self.assertEqual(sum(j["kind"] == "time" for j in jobs), 4)
        self.assertEqual({j["dedup"] for j in jobs}, {
            "lms:2026-11-12T18:00:00+03:00",
            *(f"time:2026-11-12T{h:02}:00:00+03:00" for h in (8, 12, 16, 20))})
        self.assertTrue(time_quiet(cfg, datetime(2026, 11, 12, 4, 59, tzinfo=ZoneInfo("UTC"))))
        self.assertFalse(time_quiet(cfg, datetime(2026, 11, 12, 5, 0, tzinfo=ZoneInfo("UTC"))))

    def test_shipped_schedule_overrides_old_hours_without_changing_test_policy(self):
        schedule_file = Path(__file__).resolve().parents[1] / "deploy/digests.env"
        overrides = dict(line.split("=", 1) for line in schedule_file.read_text().splitlines() if line and not line.startswith("#"))
        with patch.dict("os.environ", {"DIGEST_HOURS": "9,20", "PROACTIVE_TESTS_ENABLED": "false", **overrides}, clear=True):
            cfg = Config.load()
        self.assertEqual(cfg.digest_hours, (18,))
        self.assertTrue(cfg.schedules)
        self.assertTrue(cfg.time_enabled)
        self.assertFalse(cfg.proactive_tests)
        self.assertEqual((cfg.time_interval, cfg.time_quiet_start, cfg.time_quiet_end), (4, 0, 8))

    def test_delayed_scheduled_time_report_waits_until_morning_without_blocking_replies(self):
        scheduled = self.store.enqueue("time", "Read", "schedule", "night")
        manual = self.store.enqueue("time", "Read now", "telegram-owner", "manual")
        self.store.queue_text("job:" + scheduled, "Late scheduled report")
        self.store.queue_text("job:" + manual, "Requested report")
        self.store.queue_text("reply:100", "Ordinary reply")
        self.assertEqual(self.store.claim_message(defer_scheduled_time=True)["text"], "Requested report")
        self.assertEqual(self.store.claim_message(defer_scheduled_time=True)["text"], "Ordinary reply")
        self.assertIsNone(self.store.claim_message(defer_scheduled_time=True))
        self.assertEqual(self.store.claim_message(defer_scheduled_time=False)["text"], "Late scheduled report")


if __name__ == "__main__":
    unittest.main()
