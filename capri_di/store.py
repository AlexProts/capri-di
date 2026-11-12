from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "assistant.db"
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
                CREATE TABLE IF NOT EXISTS outbox(
                    id INTEGER PRIMARY KEY, dedup TEXT UNIQUE NOT NULL,
                    text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', message_id INTEGER);
                CREATE TABLE IF NOT EXISTS inbox(
                    update_id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending');
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(outbox)")}
            if "file_path" not in columns:
                db.execute("ALTER TABLE outbox ADD COLUMN file_path TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, kind, request, origin, dedup):
        if kind not in {"lms", "time", "homework", "tests", "submit"}:
            raise ValueError("Unknown job kind")
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,dedup,kind,request,origin,created,updated) VALUES(?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex[:12], dedup, kind, request, origin, time.time(), time.time()))
            return db.execute("SELECT id FROM jobs WHERE dedup=?", (dedup,)).fetchone()[0]

    def claim_job(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
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

    def claim_message(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM outbox WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
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
            db.execute("UPDATE outbox SET state=?,message_id=? WHERE id=?", (state, message_id, item_id))

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
                           (f"reply:{row[0]}", "Alex, Капри перезапустилась во время обработки сообщения. Проверь /status: поставленные поручения сохранены, автоповтора нет."))
            db.execute("UPDATE inbox SET state='interrupted' WHERE state='processing'")

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def remember_message(self, role, content):
        with self.connect() as db:
            db.execute("INSERT INTO messages(role,content) VALUES(?,?)", (role, content))
            db.execute("DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY id DESC LIMIT 40)")

    def history(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT role,content FROM messages ORDER BY id")]
