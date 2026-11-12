"""Default digest scope; never infer irrelevance from an old deadline alone."""
import re
from urllib.parse import urlsplit


def normalized(text):
    return re.sub(r"\s+", " ", text.casefold().replace("ё", "е")).strip()


def relevant_record(record):
    if record.get("source") != "lms":
        return True
    if "/archived/" in urlsplit(record.get("url", "")).path:
        return False
    title = normalized(record.get("title", ""))
    course = normalized(record.get("course", ""))
    if re.search(r"\bне[ -]?актуаль", title) or "утратило актуальность" in title:
        return False
    if ("ознакомлен" in course and ("норматив" in course or "приказ" in course)):
        return False
    if title.startswith(("ознакомление с норматив", "ознакомление с локально-норматив", "положение о порядке организации и проведения гиа")):
        return False
    status = normalized(record.get("status", ""))
    if re.match(r"^(на проверке|сдано|проверено|зачтено|завершено|выполнено|оценено|submitted|under review|completed|graded)\b", status):
        return False
    return True


def digest_result(result):
    # Leave the raw result untouched. Do not send its old summary/evidence to the formatter:
    # they can still mention archived tasks that have been excluded from records.
    return {"status": result["status"], "collected_at": result.get("collected_at", ""),
            "records": [r for r in result.get("records", []) if relevant_record(r)],
            "source_errors": result.get("source_errors", [])}
