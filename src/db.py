"""SQLite schema and helper functions. Plain stdlib sqlite3, no ORM.

The schema is created once, here, on first connect if the tables don't exist
yet — there is no migration system (see kankor_quiz_bot_spec.md §0/§5). New
columns are added additively via _ensure_columns() below so an existing
kankor.db from an earlier run keeps working without a real migration tool.

questions.status flow:
    raw -> pending_review -> in_review (claimed by one admin via /next,
    claimed_by set) -> approved / rejected -> posted
Any admin in config.telegram.admin_user_ids may claim/approve/reject; skip
releases a claimed row back to pending_review so it can be claimed again.

post_queue.status flow:
    pending -> (main.py, once scheduled_at is due) -> awaiting_approval ->
    (review_bot.py sends the actual rendered post + caption to Telegram;
    an admin taps Publish or Reject) -> done / rejected
A failed Facebook call drops a row back to pending for the next tick to
retry (bumping attempts); more than 4 hours late gets skipped instead of
ever reaching Telegram.
"""

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id       TEXT UNIQUE,
    source_channel  TEXT,
    tg_chat_id      INTEGER,
    tg_message_id   INTEGER,
    lang            TEXT,
    question_type   TEXT,
    question_text   TEXT,
    image_path      TEXT,
    option_a        TEXT,
    option_b        TEXT,
    option_c        TEXT,
    option_d        TEXT,
    correct_index   INTEGER,
    subject         TEXT,
    subject_method  TEXT,
    subject_conf    REAL,
    set_position    INTEGER,
    set_total       INTEGER,
    fingerprint     TEXT UNIQUE,
    lexicon_flag    INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'raw',
    claimed_by      INTEGER,
    reject_reason   TEXT,
    created_at      TEXT,
    reviewed_at     TEXT,
    posted_at       TEXT,
    answer_posted_at TEXT,
    fb_feed_post_id TEXT,
    fb_story_id     TEXT,
    fb_answer_story_id TEXT
);

CREATE TABLE IF NOT EXISTS post_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT,
    lang            TEXT,
    question_ids    TEXT,
    scheduled_at    TEXT,
    status          TEXT DEFAULT 'pending',
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS source_state (
    source_handle       TEXT PRIMARY KEY,
    last_seen_message_id INTEGER DEFAULT 0,
    backfill_complete    INTEGER DEFAULT 0,
    messages_scanned     INTEGER DEFAULT 0,
    questions_found      INTEGER DEFAULT 0,
    updated_at            TEXT
);

CREATE INDEX IF NOT EXISTS idx_questions_status ON questions(status);
CREATE INDEX IF NOT EXISTS idx_questions_lang ON questions(lang);
CREATE INDEX IF NOT EXISTS idx_post_queue_status ON post_queue(status, scheduled_at);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _ensure_columns(conn, "questions", {"claimed_by": "INTEGER"})
    conn.commit()
    return conn


def _ensure_columns(conn, table, columns):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, col_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


# ---- source_state -----------------------------------------------------

def get_source_state(conn, source_handle):
    row = conn.execute(
        "SELECT * FROM source_state WHERE source_handle = ?", (source_handle,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO source_state (source_handle) VALUES (?)", (source_handle,)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM source_state WHERE source_handle = ?", (source_handle,)
        ).fetchone()
    return dict(row)


def update_source_state(conn, source_handle, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE source_state SET {cols} WHERE source_handle = ?",
        (*fields.values(), source_handle),
    )
    conn.commit()


# ---- questions ----------------------------------------------------------

def fingerprint_exists(conn, fingerprint):
    row = conn.execute(
        "SELECT 1 FROM questions WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    return row is not None


def next_public_id(conn):
    row = conn.execute(
        "SELECT public_id FROM questions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None or not row["public_id"]:
        n = 1
    else:
        n = int(row["public_id"].lstrip("K")) + 1
    return f"K{n:04d}"


def insert_question(conn, **fields):
    fields.setdefault("status", "pending_review")
    fields.setdefault("created_at", _now())
    fields.setdefault("public_id", next_public_id(conn))
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO questions ({cols}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    conn.commit()
    return cur.lastrowid


def claim_next_pending_review(conn, admin_id):
    """Atomically hand the oldest pending_review row to one admin: marks it
    in_review so a second admin's /next gets a different row instead of the
    same one (see status flow note at the top of this file)."""
    row = conn.execute(
        "SELECT * FROM questions WHERE status = 'pending_review' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE questions SET status = 'in_review', claimed_by = ?, reviewed_at = ? WHERE id = ?",
        (admin_id, _now(), row["id"]),
    )
    conn.commit()
    question = dict(row)
    question["status"] = "in_review"
    question["claimed_by"] = admin_id
    return question


def release_question(conn, question_id):
    """Skip: release a claimed row back to pending_review so any admin can claim it again."""
    conn.execute(
        "UPDATE questions SET status = 'pending_review', claimed_by = NULL WHERE id = ?",
        (question_id,),
    )
    conn.commit()


def set_question_status(conn, question_id, status, reject_reason=None):
    conn.execute(
        "UPDATE questions SET status = ?, reject_reason = ?, reviewed_at = ? WHERE id = ?",
        (status, reject_reason, _now(), question_id),
    )
    conn.commit()


def get_approved_questions(conn, lang, limit):
    rows = conn.execute(
        "SELECT * FROM questions WHERE status = 'approved' AND lang = ? "
        "ORDER BY id LIMIT ?",
        (lang, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_questions_posted(conn, question_ids, fb_feed_post_id=None, fb_story_id=None):
    now = _now()
    for qid in question_ids:
        conn.execute(
            "UPDATE questions SET status = 'posted', posted_at = ?, "
            "fb_feed_post_id = COALESCE(?, fb_feed_post_id), "
            "fb_story_id = COALESCE(?, fb_story_id) WHERE id = ?",
            (now, fb_feed_post_id, fb_story_id, qid),
        )
    conn.commit()


def mark_answers_posted(conn, question_ids, fb_answer_story_id=None):
    now = _now()
    for qid in question_ids:
        conn.execute(
            "UPDATE questions SET answer_posted_at = ?, "
            "fb_answer_story_id = COALESCE(?, fb_answer_story_id) WHERE id = ?",
            (now, fb_answer_story_id, qid),
        )
    conn.commit()


def get_questions_by_ids(conn, question_ids):
    if not question_ids:
        return []
    placeholders = ", ".join("?" for _ in question_ids)
    rows = conn.execute(
        f"SELECT * FROM questions WHERE id IN ({placeholders})", tuple(question_ids)
    ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[qid] for qid in question_ids if qid in by_id]


# ---- post_queue -----------------------------------------------------------

def enqueue_post(conn, kind, lang, question_ids, scheduled_at):
    conn.execute(
        "INSERT INTO post_queue (kind, lang, question_ids, scheduled_at) VALUES (?, ?, ?, ?)",
        (kind, lang, json.dumps(question_ids), scheduled_at),
    )
    conn.commit()


def queue_entry_exists(conn, kind, lang, scheduled_at):
    row = conn.execute(
        "SELECT 1 FROM post_queue WHERE kind = ? AND lang = ? AND scheduled_at = ?",
        (kind, lang, scheduled_at),
    ).fetchone()
    return row is not None


def get_due_queue_entries(conn, now_iso):
    rows = conn.execute(
        "SELECT * FROM post_queue WHERE status = 'pending' AND scheduled_at <= ? "
        "ORDER BY scheduled_at",
        (now_iso,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_awaiting_approval_entries(conn):
    rows = conn.execute(
        "SELECT * FROM post_queue WHERE status = 'awaiting_approval' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_queue_entry(conn, entry_id):
    row = conn.execute("SELECT * FROM post_queue WHERE id = ?", (entry_id,)).fetchone()
    return dict(row) if row else None


def set_queue_status(conn, entry_id, status, last_error=None, bump_attempts=False):
    if bump_attempts:
        conn.execute(
            "UPDATE post_queue SET status = ?, last_error = ?, attempts = attempts + 1 "
            "WHERE id = ?",
            (status, last_error, entry_id),
        )
    else:
        conn.execute(
            "UPDATE post_queue SET status = ?, last_error = ? WHERE id = ?",
            (status, last_error, entry_id),
        )
    conn.commit()


def _now():
    import datetime

    return datetime.datetime.now().isoformat(timespec="seconds")
