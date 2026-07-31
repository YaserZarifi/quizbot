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

# True once "slots are empty, nothing approved" has been reported, so that
# warning is not repeated on every tick while the situation is unchanged.
_ran_out_logged = False


def _today_at(time_str, today=None):
    hh, mm = map(int, time_str.split(":"))
    base = today or datetime.now()
    return base.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


MIN_POSTS_PER_DAY = 1
MAX_POSTS_PER_DAY = 12
POSTS_PER_DAY_SETTING = "posts_per_day"


def configured_times(cfg):
    times = cfg["schedule"].get("post_times")
    if not times:
        raise ValueError("config.yaml is missing schedule.post_times")
    return list(times)


def _to_minutes(hhmm):
    hh, mm = map(int, hhmm.split(":"))
    return hh * 60 + mm


def _to_hhmm(minutes):
    minutes = max(0, min(24 * 60 - 1, int(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def spread_times(first, last, count):
    """Spread `count` posts evenly between two times of day, rounded to 5 min.

    Chosen so the default of 6 between 08:00 and 23:00 reproduces exactly the
    times in config.yaml — changing the number of posts a day should not
    silently move the ones already there.
    """
    if count <= 1:
        return [first]
    start, end = _to_minutes(first), _to_minutes(last)
    step = (end - start) / (count - 1)
    times = []
    for i in range(count):
        rounded = round((start + i * step) / 5) * 5
        candidate = _to_hhmm(rounded)
        if candidate not in times:
            times.append(candidate)
    return times


def post_times(cfg, conn=None):
    """The times to post at today.

    config.yaml holds the default list. Choosing a different number of posts
    from Telegram stores an override in the database, and the times are then
    spread across the same window the configured list covers.
    """
    times = configured_times(cfg)
    if conn is None:
        return times

    override = db.get_int_setting(conn, POSTS_PER_DAY_SETTING)
    if not override or override == len(times):
        return times

    override = max(MIN_POSTS_PER_DAY, min(MAX_POSTS_PER_DAY, override))
    return spread_times(times[0], times[-1], override)


def unfilled_slot_count(conn, cfg):
    """How many of today's posting slots still have no question in them."""
    now = datetime.now()
    return sum(
        1 for post_time in post_times(cfg, conn)
        if not db.queue_slot_filled(conn, "daily", _iso(_today_at(post_time, now)))
    )


def approved_still_needed(conn, cfg):
    """How many more approved questions today's schedule needs. Zero means the
    day is covered and there is no reason to ask anyone to review more.

    Counting approved-but-not-yet-queued questions alongside empty slots is what
    makes this race-free: approving a question raises the approved count the
    instant the button is tapped, so review stops immediately rather than a beat
    later once the scheduler happens to run and consume it.

    Posting a question immediately does not fill a slot and does not come from
    the approved pool, so it leaves this number untouched — review keeps going
    until a slot is genuinely filled.
    """
    approved = db.count_questions_by_status(conn).get("approved", 0)
    return unfilled_slot_count(conn, cfg) - approved


def build_todays_slots(conn, cfg):
    """Fill every one of today's slots that isn't already spoken for."""
    answer_delay = timedelta(hours=cfg["schedule"]["answer_delay_hours"])
    now = datetime.now()
    filled = 0
    ran_out = False

    for post_time in post_times(cfg, conn):
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

    global _ran_out_logged
    if ran_out:
        # Said once per dry spell, not once a minute. Repeating an unchanged
        # warning every tick buries the lines that report something new.
        if not _ran_out_logged:
            _ran_out_logged = True
            counts = db.count_questions_by_status(conn)
            waiting = counts.get("pending_review", 0)
            empty = unfilled_slot_count(conn, cfg)
            logger.warn(COMPONENT, f"{empty} of today's posting slots are still empty")
            logger.detail(COMPONENT, f"{waiting:,} questions are waiting to be reviewed in Telegram")
            logger.detail(COMPONENT, "approve some and they will fill these slots automatically")
    else:
        _ran_out_logged = False

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
    times = post_times(cfg, conn)
    logger.ok(COMPONENT, f"scheduler running — {len(times)} posts a day at {', '.join(times)}")

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
