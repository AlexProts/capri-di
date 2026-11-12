from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from capri_di.capri import Capri
from capri_di.store import Store
from capri_di.worker import Worker, copy_previous_work, error_result
from test_core import config


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "shared")
        self.capri = Capri(config(self.root), self.store, None)

    def parent(self, state="blocked", kind="homework"):
        job_id = self.store.enqueue(kind, "RecSys ДЗ 4, графовые сети. https://my.centraluniversity.ru/learn/courses/view/actual/1248/themes/12167/longreads/44572 Только подготовить, не прикреплять.", "owner", "parent")
        job = self.store.claim_job()
        if state != "running":
            self.store.finish(job_id, error_result(state, "Подготовлен код; отсутствуют numpy и torch"))
        return self.store.job(job["id"])

    def test_blocked_homework_gets_new_linked_job_and_explicit_permission(self):
        parent = self.parent()
        answer = self.capri.reply(f"/continue {parent['id']} --deps Установить библиотеки и проверить решение, не прикреплять", 1)
        self.assertIn("очередь", answer)
        self.assertIn("только в окружении", answer)
        child = self.store.jobs()[0]
        self.assertNotEqual(child["id"], parent["id"])
        self.assertEqual(child["parent_id"], parent["id"])
        self.assertEqual(self.store.job(parent["id"])["state"], "blocked")
        self.assertTrue(json.loads(child["permissions"])["install_dependencies"])
        context = json.loads(child["context"])
        self.assertEqual(context["original_homework_request"], parent["request"])
        self.assertIn("--deps", context["latest_owner_message"])
        self.assertEqual(self.store.claim_job()["id"], child["id"])

    def test_followup_waits_for_running_parent_and_deduplicates(self):
        parent = self.parent(state="running")
        args = {"job_id": parent["id"], "request": "Проверь метрики, не прикрепляй", "install_dependencies": True}
        first = self.capri.tool("continue_homework", args, 1)
        second = self.capri.tool("continue_homework", args, 1)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(first["waiting_for_parent"])
        self.assertIn("после текущей", self.capri.acknowledge(first))
        self.assertIsNone(self.store.claim_job())
        with self.assertRaises(ValueError):
            self.capri.tool("continue_homework", args, 2)
        self.store.finish(parent["id"], error_result("blocked", "Need libraries"))
        self.assertEqual(self.store.claim_job()["id"], first["job_id"])

    def test_tests_and_submissions_cannot_be_replayed_as_homework(self):
        parent = self.parent(state="unknown", kind="tests")
        with self.assertRaises(ValueError):
            self.capri.tool("continue_homework", {"job_id": parent["id"], "request": "retry"}, 1)
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertIn("error", self.capri.delegate("tests", "retry", "other", True))

    def test_dispatch_ack_is_derived_from_receipt_not_model_claim(self):
        parent = self.parent()
        class Provider:
            calls = 0
            def chat(self, messages, tools=None):
                self.calls += 1
                if self.calls > 1:
                    raise AssertionError("A model must not paraphrase a queue receipt as running")
                return {"content": "Он уже запущен под старым ID!", "tool_calls": [{"id": "dispatch", "type": "function", "function": {
                    "name": "continue_homework", "arguments": json.dumps({"job_id": parent["id"], "request": "Поставить библиотеки и проверить, не прикреплять", "install_dependencies": True})}}]}
        provider = Provider()
        capri = Capri(config(self.root), self.store, provider)
        answer = capri.reply("Разрешаю поставить библиотеки, пусть доделает", 10)
        self.assertIn("очередь", answer)
        self.assertNotIn("запущен", answer)
        self.assertNotIn(parent["id"], answer)
        self.assertEqual(provider.calls, 1)

    def test_snapshot_excludes_environment_links_and_previous_policy(self):
        source, destination = self.root / "source", self.root / "destination"
        source.mkdir()
        (source / "solution.py").write_text("print('draft')")
        (source / "AGENTS.md").write_text("old policy")
        (source / "auth.json").write_text("not a task file")
        (source / ".venv").mkdir()
        (source / ".venv/package.py").write_text("skip")
        (source / "outside-link").symlink_to(self.root)
        (source / "file-link.py").symlink_to(source / "solution.py")
        manifest = copy_previous_work(source, destination)
        self.assertEqual(manifest["copied"], ["solution.py"])
        self.assertEqual((destination / "solution.py").read_text(), "print('draft')")
        self.assertFalse((destination / "AGENTS.md").exists())
        self.assertFalse((destination / "auth.json").exists())
        self.assertFalse((destination / ".venv").exists())
        self.assertIn("file-link.py", manifest["omitted"])

    def test_snapshot_budget_reports_omission(self):
        source = self.root / "source"
        source.mkdir()
        (source / "large.bin").write_bytes(b"x" * 30)
        (source / "small.py").write_text("ok")
        manifest = copy_previous_work(source, self.root / "copy", max_bytes=10)
        self.assertEqual(manifest["copied"], ["small.py"])
        self.assertEqual(manifest["omitted"], ["large.bin"])

    def test_worker_receives_files_original_request_permission_and_supplement(self):
        parent = self.parent()
        work_root = self.root / "work"
        previous = work_root / parent["id"]
        (previous / "artifacts").mkdir(parents=True)
        (previous / "train.py").write_text("# prepared before blocking")
        (previous / "artifacts/answer.ipynb").write_text("{}")
        child = self.capri.continue_homework(parent["id"], "Разрешаю библиотеки; проверь метрики. Не прикрепляй.", "child", True)
        claimed = self.store.claim_job()
        executable = self.root / "fake-codex"
        result = error_result("completed", "Verified preparation only")
        result["collected_at"] = "2026-11-12T12:00:00+03:00"
        executable.write_text("#!/usr/bin/env python3\nimport json,sys,pathlib\np=json.load(sys.stdin)\na=sys.argv\nw=pathlib.Path(a[a.index('--cd')+1])\n(w/'prompt.json').write_text(json.dumps(p))\nr=" + repr(result) + "\npathlib.Path(a[a.index('--output-last-message')+1]).write_text(json.dumps(r))\n")
        executable.chmod(0o700)
        outcome = Worker(self.store, work_root, str(executable), timeout=10).run(claimed)
        work = work_root / child["job_id"]
        prompt = json.loads((work / "prompt.json").read_text())
        self.assertTrue(prompt["permissions"]["install_dependencies"])
        self.assertFalse(prompt["permissions"]["submit_homework"])
        self.assertEqual(prompt["continuation"]["original_homework_request"], parent["request"])
        self.assertIn("Не прикрепляй", prompt["user_request"])
        self.assertTrue((work / "previous/train.py").exists())
        self.assertTrue((work / "previous/artifacts/answer.ipynb").exists())
        self.assertIn("ТОЛЬКО в .venv", (work / "AGENTS.md").read_text())
        self.assertEqual(outcome["status"], "completed")

    def test_missing_parent_files_blocks_before_launching_codex(self):
        parent = self.parent()
        self.capri.continue_homework(parent["id"], "Продолжи", "child")
        outcome = Worker(self.store, self.root / "work", "nonexistent-codex").run(self.store.claim_job())
        self.assertEqual(outcome["status"], "blocked")
        self.assertIn("Прежняя рабочая папка", outcome["summary"])

    def test_permissions_and_original_request_survive_continuation_chain(self):
        parent = self.parent()
        first = self.capri.continue_homework(parent["id"], "Поставить библиотеки", "child", True)
        self.store.claim_job()
        self.store.finish(first["job_id"], error_result("blocked", "Need dataset"))
        second = self.capri.continue_homework(first["job_id"], "Данные появились, продолжи", "grandchild")
        final = self.store.job(second["job_id"])
        self.assertTrue(json.loads(final["permissions"])["install_dependencies"])
        self.assertEqual(json.loads(final["context"])["original_homework_request"], parent["request"])
        self.assertEqual(final["kind"], "homework")

    def test_vague_homework_is_not_enqueued_until_one_source_is_identified(self):
        result = self.capri.delegate("homework", "Рек сис ДЗ 4 или 5, сделать", "vague")
        self.assertIn("error", result)
        self.assertEqual(self.store.jobs(), [])
        exact = "https://my.centraluniversity.ru/learn/courses/view/actual/1248/themes/12167/longreads/44572"
        accepted = self.capri.tool("delegate", {"kind": "homework", "request": "Графовые сети: подготовить и проверить, не прикреплять", "assignment_url": exact}, 1)
        job = self.store.job(accepted["job_id"])
        self.assertEqual(json.loads(job["context"])["assignment_url"], exact)

    def test_dependency_permission_does_not_transfer_to_different_assignment(self):
        first_url = "https://my.centraluniversity.ru/learn/courses/view/actual/1/themes/2/longreads/3"
        second_url = "https://my.centraluniversity.ru/learn/courses/view/actual/1/themes/2/longreads/4"
        initial = self.capri.delegate("homework", "Подготовить " + first_url, "first", True)
        self.store.claim_job()
        self.store.finish(initial["job_id"], error_result("blocked", "Need data"))
        child = self.capri.continue_homework(initial["job_id"], "Исправление: нужна другая ссылка " + second_url, "second")
        self.assertFalse(child["install_dependencies"])


if __name__ == "__main__":
    unittest.main()
