from contextlib import contextmanager
from datetime import datetime
import json
import os
import re
from pathlib import Path
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "assistant.db"
        # SQLite normally creates files with 0644, even with a group-friendly umask.
        # Precreate with explicit permissions: Capri and Di use different UIDs.
        try:
            fd = os.open(self.db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                os.fchmod(fd, 0o660)
            finally:
                os.close(fd)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, dedup TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, request TEXT NOT NULL, origin TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', result TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(
                    id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memories(
                    key TEXT PRIMARY KEY, content TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS todos(
                    id TEXT PRIMARY KEY, dedup TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open',
                    due_at REAL, remind_at REAL, repeat_hours INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(
                    id INTEGER PRIMARY KEY, dedup TEXT UNIQUE NOT NULL,
                    text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
                    file_path TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS inbox(
                    update_id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending');
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in db.execute("PRAGMA table_info(outbox)")}
            if "file_path" not in columns:
                db.execute("ALTER TABLE outbox ADD COLUMN file_path TEXT NOT NULL DEFAULT ''")
            if "sent_at" not in columns:
                db.execute("ALTER TABLE outbox ADD COLUMN sent_at REAL")
            if "context" not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
            for column, default in (("parent_id", ""), ("permissions", "{}")):
                if column not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
            # Migrate once; deleting this note must not resurrect the legacy preference.
            if not db.execute("SELECT 1 FROM settings WHERE key='memory_migrated'").fetchone():
                old = db.execute("SELECT value FROM settings WHERE key='preference'").fetchone()
                if old and old[0]:
                    db.execute("INSERT OR IGNORE INTO memories VALUES(?,?,?)", ("style", old[0][:2000], time.time()))
                db.execute("DELETE FROM settings WHERE key='preference'")
                db.execute("INSERT INTO settings VALUES('memory_migrated','1')")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, kind, request, origin, dedup, context=None, install_dependencies=False):
        if kind not in {"lms", "time", "homework", "tests", "submit"}:
            raise ValueError("Unknown job kind")
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,dedup,kind,request,origin,created,updated,context,permissions) VALUES(?,?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex[:12], dedup, kind, request, origin, time.time(), time.time(),
                        json.dumps(context or {}, ensure_ascii=False), json.dumps({"install_dependencies": kind == "homework" and install_dependencies is True})))
            return db.execute("SELECT id FROM jobs WHERE dedup=?", (dedup,)).fetchone()[0]

    def continue_homework(self, parent_id, request, dedup, context, install_dependencies=False):
        if not isinstance(parent_id, str) or not isinstance(request, str) or not request.strip() or len(request) > 12000 or type(install_dependencies) is not bool:
            raise ValueError("Нужны ID домашки и конкретное уточнение до 12000 символов")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT id FROM jobs WHERE dedup=?", (dedup,)).fetchone()
            if prior:
                return prior[0]
            parent = db.execute("SELECT * FROM jobs WHERE id=?", (parent_id,)).fetchone()
            if not parent or parent["kind"] != "homework":
                raise ValueError("Продолжение доступно только для подготовки домашки; тесты и сдачи так не перезапускаются")
            child = db.execute("SELECT id FROM jobs WHERE parent_id=? AND state IN ('queued','running') LIMIT 1", (parent_id,)).fetchone()
            if child:
                raise ValueError("Продолжение уже передано: " + child[0] + ". Новая задача не создана; проверь его статус.")
            inherited = json.loads(parent["permissions"])
            parent_context = json.loads(parent["context"])
            if parent_context.get("assignment_url") and context.get("assignment_url") != parent_context["assignment_url"]:
                inherited["install_dependencies"] = False
            inherited["install_dependencies"] = inherited.get("install_dependencies") is True or install_dependencies
            context = dict(context)
            previous = parent_context.get("original_homework_request", parent["request"])
            context["original_homework_request"] = previous
            job_id = uuid.uuid4().hex[:12]
            db.execute("INSERT INTO jobs(id,dedup,kind,request,origin,created,updated,context,parent_id,permissions) VALUES(?,?,'homework',?,'telegram-continuation',?,?,?,?,?)",
                       (job_id, dedup, request, time.time(), time.time(), json.dumps(context, ensure_ascii=False), parent_id, json.dumps(inherited)))
            return job_id

    def claim_job(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT j.* FROM jobs j WHERE j.state='queued' AND (j.parent_id='' OR EXISTS (SELECT 1 FROM jobs p WHERE p.id=j.parent_id AND p.state NOT IN ('queued','running'))) ORDER BY j.created LIMIT 1").fetchone()
            if job:
                db.execute("UPDATE jobs SET state='running', updated=? WHERE id=?", (time.time(), job["id"]))
                return dict(job)

    def finish(self, job_id, result):
        if result["status"] not in {"completed", "blocked", "failed", "unknown"}:
            raise ValueError("Invalid result status")
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?,result=?,updated=? WHERE id=? AND state='running'",
                       (result["status"], json.dumps(result, ensure_ascii=False), time.time(), job_id))

    def recover_interrupted(self):
        # Never repeat a test submission or other task after a crash with unknown effects.
        with self.connect() as db:
            result = json.dumps({"status": "unknown", "summary": "Ди был прерван; исход действий неизвестен. Автоповтора нет.",
                                 "records": [], "artifacts": [], "evidence": [], "source_errors": []}, ensure_ascii=False)
            db.execute("UPDATE jobs SET state='unknown',result=?,updated=? WHERE state='running'", (result, time.time()))

    def jobs(self, limit=10):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))]

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def unreported(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT j.* FROM jobs j WHERE result IS NOT NULL AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.dedup='job:'||j.id) ORDER BY created LIMIT 10")]

    def queue_text(self, dedup, text):
        # Plain text, safely within Telegram's UTF-16 limit.
        if not text or len(text.encode("utf-16-le")) // 2 > 4096:
            raise ValueError("Message exceeds Telegram limit")
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO outbox(dedup,text) VALUES(?,?)", (dedup, text))

    def claim_message(self, defer_scheduled_time=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT o.* FROM outbox o WHERE o.state='pending' AND (?=0 OR NOT EXISTS (SELECT 1 FROM jobs j WHERE o.dedup='job:'||j.id AND j.kind='time' AND j.origin='schedule')) ORDER BY o.id LIMIT 1", (int(defer_scheduled_time),)).fetchone()
            if row:
                db.execute("UPDATE outbox SET state='sending' WHERE id=?", (row["id"],))
                return dict(row)

    def queue_file(self, job_id, name):
        root = (self.root / "artifacts" / job_id).resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Invalid artifact for delivery")
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO outbox(dedup,text,file_path) VALUES(?,?,?)",
                       (f"file:{job_id}:{name}", f"Ди · {job_id}", str(path)))

    def delivery(self, item_id, state, message_id=None):
        with self.connect() as db:
            db.execute("UPDATE outbox SET state=?,message_id=?,sent_at=? WHERE id=?", (state, message_id, time.time() if state == "sent" else None, item_id))

    def setting(self, key, default=""):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def receive(self, update):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO inbox(update_id,payload) VALUES(?,?)",
                       (update["update_id"], json.dumps(update, ensure_ascii=False)))
            db.execute("INSERT INTO settings VALUES('telegram_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (str(update["update_id"] + 1),))

    def claim_update(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM inbox WHERE state='pending' ORDER BY update_id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE inbox SET state='processing' WHERE update_id=?", (row["update_id"],))
                return json.loads(row["payload"])

    def processed(self, update_id):
        with self.connect() as db:
            db.execute("UPDATE inbox SET state='done' WHERE update_id=?", (update_id,))

    def recover_updates(self):
        with self.connect() as db:
            # Processing may already have dispatched work. Never replay it blindly.
            rows = db.execute("SELECT update_id FROM inbox WHERE state='processing'").fetchall()
            for row in rows:
                db.execute("INSERT OR IGNORE INTO outbox(dedup,text) VALUES(?,?)",
                           (f"reply:{row[0]}", "Я перезапустилась на середине сообщения. Поставленные поручения сохранены в /status — повторно их не отправляла."))
            db.execute("UPDATE inbox SET state='interrupted' WHERE state='processing'")

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def remember_message(self, role, content):
        with self.connect() as db:
            db.execute("INSERT INTO messages(role,content) VALUES(?,?)", (role, content))

    def history(self):
        with self.connect() as db:
            rows = db.execute("SELECT role,content FROM messages ORDER BY id DESC LIMIT 40").fetchall()
            return [dict(row) for row in reversed(rows)]

    def summary_batch(self):
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            if count <= 60:
                return []
            batch, size = [], 0
            for row in db.execute("SELECT * FROM messages ORDER BY id LIMIT ?", (min(count - 40, 20),)):
                if batch and size + len(row["content"]) > 24000:
                    break
                batch.append(dict(row))
                size += len(row["content"])
            return batch

    def save_summary(self, text, through_id):
        with self.connect() as db:
            db.execute("INSERT INTO settings VALUES('conversation_summary',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (text,))
            db.execute("DELETE FROM messages WHERE id<=?", (through_id,))

    def memories(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT key,content FROM memories ORDER BY key")]

    def remember(self, key, content):
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", key):
            raise ValueError("Use a short lowercase memory key")
        if not isinstance(content, str) or not content.strip() or len(content) > 2000:
            raise ValueError("Memory must contain 1-2000 characters")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute("SELECT COALESCE(SUM(length(content)),0) FROM memories WHERE key!=?", (key,)).fetchone()[0]
            count = db.execute("SELECT COUNT(*) FROM memories WHERE key!=?", (key,)).fetchone()[0]
            if count >= 24 or used + len(content) > 8000:
                raise ValueError("Memory is full; remove or shorten an existing note")
            db.execute("INSERT INTO memories VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET content=excluded.content,updated=excluded.updated", (key, content, time.time()))

    def forget(self, key):
        with self.connect() as db:
            return bool(db.execute("DELETE FROM memories WHERE key=?", (key,)).rowcount)

    def recent_reports(self):
        with self.connect() as db:
            rows = db.execute("SELECT o.dedup,o.text,o.sent_at,j.updated AS job_updated FROM outbox o LEFT JOIN jobs j ON o.dedup='job:'||j.id WHERE o.state='sent' AND o.dedup LIKE 'job:%' ORDER BY o.id DESC LIMIT 3").fetchall()
            return [dict(r) for r in reversed(rows)]

    def todos(self, limit=30):
        with self.connect() as db:
            rows = db.execute("SELECT id,title,state,due_at,remind_at,repeat_hours FROM todos WHERE state='open' ORDER BY due_at IS NULL,due_at,created LIMIT ?", (limit,)).fetchall()
            return [self.todo_view(dict(r)) for r in rows]

    @staticmethod
    def todo_view(todo):
        for key in ("due_at", "remind_at"):
            if todo.get(key) is not None:
                todo[key] = datetime.fromtimestamp(todo[key], ZoneInfo("Europe/Moscow")).isoformat()
        return todo

    def change_todo(self, action, dedup, todo_id="", **fields):
        allowed = {"title", "due_at", "remind_at", "repeat_hours"}
        if set(fields) - allowed or action not in {"add", "update", "done", "cancel"}:
            raise ValueError("Invalid todo action")
        if not isinstance(todo_id, str) or len(todo_id) > 64:
            raise ValueError("Нужен ID задачи из списка дел")
        if "title" in fields and (not isinstance(fields["title"], str) or not fields["title"].strip() or len(fields["title"]) > 500):
            raise ValueError("Нужно название задачи до 500 символов")
        for key in ("due_at", "remind_at"):
            value = fields.get(key)
            if value is not None and (not isinstance(value, (float, int)) or not 0 < value < 32503680000):
                raise ValueError("Некорректное время")
        repeat = fields.get("repeat_hours", 0)
        if type(repeat) is not int or not 0 <= repeat <= 8760:
            raise ValueError("Повтор: целое число часов от 1 до 8760; 0 — без повтора")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if action == "add":
                prior = db.execute("SELECT * FROM todos WHERE dedup=?", (dedup,)).fetchone()
                if prior:
                    return self.todo_view(dict(prior))
                if "title" not in fields:
                    raise ValueError("Нужно название задачи")
                if db.execute("SELECT COUNT(*) FROM todos WHERE state='open'").fetchone()[0] >= 100:
                    raise ValueError("Сначала закрой часть задач: лимит — 100 открытых")
                todo_id = uuid.uuid4().hex[:8]
                db.execute("INSERT INTO todos(id,dedup,title,created,updated) VALUES(?,?,?,?,?)", (todo_id, dedup, fields["title"], time.time(), time.time()))
            current = db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
            if not current:
                raise ValueError("Такой задачи нет")
            if current["state"] != "open":
                if action in {"done", "cancel"}:
                    return self.todo_view(dict(current))
                raise ValueError("Эта задача уже закрыта")
            if action in {"done", "cancel"}:
                if fields:
                    raise ValueError("Закрытие задачи не принимает дополнительные поля")
                fields = {"state": "done" if action == "done" else "cancelled", "remind_at": None, "repeat_hours": 0}
            merged = {**dict(current), **fields}
            if merged["repeat_hours"] and merged["remind_at"] is None:
                raise ValueError("Для повтора нужно время первого напоминания")
            if fields:
                assignments = ",".join(key + "=?" for key in fields)
                db.execute(f"UPDATE todos SET {assignments},revision=revision+1,updated=? WHERE id=?", (*fields.values(), time.time(), todo_id))
                # A changed or closed task must not leave an obsolete unsent reminder.
                db.execute("UPDATE outbox SET state='cancelled' WHERE state='pending' AND dedup LIKE ?", (f"todo:{todo_id}:%",))
            return self.todo_view(dict(db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()))

    def schedule_reminders(self, now):
        stamp = now.timestamp()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM todos WHERE state='open' AND remind_at<=? ORDER BY remind_at LIMIT 30", (stamp,)).fetchall()
            for row in rows:
                due = ""
                if row["due_at"] is not None:
                    due = "\nСрок: " + datetime.fromtimestamp(row["due_at"], ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y в %H:%M МСК")
                text = "Не даём этому делу потеряться: " + row["title"] + due + f"\nСделано? /done {row['id']}"
                db.execute("INSERT OR IGNORE INTO outbox(dedup,text) VALUES(?,?)", (f"todo:{row['id']}:{row['revision']}:{row['remind_at']}", text))
                interval = row["repeat_hours"] * 3600
                # At most one catch-up reminder after downtime, never a flood.
                following = row["remind_at"] + (int((stamp - row["remind_at"]) // interval) + 1) * interval if interval else None
                db.execute("UPDATE todos SET remind_at=?,updated=? WHERE id=?", (following, stamp, row["id"]))
