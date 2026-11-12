from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from capri_di.capri import Capri, schedule
from capri_di.config import Config
from capri_di.main import handle_update
from capri_di.network import post_json, RemoteError
from capri_di.store import Store
from capri_di.worker import safe_artifact, validate_result, error_result, Worker
from capri_di.relevance import digest_result, relevant_record


def config(root):
    return Config(Path(root), 42, "fake", "fake", "fake/model", True, True, True, (18,), 4, 10)


def routed(tool="answer", arguments=None, reply="Поняла."):
    return {"tool_calls": [{"id": "route", "type": "function", "function": {"name": "route_request", "arguments": json.dumps({"tool": tool, "arguments": arguments or {}, "reply": reply})}}]}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.config = config(self.temp.name)

    def test_atomic_claim_and_dedup(self):
        first = self.store.enqueue("lms", "read", "owner", "slot")
        self.assertEqual(first, self.store.enqueue("lms", "read again", "owner", "slot"))
        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda _: self.store.claim_job(), range(2)))
        self.assertEqual(sum(c is not None for c in claims), 1)

    def test_shared_database_is_group_writable_even_with_restrictive_umask(self):
        root = Path(self.temp.name) / "restrictive"
        old = os.umask(0o077)
        try:
            store = Store(root)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(store.db.stat().st_mode), 0o660)
        with store.connect() as db:
            db.execute("INSERT INTO settings VALUES('permissions','checked')")
            for name in ('assistant.db-wal', 'assistant.db-shm'):
                sidecar = root / name
                if sidecar.exists():
                    self.assertTrue(sidecar.stat().st_mode & stat.S_IWGRP, name)

    def test_restart_never_requeues_external_actions(self):
        job = self.store.enqueue("tests", "pass tests", "owner", "tests")
        self.store.claim_job()
        self.store.recover_interrupted()
        self.assertEqual(self.store.job(job)["state"], "unknown")
        self.assertIsNone(self.store.claim_job())

    def test_unknown_telegram_delivery_is_not_retried(self):
        self.store.queue_text("slot", "hello")
        item = self.store.claim_message()
        self.store.delivery(item["id"], "unknown")
        self.assertIsNone(self.store.claim_message())
        self.store.queue_text("slot", "duplicate")
        self.assertIsNone(self.store.claim_message())

    def test_crash_while_sending_keeps_ambiguous_state(self):
        self.store.queue_text("slot", "hello")
        self.store.claim_message()
        reloaded = Store(self.temp.name)
        self.assertIsNone(reloaded.claim_message())

    def test_durable_inbox_and_interrupted_processing(self):
        update = {"update_id": 17, "message": {"text": "/digest"}}
        self.store.receive(update)
        self.store.receive(update)
        self.assertEqual(self.store.setting("telegram_offset"), "18")
        self.assertEqual(self.store.claim_update(), update)
        self.store.recover_updates()
        self.assertIsNone(self.store.claim_update())
        self.assertIn("перезапустилась", self.store.claim_message()["text"])

    def test_schedule_uses_moscow_and_single_slot(self):
        now = datetime(2026,11,12,15,3,tzinfo=ZoneInfo("UTC"))
        schedule(self.config, self.store, now)
        schedule(self.config, self.store, now)
        self.assertEqual([j["kind"] for j in self.store.jobs()], ["lms"])
        schedule(self.config, self.store, datetime(2026,11,12,17,0,tzinfo=ZoneInfo("UTC")))
        self.assertEqual(len(self.store.jobs()), 2)  # 18 MSK = LMS, 20 MSK = Time

    def test_only_owner_can_dispatch(self):
        capri = Capri(self.config, self.store, None)
        update = {"update_id": 5, "message": {"text": "/digest", "from": {"id":1}, "chat": {"id":42,"type":"private"}}}
        handle_update(self.config, self.store, capri, update)
        self.assertEqual(self.store.jobs(), [])
        update["message"]["from"]["id"] = 42
        handle_update(self.config, self.store, capri, update)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_empty_homework_and_submit_guard(self):
        capri = Capri(self.config, self.store, None)
        self.assertIn("задание или ссылку", capri.reply("/do", 1))
        self.assertEqual(self.store.jobs(), [])
        self.assertIn("ничего не отправляла", capri.reply("/submit invalid", 2))

    def test_model_cannot_execute_shell_or_submit(self):
        capri = Capri(self.config, self.store, None)
        self.assertIn("error", capri.tool("shell", {"cmd":"ls"}, 1))
        self.assertIn("error", capri.tool("delegate", {"kind":"submit", "request":"x"}, 1))

    def test_duplicate_model_calls_do_not_duplicate_job(self):
        capri = Capri(self.config, self.store, None)
        url = "https://my.centraluniversity.ru/learn/courses/view/actual/1/themes/2/longreads/3"
        one = capri.tool("delegate", {"kind":"homework", "request":"x", "assignment_url":url}, 1)
        two = capri.tool("delegate", {"kind":"homework", "request":"y", "assignment_url":url}, 1)
        self.assertEqual(one["job_id"], two["job_id"])
        self.assertEqual(len(self.store.jobs()), 1)

    def test_artifact_path_traversal_and_symlinks(self):
        root = Path(self.temp.name)
        (root / "artifacts").mkdir()
        (root / "secret").write_text("not an artifact")
        (root / "artifacts/link").symlink_to(root / "secret")
        for name in ("artifacts/../secret", "artifacts/link", "/etc/passwd"):
            with self.assertRaises(ValueError):
                safe_artifact(root, name)

    def test_completed_mutation_requires_evidence(self):
        result = error_result("completed", "done")
        result["collected_at"] = "2026-11-12T09:00:00+03:00"
        with self.assertRaises(ValueError):
            validate_result(result, "tests")
        result["evidence"] = ["LMS confirmation"]
        self.assertEqual(validate_result(result,"tests")["status"], "completed")

    def test_network_errors_never_expose_url_or_token(self):
        with patch("capri_di.network.urlopen", side_effect=OSError("secret-url-token")):
            with self.assertRaises(RemoteError) as raised:
                post_json("https://example.org/private", {}, service="Telegram", sending=True)
        self.assertNotIn("secret", str(raised.exception))
        self.assertTrue(raised.exception.uncertain)

    def test_real_subprocess_contract_without_credentials(self):
        # A tiny executable simulates the CLI protocol, not an actual inference run.
        root = Path(self.temp.name)
        executable = root / "fake-codex"
        simulated_result = error_result("completed", "collected")
        simulated_result["collected_at"] = "2026-11-12T09:00:00+03:00"
        executable.write_text("#!/usr/bin/env python3\nimport sys,json,os\np=json.load(sys.stdin)\nassert p['kind']=='lms'\nassert 'OPENROUTER_API_KEY' not in os.environ\nassert 'OPENAI_API_KEY' not in os.environ\na=sys.argv\nassert '--approve-for-me' in a\nassert 'never' not in a\nassert '--sandbox' not in a\nassert '--dangerously-bypass-approvals-and-sandbox' not in a\nresult=" + repr(simulated_result) + "\nopen(a[a.index('--output-last-message')+1],'w').write(json.dumps(result))\n")
        executable.chmod(0o700)
        job_id = self.store.enqueue("lms", "read", "owner", "read")
        job = self.store.claim_job()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY":"private", "OPENAI_API_KEY":"private"}):
            result = Worker(self.store, root / "work", str(executable), timeout=10).run(job)
        self.store.finish(job_id,result)
        self.assertEqual(self.store.job(job_id)["state"], "completed")

    def test_completed_without_timestamp_is_not_accepted(self):
        with self.assertRaises(ValueError):
            validate_result(error_result("completed", "done"), "lms")

    def test_files_are_queued_once_and_cannot_escape(self):
        artifact = Path(self.temp.name) / "artifacts" / "job" / "answer.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("answer")
        self.store.queue_file("job", "answer.txt")
        self.store.queue_file("job", "answer.txt")
        self.assertEqual(self.store.claim_message()["file_path"], str(artifact.resolve()))
        self.assertIsNone(self.store.claim_message())
        with self.assertRaises(ValueError):
            self.store.queue_file("job", "../../../assistant.db")

    def test_result_report_does_not_dispatch_tasks(self):
        class Provider:
            def chat(self, messages, tools=None, **kwargs):
                assert tools is None
                return {"content": "Alex, данные собраны."}
        job_id = self.store.enqueue("lms", "read", "owner", "report")
        self.store.claim_job()
        self.store.finish(job_id, error_result("blocked", "login required"))
        capri = Capri(self.config, self.store, Provider())
        report = capri.report(self.store.job(job_id))
        self.assertIn(job_id, report)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_unknown_report_does_not_invent_authentication_failure(self):
        class NoProvider:
            def chat(self, *args, **kwargs):
                raise AssertionError("Failure reports must preserve known facts")
        capri = Capri(self.config, self.store, NoProvider())
        result = error_result("unknown", "Codex returned no confirmed result")
        job = {"id": "test-job", "kind": "lms", "result": json.dumps(result)}
        answer = capri.report(job)
        self.assertIn("Codex returned no confirmed result", answer)
        self.assertIn("сбор LMS", answer)
        self.assertNotIn("login-desktop.sh", answer)

    def test_lms_scope_filters_archive_admin_and_submitted_but_keeps_overdue(self):
        def record(**changes):
            return {"source":"lms", "url":"https://my.centraluniversity.ru/learn/courses/view/actual/1/themes/2/longreads/3",
                    "title":"ДЗ 4", "course":"NLP", "status":"Не сдано", "due_at":"1 ноября", **changes}
        overdue = record()
        archived = record(url="https://my.centraluniversity.ru/learn/courses/view/archived/135/themes/686/longreads/1564")
        obsolete = record(title="Домашнее задание. (НЕактуально с 02.11)")
        regulation = record(course="Ознакомление с локально-нормативными актами и приказами ЦУ")
        review = record(status="На проверке")
        submitted = record(status="Сдано")
        self.assertTrue(relevant_record(overdue))
        for excluded in (archived, obsolete, regulation, review, submitted):
            self.assertFalse(relevant_record(excluded))
        raw = {"status":"completed", "summary":"Legacy archive and admin summary", "records":[overdue,archived,regulation,review]}
        scoped = digest_result(raw)
        self.assertEqual(scoped["records"], [overdue])
        self.assertNotIn("summary", scoped)
        self.assertEqual(len(raw["records"]), 4)

    def test_fallback_is_short_relevant_and_has_no_real_name_or_technical_status(self):
        result = error_result("completed", "Collected all 17 active records; completed")
        result["collected_at"] = "2026-11-12T09:00:00+03:00"
        base = {"source":"lms", "title":"ДЗ 9", "course":"Командная работа по Agile",
                "status":"Задано", "due_at":"12 ноября, 20:00", "url":"https://my.centraluniversity.ru/learn/courses/view/actual/1"}
        result["records"] = [base, {**base,"title":"ОРГ_2","url":"https://my.centraluniversity.ru/learn/courses/view/archived/135"}]
        answer = Capri.fallback({"kind":"lms"}, result)
        for unwanted in ("Alex", "completed", "17 active", "ОРГ_2"):
            self.assertNotIn(unwanted, answer)
        self.assertIn("20:00", answer)
        self.assertIn(base["url"], answer)

    def test_formatter_gets_only_relevant_records_and_no_legacy_summary(self):
        class Provider:
            def chat(self, messages, tools=None, **kwargs):
                supplied = json.loads(messages[-1]["content"])["result"]
                self.supplied = supplied
                return {"content":"Сегодня сначала Agile — до 20:00."}
        result = error_result("completed", "Archived task ORG_2 summary")
        result["records"] = [{"source":"lms","course":"ORG_2","title":"Old task","status":"На проверке",
                              "url":"https://my.centraluniversity.ru/learn/courses/view/archived/135"}]
        provider = Provider()
        answer = Capri(self.config,self.store,provider).report({"kind":"lms","id":"job-id","result":json.dumps(result)})
        self.assertEqual(provider.supplied["records"], [])
        self.assertNotIn("summary", provider.supplied)
        self.assertNotIn("ID:", answer)
        self.assertNotIn("Alex", Capri.acknowledge({"job_id":"job-id"}))

    def test_time_collection_passes_successful_checkpoint_to_next_job(self):
        root = Path(self.temp.name)
        executable = root / "fake-time-codex"
        result = error_result("completed", "Checked new announcements")
        result["collected_at"] = "2026-11-12T09:00:00+03:00"
        executable.write_text("#!/usr/bin/env python3\nimport json,sys,pathlib\np=json.load(sys.stdin)\na=sys.argv\nwork=pathlib.Path(a[a.index('--cd')+1])\n(work/'observed-prompt.json').write_text(json.dumps(p))\nresult=" + repr(result) + "\npathlib.Path(a[a.index('--output-last-message')+1]).write_text(json.dumps(result))\n")
        executable.chmod(0o700)
        worker = Worker(self.store, root / "time-work", str(executable), timeout=10)
        for key in ("first", "second"):
            job_id = self.store.enqueue("time", "Read new study messages", "owner", key)
            job = self.store.claim_job()
            returned = worker.run(job)
            self.store.finish(job_id, returned)
            supplied = json.loads((root / "time-work" / job_id / "observed-prompt.json").read_text())
            self.assertEqual(supplied["since_last_successful_collection"], "" if key == "first" else result["collected_at"])
        self.assertEqual(self.store.setting("time_collected_at"), result["collected_at"])

    def test_digest_retains_user_language_across_commands(self):
        class Provider:
            def __init__(self):
                self.messages = []
            def chat(self, messages, tools=None, **kwargs):
                self.messages = messages
                return routed(reply="Ready.") if tools else {"content": "Ready."}
        provider = Provider()
        capri = Capri(self.config, self.store, provider)
        capri.reply("Please keep me posted about deadlines.", 1)
        capri.reply("/digest", 2)
        self.assertEqual(self.store.setting("conversation_language_hint"), "Please keep me posted about deadlines.")
        job = self.store.jobs()[0]
        self.store.claim_job()
        result = error_result("completed", "read")
        result["collected_at"] = "2026-11-12T09:00:00+03:00"
        self.store.finish(job["id"], result)
        capri.report(self.store.job(job["id"]))
        self.assertIn("Please keep me posted about deadlines.", provider.messages[1]["content"])
        self.assertIn("free-form", provider.messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
