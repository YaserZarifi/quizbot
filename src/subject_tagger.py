"""Subject tagging: position-based table, then keyword match, then default.

Stops at the first hit. Never blocks ingestion — always returns something,
falling back to 'عمومی' with zero confidence if nothing else matches.
"""

import json

DEFAULT_SUBJECT = "عمومی"


def load_position_ranges(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("ranges", [])  # each: {"subject": ..., "from": int, "to": int}


def load_subject_keywords(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def tag_subject(question_text, set_position, position_ranges, subject_keywords):
    # 1. Position-based lookup
    if set_position is not None:
        for r in position_ranges:
            if r["from"] <= set_position <= r["to"]:
                return r["subject"], "position", 1.0

    # 2. Keyword match
    if question_text:
        for subject, keywords in subject_keywords.items():
            for kw in keywords:
                if kw in question_text:
                    return subject, "keyword", 0.6

    # 3. Default — never blocks ingestion
    return DEFAULT_SUBJECT, "default", 0.0
