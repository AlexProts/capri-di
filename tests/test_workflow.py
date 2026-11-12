import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from capri_di.capri import Capri
from capri_di.context import find_assignment
from capri_di.network import OpenRouter, RemoteError
from capri_di.store import Store
from capri_di.worker import error_result
from test_core import config, routed


URL4 = "https://my.centraluniversity.ru/learn/courses/view/actual/1248/themes/12167/longreads/44572"
URL5 = "https://my.centraluniversity.ru/learn/courses/view/actual/1248/themes/12168/longreads/45997"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.llm = OpenRouter("fake", "fake/model")
        self.capri = Capri(config(self.temp.name), self.store, self.llm)

    def seed(self, ambiguous=False):
        parent = self.store.enqueue("homework", "Рек сис ДЗ 4 или 5", "owner", "legacy")
        self.store.claim_job()
        self.store.finish(parent, error_result("blocked", "Missing libraries"))
        collection = self.store.enqueue("lms", "read", "owner", "collection")
        self.store.claim_job()
        result = error_result("completed", "Source records, not authorization")
        result["records"] = [{"source": "lms", "course": "Рекомендательные системы", "title": "ДЗ 4. Графовые сети", "status": "Задано", "due_at": "15 ноября", "url": URL4, "details": ""}]
        if ambiguous:
            result["records"].append({**result["records"][0], "title": "ДЗ 5. Соревнование", "url": URL5})
        self.store.finish(collection, result)
        return parent

    def test_plain_model_promise_is_not_accepted_as_workflow_result(self):
        with patch.object(self.llm, "chat", return_value={"content": "Ди уже запущен!"}):
            with self.assertRaises(RemoteError):
                self.capri.reply("Отправь Ди задачу", 1)
        self.assertFalse(self.store.jobs())

    def test_natural_request_resolves_unique_assignment_and_actually_continues(self):
        parent = self.seed()
        plan = routed("continue_homework", {"job_id": parent, "request": "Продолжить прежнюю домашку, не прикреплять"})
        with patch.object(self.llm, "chat", return_value=plan) as model:
            answer = self.capri.reply("отправь еще раз ди задачку на рек сис", 1)
        self.assertIn("очередь", answer)
        child = self.store.jobs()[0]
        self.assertEqual(child["parent_id"], parent)
        self.assertEqual(json.loads(child["context"])["assignment_url"], URL4)
        kwargs = model.call_args.kwargs
        self.assertEqual(kwargs["tool_choice"]["function"]["name"], "route_request")

    def test_ambiguity_is_a_human_choice_and_short_answer_finishes_pending_request(self):
        parent = self.seed(ambiguous=True)
        attempt = routed("continue_homework", {"job_id": parent, "request": "Продолжить, не прикреплять"})
        with patch.object(self.llm, "chat", side_effect=[attempt, routed("clarify", reply="ДЗ 4 про графовые сети или ДЗ 5 — соревнование?")]) as model:
            question = self.capri.reply("отправь еще раз ди задачку на рек сис", 1)
        self.assertNotIn("recall", question)
        self.assertEqual(len(self.store.jobs()), 2)
        error_context = model.call_args_list[1].args[0]
        self.assertIn("assignment_selection_required", str(error_context))
        self.assertIn("Графовые сети", str(error_context))
        followup = routed("continue_homework", {"job_id": parent, "request": "Подготовить ДЗ 4 и проверить, не прикреплять", "assignment_url": URL4, "install_dependencies": True})
        with patch.object(self.llm, "chat", return_value=followup):
            answer = self.capri.reply("четвертую, библиотеки можно поставить", 2)
        self.assertIn("очередь", answer)
        child = self.store.jobs()[0]
        self.assertTrue(json.loads(child["permissions"])["install_dependencies"])
        self.assertIn("отправь еще раз", json.loads(child["context"])["pending_owner_request"]["original_request"])
        self.assertEqual(self.store.setting("pending_workflow"), "{}")

    def test_manual_command_advice_for_action_is_rejected_and_rerouted(self):
        parent = self.seed()
        with patch.object(self.llm, "chat", side_effect=[routed(reply="Введи /continue ID, чтобы он продолжил"), routed("continue_homework", {"job_id": parent, "request": "Доделать без прикрепления"})]):
            answer = self.capri.reply("пусть Ди продолжит", 1)
        self.assertNotIn("/continue", answer)
        self.assertIn("очередь", answer)

    def test_question_disguised_as_dispatch_is_rejected_before_any_task_is_created(self):
        self.seed(ambiguous=True)
        invalid = routed("delegate", {"kind": "homework", "request": "ДЗ4 или5", "assignment_url": URL4}, reply="Какую именно, четвёртую или пятую?")
        with patch.object(self.llm, "chat", side_effect=[invalid, routed("clarify", reply="ДЗ 4 про графовые сети или ДЗ 5 — соревнование?", workflow="continue_homework")]):
            answer = self.capri.reply("Отправь ещё раз задачу по рек сису", 1)
        self.assertIn("Граф", answer.title())
        self.assertEqual(len(self.store.jobs()), 2)
        self.assertEqual(json.loads(self.store.setting("pending_workflow"))["workflow"], "continue_homework")

    def test_continuation_workflow_cannot_dispatch_a_new_unlinked_homework(self):
        self.seed(ambiguous=True)
        invalid = routed("delegate", {"kind": "homework", "request": "ДЗ4", "assignment_url": URL4}, workflow="continue_homework")
        with patch.object(self.llm, "chat", side_effect=[invalid, routed("clarify", reply="ДЗ 4 или ДЗ 5?", workflow="continue_homework")]):
            self.capri.reply("Отправь ещё раз задачу по рек сису", 1)
        self.assertEqual(len(self.store.jobs()), 2)

    def test_continue_shortcut_without_instruction_uses_context_instead_of_bouncing_user(self):
        parent = self.seed()
        with patch.object(self.llm, "chat", return_value=routed("continue_homework", {"job_id": parent, "request": "Продолжить без прикрепления"})):
            answer = self.capri.reply("/continue " + parent, 1)
        self.assertIn("очередь", answer)
        self.assertNotIn("Нужны ID", answer)

    def test_course_alias_finds_candidates_without_generating_new_jobs(self):
        self.seed(ambiguous=True)
        for query in ("рек сис", "что по рек сису", "с рек сисом", "recsys", "рекомендательным системам"):
            with self.subTest(query=query):
                found = find_assignment(self.store, query)
                self.assertEqual({r["url"] for r in found}, {URL4, URL5})
        self.assertEqual(len(self.store.jobs()), 2)

    def test_openrouter_sends_forced_route_choice_to_provider(self):
        with patch("capri_di.network.post_json", return_value={"choices": [{"message": routed(reply="Привет!")} ]}) as request:
            self.assertEqual(self.capri.reply("Привет", 1), "Привет!")
        payload = request.call_args.args[1]
        self.assertEqual(payload["tool_choice"], {"type": "function", "function": {"name": "route_request"}})
        self.assertEqual(payload["tools"][0]["function"]["name"], "route_request")

    def test_natural_submission_and_files_are_separate_from_preparation(self):
        parent = self.store.enqueue("homework", "Prepare " + URL4, "owner", "ready")
        self.store.claim_job()
        result = error_result("completed", "Prepared and verified")
        result["artifacts"] = ["artifacts/answer.py"]
        self.store.finish(parent, result)
        file = Path(self.temp.name) / "artifacts" / parent / "answer.py"
        file.parent.mkdir(parents=True)
        file.write_text("# ready")
        with patch.object(self.llm, "chat", side_effect=[routed("get_files", {"job_id": parent}), routed(reply="Файлы поставила на отправку.")]):
            self.capri.reply("пришли мне готовые файлы", 1)
        self.assertEqual(self.store.claim_message()["file_path"], str(file.resolve()))
        self.assertEqual(len(self.store.jobs()), 1)
        with patch.object(self.llm, "chat", return_value=routed("submit_homework", {"job_id": parent})):
            answer = self.capri.reply("прикрепи эту готовую домашку в LMS", 2)
        self.assertIn("на сдачу", answer)
        self.assertEqual(self.store.jobs()[0]["kind"], "submit")
        self.assertEqual(self.store.jobs()[0]["request"], parent)


if __name__ == "__main__":
    unittest.main()
