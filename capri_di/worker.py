import fcntl
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from urllib.parse import urlsplit

from .store import Store
from .relevance import relevant_record

PACKAGE = Path(__file__).parent


def error_result(status, summary):
    return dict(status=status, summary=summary, collected_at="", records=[],
                artifacts=[], evidence=[], source_errors=[])


def safe_artifact(work, name):
    if not isinstance(name, str) or not name.startswith("artifacts/"):
        raise ValueError("Invalid artifact path")
    file = (work / name).resolve()
    if not file.is_relative_to((work / "artifacts").resolve()) or not file.is_file():
        raise ValueError("Artifact escapes job workspace")
    if file.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Artifact larger than 20 MiB")
    return file


def validate_result(result, kind):
    schema = json.loads((PACKAGE / "schemas/result.json").read_text())
    if not isinstance(result, dict) or set(result) != set(schema["required"]):
        raise ValueError("Invalid result fields")
    if result["status"] not in schema["properties"]["status"]["enum"]:
        raise ValueError("Invalid status")
    for key in ("summary", "collected_at"):
        if not isinstance(result[key], str):
            raise ValueError("Invalid result text")
    for key in ("records", "artifacts", "evidence", "source_errors"):
        if not isinstance(result[key], list) or len(result[key]) > 300:
            raise ValueError("Invalid result list")
    for key in ("artifacts", "evidence", "source_errors"):
        if any(not isinstance(item, str) for item in result[key]):
            raise ValueError("Invalid result item")
    fields = set(schema["properties"]["records"]["items"]["required"])
    for record in result["records"]:
        if not isinstance(record, dict) or set(record) != fields or any(not isinstance(v, str) for v in record.values()):
            raise ValueError("Invalid source record")
        if record["source"] not in {"lms", "time"}:
            raise ValueError("Invalid source")
        url = urlsplit(record["url"])
        expected_host = "my.centraluniversity.ru" if record["source"] == "lms" else "time.cu.ru"
        if url.scheme != "https" or url.hostname != expected_host or url.username or url.password or url.query or url.fragment:
            raise ValueError("Invalid source link or access-bearing URL")
    if result["status"] == "completed":
        collected = datetime.fromisoformat(result["collected_at"])
        if collected.tzinfo is None:
            raise ValueError("Collection time must include timezone")
    if result["status"] == "completed" and kind in {"tests", "submit"} and not result["evidence"]:
        raise ValueError("Missing evidence for external action")
    return result


class Worker:
    def __init__(self, store, work_root, codex="codex", timeout=3600):
        self.store, self.root, self.codex, self.timeout = store, Path(work_root), codex, timeout
        self.root.mkdir(parents=True, exist_ok=True)

    def run(self, job):
        work = self.root / job["id"]
        work.mkdir(mode=0o700, exist_ok=False)
        (work / "artifacts").mkdir()
        (work / "AGENTS.md").write_text((PACKAGE / "prompts/di.md").read_text())
        schema = work / "result-schema.json"
        shutil.copyfile(PACKAGE / "schemas/result.json", schema)
        prompt = {"job_id": job["id"], "kind": job["kind"], "user_request": job["request"],
                  "origin": job["origin"], "instruction": "Выполни только эту задачу по AGENTS.md. Верни JSON по схеме."}
        if job["kind"] == "time":
            prompt["since_last_successful_collection"] = self.store.setting("time_collected_at")
        if job["kind"] == "submit":
            parent = self.store.job(job["request"])
            if not parent or parent["kind"] != "homework" or parent["state"] != "completed":
                return error_result("blocked", "Исходная домашка не завершена")
            prior = json.loads(parent["result"])
            source_dir = self.root / parent["id"]
            for name in prior["artifacts"]:
                src = safe_artifact(source_dir, name)
                dst = work / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            prompt["prepared_homework"] = prior
            prompt["original_user_request"] = parent["request"]
        output = work / "result.json"
        # --approve-for-me already selects workspace-write; combining --sandbox is invalid.
        argv = [self.codex, "exec", "--approve-for-me",
                "--skip-git-repo-check", "--cd", str(work), "--output-schema", str(schema),
                "--output-last-message", str(output), "-"]
        if os.environ.get("CODEX_MODEL"):
            argv[-1:-1] = ["--model", os.environ["CODEX_MODEL"]]
        # Credentials for Telegram/OpenRouter are not inherited. Production uses another OS user.
        env = {k: v for k, v in os.environ.items() if k in {
            "PATH", "HOME", "USER", "LANG", "LC_ALL", "CODEX_HOME", "TMPDIR",
            "DISPLAY", "XAUTHORITY", "PLAYWRIGHT_BROWSERS_PATH", "SSL_CERT_FILE"}}
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, env=env, start_new_session=True)
            try:
                process.communicate(json.dumps(prompt, ensure_ascii=False).encode(), timeout=self.timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                return error_result("unknown", "Ди превысил время выполнения. Возможные действия в LMS не повторяются автоматически.")
            if process.returncode or not output.is_file():
                return error_result("unknown", "Codex завершился без подтверждённого результата. Проверь вход, лимиты и браузер; автоповтора нет.")
            if output.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Result too large")
            result = validate_result(json.loads(output.read_text()), job["kind"])
            if job["kind"] == "lms":
                result["records"] = [r for r in result["records"] if relevant_record(r)]
            if job["kind"] == "time" and result["status"] == "completed" and not result["source_errors"]:
                self.store.set_setting("time_collected_at", result["collected_at"])
            for name in result["artifacts"]:
                src = safe_artifact(work, name)
                destination = self.store.root / "artifacts" / job["id"] / name.removeprefix("artifacts/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, destination)
            return result
        except FileNotFoundError:
            return error_result("blocked", "Codex CLI или подготовленный файл не найден")
        except (OSError, ValueError, KeyError, TypeError):
            return error_result("unknown", "Некорректный результат Ди; возможные внешние действия не повторяются автоматически")

    def serve(self):
        # One worker owns the persistent browser profile and recovery process.
        with (self.root / "worker.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.store.recover_interrupted()
            while True:
                job = self.store.claim_job()
                if job:
                    self.store.finish(job["id"], self.run(job))
                else:
                    time.sleep(2)
