"""SQLite schema and helper functions. Plain stdlib sqlite3, no ORM.

The schema is created once, here, on first connect if the tables don't exist
yet — there is no migration system (see kankor_quiz_bot_spec.md §0/§5). New
columns are added additively via _ensure_columns() below so an existing
kankor.db from an earlier run keeps working without a real migration tool.

questions.status flow:
    raw -> pending_review -> in_review (claimed by one admin via /next,
    claimed_by set) -> approved -> queued -> posted, or rejected at any point
Any admin in config.telegram.admin_user_ids may claim/approve/reject; skip
releases a claimed row back to pending_review so it can be claimed again.

'queued' is what stops one question filling every slot in the day: the
scheduler claims a question the moment it builds a slot for it, so the next
slot's lookup can't hand back the same row. It is not the same as 'posted' —
a question sits in 'queued' from the moment its slot is created until Facebook
actually accepts the post, which can be hours later.

post_queue.status flow:
    pending -> (scheduler, once scheduled_at is due) -> awaiting_approval ->
    (review_bot.py sends the rendered post + caption to Telegram; an admin taps
    Publish or Reject) -> done / rejected
Answer-story rows skip approval entirely: they carry parent_id pointing at the
post they answer, and auto-publish once due if — and only if — that parent
reached 'done'. Rejecting a post therefore cancels its answer story too.

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
    last_error      TEXT,
    parent_id       INTEGER
);

CREATE TABLE IF NOT EXISTS source_state (
    source_handle       TEXT PRIMARY KEY,
    last_seen_message_id INTEGER DEFAULT 0,
    backfill_complete    INTEGER DEFAULT 0,
    messages_scanned     INTEGER DEFAULT 0,
    questions_found      INTEGER DEFAULT 0,
    updated_at            TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT
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
    _ensure_columns(conn, "post_queue", {"parent_id": "INTEGER"})
    conn.commit()
    return conn


def _ensure_columns(conn, table, columns):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, col_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


# ---- settings ------------------------------------------------------------
# Choices made from Telegram that must outlive a restart. They live here rather
# than in config.yaml so changing one never rewrites the file the user edits by
# hand — a rewrite would strip every comment out of it.

def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
    conn.commit()


def get_int_setting(conn, key, default=None):
    raw = get_setting(conn, key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


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


def recover_interrupted_publishes(conn):
    """Clean up posts that were mid-publish when the app stopped.

    'publishing' is held only for the seconds a Facebook upload takes, purely so
    a second reviewer's tap cannot start the same upload twice. If the app dies
    inside that window the row would otherwise sit in a state nothing looks at,
    silently costing that slot its post.

    The two kinds recover differently. A scheduled post goes back to 'pending'
    for the scheduler to offer again. An instant post has no scheduler watching
    it, so it is closed out and its question returned to the approved pool,
    where it will be picked up by an ordinary slot.

    Returns (rescheduled, returned_to_pool).
    """
    scheduled = conn.execute(
        "UPDATE post_queue SET status = 'pending' WHERE status = 'publishing' AND kind != 'instant'"
    ).rowcount

    stalled = conn.execute(
        "SELECT id, question_ids FROM post_queue WHERE status = 'publishing' AND kind = 'instant'"
    ).fetchall()
    for row in stalled:
        conn.execute(
            "UPDATE post_queue SET status = 'failed', last_error = ? WHERE id = ?",
            ("the app stopped while publishing", row["id"]),
        )
        conn.execute(
            "UPDATE post_queue SET status = 'cancelled', last_error = ? "
            "WHERE parent_id = ? AND status = 'pending'",
            ("its post never published", row["id"]),
        )
        for qid in json.loads(row["question_ids"]):
            conn.execute(
                "UPDATE questions SET status = 'approved' WHERE id = ? AND status = 'queued'",
                (qid,),
            )

    conn.commit()
    return scheduled, len(stalled)


def release_all_in_review(conn):
    """Put every claimed-but-undecided question back in the pool.

    Which question a reviewer is currently looking at is only ever held in
    memory, so after a restart nothing knows about an in_review row and it would
    sit there unreviewable forever. Called once at startup.
    """
    cur = conn.execute(
        "UPDATE questions SET status = 'pending_review', claimed_by = NULL WHERE status = 'in_review'"
    )
    conn.commit()
    return cur.rowcount


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


def claim_next_approved_question(conn):
    """Take the oldest approved question and mark it 'queued' in one step.

    The mark is the whole point: slots for the day are built in a single pass,
    so without it every slot would look up "oldest approved" and get the same
    row back. Returns None when nothing is approved yet.
    """
    # Ordered by when it was approved, not by id. Returning a question to the
    # pool stamps it with a fresh time, which sends it to the back — otherwise
    # a question removed from a slot would have the lowest id in the pool and
    # would win that same slot straight back, forever.
    row = conn.execute(
        "SELECT * FROM questions WHERE status = 'approved' "
        "ORDER BY COALESCE(reviewed_at, '') , id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE questions SET status = 'queued' WHERE id = ?", (row["id"],))
    conn.commit()
    question = dict(row)
    question["status"] = "queued"
    return question


def count_questions_by_status(conn):
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM questions GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def unqueue_questions(conn, question_ids, status):
    """Send queued questions back out of the queue — 'approved' to try again
    later, 'rejected' to retire them.

    Going back to 'approved' re-stamps reviewed_at so the question joins the
    back of the pool (see claim_next_approved_question) instead of instantly
    reclaiming the slot it was just taken out of.
    """
    for qid in question_ids:
        if status == "approved":
            conn.execute(
                "UPDATE questions SET status = ?, reviewed_at = ? WHERE id = ? AND status = 'queued'",
                (status, _now(), qid),
            )
        else:
            conn.execute(
                "UPDATE questions SET status = ? WHERE id = ? AND status = 'queued'",
                (status, qid),
            )
    conn.commit()


def todays_schedule(conn, day_prefix):
    """Every scheduled post for one day, newest status first, for /queue."""
    rows = conn.execute(
        "SELECT * FROM post_queue WHERE kind = 'daily' AND scheduled_at LIKE ? "
        "AND status NOT IN ('rejected', 'cancelled', 'skipped') "
        "ORDER BY scheduled_at",
        (day_prefix + "%",),
    ).fetchall()
    return [dict(r) for r in rows]


def cancel_queue_entry(conn, entry_id, reason):
    """Drop one scheduled post and its answer story, freeing its slot."""
    conn.execute(
        "UPDATE post_queue SET status = 'cancelled', last_error = ? WHERE id = ?",
        (reason, entry_id),
    )
    conn.execute(
        "UPDATE post_queue SET status = 'cancelled', last_error = ? "
        "WHERE parent_id = ? AND status = 'pending'",
        (reason, entry_id),
    )
    conn.commit()


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

def enqueue_post(conn, kind, lang, question_ids, scheduled_at, parent_id=None):
    cur = conn.execute(
        "INSERT INTO post_queue (kind, lang, question_ids, scheduled_at, parent_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (kind, lang, json.dumps(question_ids), scheduled_at, parent_id),
    )
    conn.commit()
    return cur.lastrowid


def queue_slot_filled(conn, kind, scheduled_at):
    """Is this time slot still occupied by a post that might yet go out?

    Rejected and cancelled entries deliberately do NOT count as filling the
    slot. That is what makes a rejection self-healing: the slot reads as empty
    again, so the scheduler's next pass drops a fresh question into it and the
    day still reaches its full number of posts.
    """
    row = conn.execute(
        "SELECT 1 FROM post_queue WHERE kind = ? AND scheduled_at = ? "
        "AND status NOT IN ('rejected', 'cancelled', 'skipped')",
        (kind, scheduled_at),
    ).fetchone()
    return row is not None


def get_due_queue_entries(conn, now_iso, kind):
    rows = conn.execute(
        "SELECT * FROM post_queue WHERE status = 'pending' AND kind = ? AND scheduled_at <= ? "
        "ORDER BY scheduled_at",
        (kind, now_iso),
    ).fetchall()
    return [dict(r) for r in rows]


def get_due_answer_stories(conn, now_iso):
    """Answer stories that are due AND whose post actually made it to Facebook.

    The parent join is what makes rejecting a post also cancel its answer —
    without it, rejecting the question still leaves its answer story armed and
    it would publish an answer to a post nobody ever saw.
    """
    rows = conn.execute(
        "SELECT q.* FROM post_queue q "
        "JOIN post_queue parent ON parent.id = q.parent_id "
        "WHERE q.status = 'pending' AND q.kind = 'story_a' AND q.scheduled_at <= ? "
        "AND parent.status = 'done' "
        "ORDER BY q.scheduled_at",
        (now_iso,),
    ).fetchall()
    return [dict(r) for r in rows]


def cancel_child_entries(conn, parent_id, reason):
    conn.execute(
        "UPDATE post_queue SET status = 'cancelled', last_error = ? "
        "WHERE parent_id = ? AND status = 'pending'",
        (reason, parent_id),
    )
    conn.commit()


def count_queue_by_status(conn):
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM post_queue GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


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
