"""Bounded, read-only views of private server memory for Capri and Di."""
import json
import re

from .relevance import digest_result, relevant_record


def study_terms(text):
    text = text.casefold()
    for pattern, canonical in ((r"\b(?:рек[\s-]*сис(?:ом|у|а|е)?|рексис(?:ом|у|а|е)?|recsys)\b", "рекомендательные системы"),
                               (r"\bрекомендательн\w*\s+систем\w*", "рекомендательные системы"),
                               (r"\b(?:прод[\s-]*мл|prodml)\b", "production ml")):
        text = re.sub(pattern, canonical, text)
    return {w for w in re.findall(r"[\w-]+", text) if len(w) >= 3}


def find_assignment(store, query):
    terms = study_terms(query)
    found = {}
    for job in store.jobs(100):
        if not job["result"]:
            continue
        result = json.loads(job["result"])
        if result.get("status") != "completed":
            continue
        for record in result.get("records", []):
            if record.get("source") != "lms" or not relevant_record(record) or re.search(r"\b(?:тест|test)\b", record.get("title", ""), re.I):
                continue
            haystack = " ".join(record.get(k, "") for k in ("title", "course", "details")).casefold()
            course_score = sum(term in record.get("course", "").casefold() for term in terms)
            score = 10 * course_score + sum(term in haystack for term in terms)
            url = record["url"]
            if score and url not in found:
                found[url] = {**record, "collected_at": result.get("collected_at", ""), "score": score}
    matches = sorted(found.values(), key=lambda r: r["score"], reverse=True)
    if matches and matches[0]["score"] >= 10:
        matches = [r for r in matches if r["score"] >= 10]
    return matches[:8]


def previous_homework(store, assignment_url):
    jobs = store.jobs(100)
    parents = {j["parent_id"] for j in jobs if j.get("parent_id")}
    leaves = [j for j in jobs if j["kind"] == "homework" and j["id"] not in parents]
    for job in leaves:
        known = json.loads(job["context"]).get("assignment_url", "")
        if known == assignment_url or assignment_url in job["request"]:
            return job
    # Legacy jobs had a course name but no URL. Reuse only a unique matching
    # course thread; never pick one of several old workspaces arbitrarily.
    course = ""
    for job in jobs:
        result = json.loads(job["result"]) if job["result"] else {}
        for record in result.get("records", []):
            if record.get("url") == assignment_url:
                course = record.get("course", "")
                break
        if course:
            break
    tags = study_terms(course) & {"рекомендательные", "nlp", "production", "agile", "технологическое"}
    matches = [j for j in leaves if not json.loads(j["context"]).get("assignment_url") and tags & study_terms(j["request"])]
    return matches[0] if len(matches) == 1 else None


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
            "pending_owner_request": json.loads(store.setting("pending_workflow", "{}")),
            "older_conversation_summary": store.setting("conversation_summary")[:3000],
            "last_delivered_report": store.recent_reports()[-1:],
            "matching_past_jobs": recall(store, request)}
