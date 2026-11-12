import argparse
from datetime import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import time

from .capri import Capri, schedule, time_quiet
from .config import Config
from .network import OpenRouter, RemoteError, Telegram
from .store import Store
from .worker import Worker

log = logging.getLogger("capri-di")


def handle_update(config, store, capri, update):
    message = update.get("message", {})
    sender = message.get("from", {})
    chat = message.get("chat", {})
    if (chat.get("type") != "private" or chat.get("id") != config.owner
            or sender.get("id") != config.owner or sender.get("is_bot")):
        return
    text = message.get("text")
    if not isinstance(text, str):
        text = "/unsupported"
        answer = "Пока принимаю текст и ссылки. Файлы можно оставить в LMS или рабочей папке Ди."
    else:
        answer = capri.reply(text, update["update_id"])
    store.queue_text(f"reply:{update['update_id']}", answer)


def run_bot(config):
    if not config.telegram_token or not config.bot_username or not config.openrouter_key or not config.model or config.owner <= 0:
        raise ValueError("Fill TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME, TELEGRAM_OWNER_ID, OPENROUTER_API_KEY, OPENROUTER_MODEL")
    store = Store(config.state)
    telegram = Telegram(config.telegram_token, config.owner, config.bot_username)
    capri = Capri(config, store, OpenRouter(config.openrouter_key, config.model))
    with (config.state / "capri.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        telegram.verify()
        store.recover_updates()
        log.info("Capri ready; owner and bot verified")
        while True:
            try:
                schedule(config, store, datetime.now().astimezone())
                for job in store.unreported():
                    if job["kind"] == "time" and job["origin"] == "schedule" and time_quiet(config, datetime.now().astimezone()):
                        continue  # A late collection must not wake the owner overnight.
                    try:
                        report = capri.report(job)
                    except RemoteError as error:
                        # Collection/results still delivered without LLM formatting on provider failure.
                        log.warning("Digest formatter unavailable for job %s: %s; using compact fallback", job["id"], error)
                        report = capri.fallback(job, json.loads(job["result"]))
                    store.queue_text("job:" + job["id"], report)
                while item := store.claim_message(defer_scheduled_time=time_quiet(config, datetime.now().astimezone())):
                    try:
                        mid = telegram.send_file(item["file_path"], item["text"]) if item["file_path"] else telegram.send(item["text"])
                        store.delivery(item["id"], "sent", mid)
                    except RemoteError as error:
                        store.delivery(item["id"], "unknown" if error.uncertain else "rejected")
                        log.warning("Telegram delivery %s; no automatic retry", item["id"])
                offset = int(store.setting("telegram_offset", "0"))
                for update in telegram.updates(offset):
                    # Persist receipt and offset atomically before processing.
                    message = update.get("message", {})
                    if (message.get("chat", {}).get("id") == config.owner
                            and message.get("from", {}).get("id") == config.owner
                            and message.get("chat", {}).get("type") == "private"
                            and not message.get("from", {}).get("is_bot")):
                        store.receive(update)
                    else:
                        store.set_setting("telegram_offset", str(update["update_id"] + 1))
                while update := store.claim_update():
                    try:
                        handle_update(config, store, capri, update)
                    except RemoteError:
                        answer = "На связи с моделью случился сбой. Поручения Ди можно проверить в /status, дела — в /todo, заметки — в /memory. Повторно ничего не отправляю."
                        store.remember_message("assistant", answer)
                        store.queue_text(f"reply:{update['update_id']}", answer)
                    store.processed(update["update_id"])
            except RemoteError as error:
                log.warning("Remote request failed: %s", error)
                time.sleep(5)


def main():
    os.umask(0o007)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["capri", "di", "status"])
    args = parser.parse_args()
    config = Config.load()
    try:
        if args.role == "capri":
            run_bot(config)
        elif args.role == "di":
            Worker(Store(config.state), Path(os.environ.get("DI_WORK_DIR", "state/di/jobs")),
                   os.environ.get("CODEX_BIN", "codex"), int(os.environ.get("DI_TIMEOUT_SECONDS", "3600"))).serve()
        else:
            for job in Store(config.state).jobs():
                print(job["id"], job["kind"], job["state"])
    except (ValueError, RemoteError, BlockingIOError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
