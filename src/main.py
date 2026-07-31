"""Scheduler entry point — this is what the .exe runs. See spec §8.5.

Every 15 minutes: build today's post_queue entries if missing, then hand off
whatever is due to Telegram for admin approval. This process never calls
Facebook directly — it only flips due 'pending' rows to 'awaiting_approval';
review_bot.py is what renders the preview, sends it with Publish/Reject
buttons, and actually calls the Facebook API once an admin approves. Nothing
here needs cron/Task Scheduler — just leave the window open (or launch it at
login via Task Scheduler, documented in README).
"""

import os
import sys
import time
from datetime import datetime, timedelta

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import logger

COMPONENT = "main"
LOOP_INTERVAL_SECONDS = 15 * 60
LATE_CUTOFF = timedelta(hours=4)
KNOWN_KINDS = {"feed", "story_q", "story_a"}


def _base_dir():
    """Frozen exe: the folder holding the .exe. Source: the project root."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(path="config.yaml"):
    if not os.path.isabs(path):
        path = os.path.join(_base_dir(), path)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _today_at(time_str):
    hh, mm = map(int, time_str.split(":"))
    return datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)


def build_daily_queue(conn, cfg):
    """Idempotent: safe to call every loop tick. Skips a language once today's
    entries already exist for it, so approved-question counts at startup time
    are what get queued, not whatever is approved later in the day."""
    schedule = cfg["schedule"]
    batch_size = schedule["questions_per_batch"]
    answer_delay = timedelta(hours=schedule["answer_delay_hours"])
    lang_times = {"fa": schedule["fa_post_time"], "ps": schedule["ps_post_time"]}

    for lang, post_time in lang_times.items():
        scheduled_dt = _today_at(post_time)
        scheduled_iso = scheduled_dt.isoformat(timespec="seconds")

        if db.queue_entry_exists(conn, "feed", lang, scheduled_iso):
            continue

        questions = db.get_approved_questions(conn, lang, batch_size)
        if not questions:
            logger.warn(COMPONENT, f"no approved {lang} questions available yet for today's batch")
            continue

        question_ids = [q["id"] for q in questions]
        answer_iso = (scheduled_dt + answer_delay).isoformat(timespec="seconds")

        db.enqueue_post(conn, "feed", lang, question_ids, scheduled_iso)
        db.enqueue_post(conn, "story_q", lang, question_ids, scheduled_iso)
        db.enqueue_post(conn, "story_a", lang, question_ids, answer_iso)
        logger.ok(COMPONENT, f"queued today's {lang} batch — {len(question_ids)} questions")


def process_due_queue(conn):
    """Flip due entries to 'awaiting_approval' — review_bot.py picks those up,
    sends the preview + Publish/Reject buttons to Telegram, and only calls
    Facebook once an admin taps Publish."""
    now = datetime.now()
    entries = db.get_due_queue_entries(conn, now.isoformat(timespec="seconds"))
    for entry in entries:
        scheduled_dt = datetime.fromisoformat(entry["scheduled_at"])
        if now - scheduled_dt > LATE_CUTOFF:
            db.set_queue_status(conn, entry["id"], "skipped", "more than 4 hours late")
            logger.warn(
                COMPONENT,
                f"queue entry #{entry['id']} ({entry['kind']}/{entry['lang']}) skipped — more than 4 hours late",
            )
            continue

        if entry["kind"] not in KNOWN_KINDS:
            logger.error(COMPONENT, f"unknown queue entry kind '{entry['kind']}' for #{entry['id']}")
            continue

        db.set_queue_status(conn, entry["id"], "awaiting_approval")
        logger.ok(
            COMPONENT,
            f"queue entry #{entry['id']} ({entry['kind']}/{entry['lang']}) ready — sent to admins for approval",
        )


def run_main(config_path="config.yaml"):
    # config.yaml holds relative paths (db_file, log_dir, output_dir); anchor
    # them to the exe folder so a launcher's working directory can't move them.
    os.chdir(_base_dir())
    cfg = load_config(config_path)
    os.environ.setdefault("KANKOR_LOG_DIR", cfg["paths"].get("log_dir", "logs"))
    conn = db.connect(cfg["paths"]["db_file"])

    logger.ok(COMPONENT, "kankor-bot started")
    logger.info(
        COMPONENT,
        f"checking post queue every {LOOP_INTERVAL_SECONDS // 60} minutes — "
        "leave this window open, and review_bot.py running to approve posts",
    )
    logger.blank()

    while True:
        try:
            build_daily_queue(conn, cfg)
            process_due_queue(conn)
        except Exception as exc:
            logger.error(COMPONENT, "unexpected error during scheduler loop — see error.log", exc)
        time.sleep(LOOP_INTERVAL_SECONDS)


def main():
    try:
        run_main()
    except KeyboardInterrupt:
        logger.info(COMPONENT, "stopped")
        return
    except FileNotFoundError as exc:
        logger.error(COMPONENT, f"could not open {exc.filename} — copy config.yaml next to kankor-bot.exe", exc)
    except Exception as exc:
        logger.error(COMPONENT, f"could not start: {exc} — see logs/error.log", exc)
    _hold_window_open()


def _hold_window_open():
    """A double-clicked exe closes its console the instant it exits, taking the
    error message with it. Only pause when nobody is watching the output."""
    if not getattr(sys, "frozen", False):
        return
    try:
        input("\nPress Enter to close...")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
