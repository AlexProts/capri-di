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
from .context import result_view

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


def copy_previous_work(source, destination, max_bytes=100 * 1024 * 1024):
    """Copy only bounded regular task files, never environments, links or old policy."""
    copied, omitted, size = [], [], 0
    excluded = {"AGENTS.md", "result.json", "result-schema.json", "auth.json", "credentials.json", "storage-state.json", "browser.json", "config.toml"}
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Previous workspace unavailable")
    for folder, directories, files in os.walk(source, followlinks=False):
        directories[:] = sorted(d for d in directories if not d.startswith(".") and d not in {"venv", "node_modules", "__pycache__"} and not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            path = Path(folder) / name
            relative = path.relative_to(source)
            if name.startswith(".") or name in excluded:
                continue
            if path.is_symlink() or not path.is_file() or len(copied) >= 500 or size + path.stat().st_size > max_bytes:
                if len(omitted) < 50:
                    omitted.append(str(relative))
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            copied.append(str(relative))
            size += path.stat().st_size
    return {"directory": "previous/", "copied": copied, "omitted": omitted}


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
        if job.get("context"):
            prompt["reference_context_data"] = json.loads(job["context"])
            prompt["assignment_url"] = prompt["reference_context_data"].get("assignment_url", "")
        prompt["permissions"] = {"install_dependencies": job["kind"] == "homework" and json.loads(job.get("permissions", "{}")).get("install_dependencies") is True,
                                 "submit_homework": job["kind"] == "submit"}
        if job.get("parent_id"):
            parent = self.store.job(job["parent_id"])
            if not parent or parent["kind"] != "homework" or job["kind"] != "homework" or parent["state"] in {"queued", "running"}:
                return error_result("blocked", "Не удалось подтвердить исходную домашку для продолжения")
            try:
                files = copy_previous_work(self.root / parent["id"], work / "previous")
            except (OSError, ValueError):
                return error_result("blocked", "Прежняя рабочая папка недоступна; продолжение не запускал. Нужны исходные файлы.")
            prompt["continuation"] = {"parent_job_id": parent["id"], "previous_result_data": result_view(parent),
                                      "original_homework_request": json.loads(job["context"]).get("original_homework_request", parent["request"]),
                                      "files": files,
                                      "instruction": "Продолжи подготовку этой же домашки с учётом текущего user_request. Сначала прочитай прежнее решение и точное условие LMS; не подменяй задание похожим. Не прикрепляй и не сдавай. Финальные файлы положи в artifacts/ нового job."}
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
