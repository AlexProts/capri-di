"""Bounded, read-only views of private server memory for Capri and Di."""
import json
import re

from .relevance import digest_result


def bounded_history(store, budget=16000):
    messages = []
    for row in reversed(store.history()):
        if budget <= 0:
            break
        content = row["content"][:budget]
        messages.append({"role": row["role"], "content": content})
        budget -= len(content)
    return list(reversed(messages))


def result_view(job, budget=10000, query=""):
    view = {k: job[k] for k in ("id", "kind", "state", "created", "updated")}
    view["parent_id"] = job.get("parent_id", "")
    view["assignment_url"] = json.loads(job.get("context", "{}")).get("assignment_url", "")
    view["request"] = job["request"][:2000]
    if not job["result"]:
        return view
    result = json.loads(job["result"])
    if job["kind"] in {"lms", "time"} and result["status"] == "completed":
        result = digest_result(result)
    view["result"] = {}
    for key, value in result.items():
        if key == "records":
            continue
        view["result"][key] = value[:2000] if isinstance(value, str) else [v[:600] for v in value[:3]] if isinstance(value, list) else value
    view["result"]["records"] = []
    # Preserve whole records and their URLs; say explicitly when the view is partial.
    records = result.get("records", [])
    terms = {t for t in re.findall(r"[\w-]+", query.casefold()) if len(t) >= 3}
    if terms:
        records = sorted(records, key=lambda r: sum(t in json.dumps(r, ensure_ascii=False).casefold() for t in terms), reverse=True)
        view["records_reordered_for_search"] = True
    for record in records:
        if len(json.dumps(view, ensure_ascii=False)) + len(json.dumps(record, ensure_ascii=False)) > budget:
            view["records_truncated"] = True
            break
        view["result"]["records"].append(record)
    return view


def recall(store, query="", job_id=""):
    if job_id:
        job = store.job(job_id)
        return [result_view(job)] if job else []
    terms = {t for t in re.findall(r"[\w-]+", query.casefold()) if len(t) >= 3}
    matches = []
    for job in store.jobs(100 if terms else 3):
        # Search the complete filtered records, even when the returned view is bounded.
        result = json.loads(job["result"]) if job["result"] else {}
        if job["kind"] in {"lms", "time"} and result.get("status") == "completed":
            result = digest_result(result)
        searchable = (job["request"] + " " + json.dumps(result, ensure_ascii=False)).casefold()
        score = sum(term in searchable for term in terms)
        if not terms or score:
            matches.append((score, job))
    matches.sort(key=lambda item: item[0], reverse=True)
    return [result_view(job, query=query) for _, job in matches[:2]]


def conversation_context(store):
    return {"preferences_and_profile": store.memories(),
            "older_conversation_summary": store.setting("conversation_summary")[:6000],
            "recent_delivered_reports": store.recent_reports(),
            "recent_jobs": recall(store),
            "recent_homework_threads": [result_view(j, budget=4000) for j in [j for j in store.jobs(100) if j["kind"] == "homework"][:3]],
            "open_todos_first_12": store.todos(12)}


def delegation_context(store, request):
    # Capture at enqueue time, so later conversation cannot change an existing job.
    # Preferences are for Capri; Di only needs the concrete task and study references.
    return {"recent_conversation": bounded_history(store, 6000)[-8:],
            "latest_owner_message": next((r["content"] for r in reversed(store.history()) if r["role"] == "user"), ""),
            "older_conversation_summary": store.setting("conversation_summary")[:3000],
            "last_delivered_report": store.recent_reports()[-1:],
            "matching_past_jobs": recall(store, request)}
