"""Resolve literal answers to a stored assignment choice without another LLM guess."""
import re


def is_assignment_choice(pending):
    if pending.get("action") in {"delegate_homework", "continue_homework"}:
        return True
    if pending.get("workflow") not in {"conversation", "new_homework", "continue_homework"}:
        return False
    numbers = re.findall(r"\b(?:дз|hw)\s*(\d+(?:\.\d+)*)", pending.get("question", ""), re.I)
    return len(set(numbers)) > 1


def selected_assignment(candidates, text):
    answer = text.strip().casefold().rstrip(".!?")
    ordinals = {"первую": "1", "вторую": "2", "третью": "3", "четвертую": "4", "четвёртую": "4",
                "пятую": "5", "шестую": "6", "седьмую": "7", "восьмую": "8", "девятую": "9", "десятую": "10"}
    answer = ordinals.get(answer, answer)
    number = re.fullmatch(r"(?:дз\s*|№\s*)?(\d+(?:\.\d+)*)", answer)
    matches = []
    for candidate in candidates:
        title = candidate.get("title", "")
        title_number = re.search(r"\b(?:дз|hw)\s*[№#]?\s*(\d+(?:\.\d+)*)", title, re.I)
        if answer in {candidate.get("url", "").casefold(), title.casefold()} or (number and title_number and number[1] == title_number[1]):
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None
