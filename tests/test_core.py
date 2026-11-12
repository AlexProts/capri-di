from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
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


def config(root):
    return Config(Path(root), 42, "fake", "fake", "fake/model", True, True, True, (9,20), 4, 10)


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
        now = datetime(2026,11,12,6,3,tzinfo=ZoneInfo("UTC"))
        schedule(self.config, self.store, now)
        schedule(self.config, self.store, now)
        self.assertEqual([j["kind"] for j in self.store.jobs()], ["lms"])
        schedule(self.config, self.store, datetime(2026,11,12,17,0,tzinfo=ZoneInfo("UTC")))
        self.assertEqual(len(self.store.jobs()), 3)  # 20 MSK = LMS + Time

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
        self.assertIn("пришли задание", capri.reply("/do", 1))
        self.assertEqual(self.store.jobs(), [])
        self.assertIn("Ничего не отправлено", capri.reply("/submit invalid", 2))

    def test_model_cannot_execute_shell_or_submit(self):
        capri = Capri(self.config, self.store, None)
        self.assertIn("error", capri.tool("shell", {"cmd":"ls"}, 1))
        self.assertIn("error", capri.tool("delegate", {"kind":"submit", "request":"x"}, 1))

    def test_duplicate_model_calls_do_not_duplicate_job(self):
        capri = Capri(self.config, self.store, None)
        one = capri.tool("delegate", {"kind":"homework", "request":"x"}, 1)
        two = capri.tool("delegate", {"kind":"homework", "request":"y"}, 1)
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
        executable.write_text("#!/usr/bin/env python3\nimport sys,json,os\np=json.load(sys.stdin)\nassert p['kind']=='lms'\nassert 'OPENROUTER_API_KEY' not in os.environ\nassert 'OPENAI_API_KEY' not in os.environ\na=sys.argv\nresult=" + repr(simulated_result) + "\nopen(a[a.index('--output-last-message')+1],'w').write(json.dumps(result))\n")
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
            def chat(self, messages, tools=None):
                assert tools is None
                return {"content": "Alex, данные собраны."}
        job_id = self.store.enqueue("lms", "read", "owner", "report")
        self.store.claim_job()
        self.store.finish(job_id, error_result("blocked", "login required"))
        capri = Capri(self.config, self.store, Provider())
        report = capri.report(self.store.job(job_id))
        self.assertIn(job_id, report)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_digest_retains_user_language_across_commands(self):
        class Provider:
            def __init__(self):
                self.messages = []
            def chat(self, messages, tools=None):
                self.messages = messages
                return {"content": "Ready."}
        provider = Provider()
        capri = Capri(self.config, self.store, provider)
        capri.reply("Please keep me posted about deadlines.", 1)
        capri.reply("/digest", 2)
        self.assertEqual(self.store.setting("conversation_language_hint"), "Please keep me posted about deadlines.")
        job = self.store.jobs()[0]
        self.store.claim_job()
        self.store.finish(job["id"], error_result("blocked", "login required"))
        capri.report(self.store.job(job["id"]))
        self.assertIn("Please keep me posted about deadlines.", provider.messages[1]["content"])
        self.assertIn("free-form", provider.messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
