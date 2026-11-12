"""Opt-in real-model CJM check. Temporary DB only; no Telegram, LMS or worker."""
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import tempfile

from .capri import Capri, MSK
from .config import Config
from .network import OpenRouter
from .store import Store
from .worker import error_result


def main():
    config = Config.load()
    class ProbeModel(OpenRouter):
        def chat(self, *args, **kwargs):
            result = super().chat(*args, **kwargs)
            if kwargs.get("tool_choice"):
                for call in result.get("tool_calls", []):
                    print(json.dumps({"probe_route": json.loads(call["function"]["arguments"])}, ensure_ascii=False), flush=True)
            return result
    print(json.dumps({"probe_model": config.model, "routing_model": config.routing_model or config.model}), flush=True)
    with tempfile.TemporaryDirectory(prefix="capri-routing-") as directory:
        config = replace(config, state=Path(directory), owner=42, telegram_token="", bot_username="", schedules=False, proactive_tests=False)
        store = Store(directory)
        parent = store.enqueue("homework", "Рек сис, ДЗ 4 или 5, подготовить без прикрепления", "probe-owner", "parent")
        store.claim_job()
        store.finish(parent, error_result("blocked", "Частичный код сохранён, не хватает NumPy и PyTorch. Проверка не выполнена, файлы не прикреплял."))
        source = store.enqueue("lms", "Read-only collection", "probe", "source")
        store.claim_job()
        result = error_result("completed", "Observed actual assignments")
        result["collected_at"] = datetime.now(MSK).isoformat()
        result["records"] = [{"id": "hw4", "source": "lms", "title": "ДЗ 4. Графовые нейронные сети в рекомендациях", "course": "Рекомендательные системы", "status": "Задано", "due_at": "15 ноября 2026, 20:00", "url": "https://my.centraluniversity.ru/learn/courses/view/actual/1248/themes/12167/longreads/44572", "details": "Подготовить решение по условию LMS"},
                             {"id": "hw5", "source": "lms", "title": "ДЗ 5. Соревнование", "course": "Рекомендательные системы", "status": "Задано", "due_at": "28 декабря 2026, 20:00", "url": "https://my.centraluniversity.ru/learn/courses/view/actual/1248/themes/12168/longreads/45997", "details": "Подготовить решение по условию LMS"}]
        store.finish(source, result)
        capri = Capri(config, store, OpenRouter(config.openrouter_key, config.model), router_llm=ProbeModel(config.openrouter_key, config.routing_model or config.model))
        question = capri.reply("отправь еще раз ди задачку на рек сис", 1)
        print(json.dumps({"probe_question": question}, ensure_ascii=False), flush=True)
        assert len([j for j in store.jobs() if j["kind"] == "homework"]) == 1, "Must clarify ambiguous HW4/HW5 before execution"
        assert "/continue" not in question and "recall" not in question, "Internal instruction leaked"
        assert any(term in question.casefold() for term in ("4", "5", "граф", "соревн")), "Need a concrete assignment choice"
        accepted = capri.reply("ДЗ 4, графовые сети. Библиотеки разрешаю ставить, доделать и проверить, не прикреплять", 2)
        print(json.dumps({"probe_acceptance": accepted}, ensure_ascii=False), flush=True)
        children = [j for j in store.jobs() if j["parent_id"] == parent]
        assert len(children) == 1, "Clarification must actually enqueue continuation"
        child = children[0]
        assert json.loads(child["permissions"])["install_dependencies"], "Owner library permission was lost"
        assert json.loads(child["context"])["assignment_url"].endswith("/44572"), "Wrong assignment"
        assert all(j["kind"] != "submit" for j in store.jobs()), "Preparation must not submit"
        reminder = capri.reply("Напомни завтра в 18:00 проверить результат этой домашки", 3)
        assert len(store.todos()) == 1 and store.todos()[0]["remind_at"], "Natural-language reminder was not saved"
        print(json.dumps({"passed": True, "model": config.model, "clarification": question, "accepted": accepted, "reminder": reminder,
                          "real_telegram_sends": 0, "real_worker_runs": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
