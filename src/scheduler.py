"""Decides what gets posted and when. See kankor_quiz_bot_spec.md §8.5.

One question per post. Each day the scheduler makes sure every time slot in
config.schedule.post_times holds one approved question, and pairs it with an
answer story timed answer_delay_hours later.

Two rules shape everything here:

  A slot is "filled" only by an entry that might still go out. Rejecting a post
  empties its slot, so the very next pass refills it from the next approved
  question. That is how a day still reaches its full number of posts no matter
  how many candidates get rejected along the way.

  Slots are only honoured on the day they belong to. A slot left unfilled
  because the app was switched off does not fire at three in the morning when
  the app comes back — it is retired and the day moves on.
"""

import asyncio
import json
from datetime import datetime, timedelta

import db
import logger
import publish

COMPONENT = "scheduler"
TICK_SECONDS = 60

# Set by the review bot when a post is rejected, so the replacement question is
# offered within seconds instead of waiting for the next tick.
refill_now = asyncio.Event()


def _today_at(time_str, today=None):
    hh, mm = map(int, time_str.split(":"))
    base = today or datetime.now()
    return base.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def post_times(cfg):
    times = cfg["schedule"].get("post_times")
    if not times:
        raise ValueError("config.yaml is missing schedule.post_times")
    return times


def build_todays_slots(conn, cfg):
    """Fill every one of today's slots that isn't already spoken for."""
    answer_delay = timedelta(hours=cfg["schedule"]["answer_delay_hours"])
    now = datetime.now()
    filled = 0
    ran_out = False

    for post_time in post_times(cfg):
        scheduled_dt = _today_at(post_time, now)
        scheduled_iso = _iso(scheduled_dt)

        if db.queue_slot_filled(conn, "daily", scheduled_iso):
            continue

        question = db.claim_next_approved_question(conn)
        if question is None:
            ran_out = True
            break

        parent_id = db.enqueue_post(conn, "daily", question["lang"], [question["id"]], scheduled_iso)
        db.enqueue_post(
            conn, "story_a", question["lang"], [question["id"]],
            _iso(scheduled_dt + answer_delay), parent_id=parent_id,
        )
        filled += 1
        logger.ok(
            COMPONENT,
            f"question {question['public_id']} scheduled for {post_time} today "
            f"(answer story at {(scheduled_dt + answer_delay).strftime('%H:%M')})",
        )

    if ran_out:
        counts = db.count_questions_by_status(conn)
        waiting = counts.get("pending_review", 0)
        logger.warn(COMPONENT, "ran out of approved questions — some of today's slots are still empty")
        logger.detail(COMPONENT, f"{waiting:,} questions are waiting for you to review in Telegram")
        logger.detail(COMPONENT, "send /next to the review bot to approve more, and they'll fill in automatically")

    return filled


def release_due_posts(conn):
    """Hand slots that have come due to the review bot for approval."""
    now = datetime.now()
    today = now.date()
    released = 0

    for entry in db.get_due_queue_entries(conn, _iso(now), "daily"):
        scheduled_dt = datetime.fromisoformat(entry["scheduled_at"])

        if scheduled_dt.date() < today:
            db.set_queue_status(conn, entry["id"], "skipped", "left over from a previous day")
            db.cancel_child_entries(conn, entry["id"], "its post was never published")
            db.unqueue_questions(conn, json.loads(entry["question_ids"]), "approved")
            logger.warn(
                COMPONENT,
                f"a post scheduled for {scheduled_dt.strftime('%d %b at %H:%M')} was missed "
                "because the app was not running",
            )
            logger.detail(COMPONENT, "its question has gone back in the pool and will be scheduled again")
            continue

        db.set_queue_status(conn, entry["id"], "awaiting_approval")
        released += 1
        logger.info(
            COMPONENT,
            f"the {scheduled_dt.strftime('%H:%M')} post is due — sending it to Telegram for your approval",
        )

    return released


async def publish_due_answer_stories(conn, cfg):
    """Answer stories need no approval — approving the question approved these."""
    published = 0
    for entry in db.get_due_answer_stories(conn, _iso(datetime.now())):
        await publish.publish_answer_story(conn, cfg, entry)
        published += 1
    return published


async def run(conn, cfg, stop_event):
    """The scheduling loop. Runs alongside the scraper and the review bot."""
    logger.ok(COMPONENT, f"scheduler running — {len(post_times(cfg))} posts a day at {', '.join(post_times(cfg))}")

    while not stop_event.is_set():
        try:
            build_todays_slots(conn, cfg)
            release_due_posts(conn)
            await publish_due_answer_stories(conn, cfg)
        except Exception as exc:
            logger.error(
                COMPONENT,
                "something went wrong while working out today's schedule",
                exc,
                next_step="the app keeps running and will try again in a minute",
            )

        # Wake early when the review bot rejects a post, so its replacement is
        # offered straight away rather than up to a tick later.
        refill_now.clear()
        waiters = [asyncio.create_task(refill_now.wait()), asyncio.create_task(stop_event.wait())]
        done, pending = await asyncio.wait(waiters, timeout=TICK_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
