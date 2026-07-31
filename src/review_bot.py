"""The Telegram side of the app. Two jobs share one bot token (a token can
only have one poller, so both live here):

1. Question review — the same question is shown to every reviewer at once, with
   Approve / Reject / Skip buttons. The first reviewer to act decides it, and
   the buttons disappear from everyone's copy immediately so two people cannot
   make conflicting decisions about one question.

2. Post approval — the scheduler marks a due post 'awaiting_approval'; this
   module renders the real images that would go out, shows them with the exact
   caption, and calls publish.py only once a reviewer taps Publish. Nothing
   reaches Facebook without that tap. Same rule: the first tap wins and the
   buttons clear everywhere.

Rejecting a post retires that question and frees its time slot, which the
scheduler refills from the next approved question within seconds — so a day
always works its way to a full set of posts.

This module no longer owns the process: main.py runs it as one task beside the
scraper and the scheduler, all on a single event loop.
"""

import asyncio
import json
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import db
import logger
import publish
import render
import scheduler

COMPONENT = "review"
PUSH_INTERVAL_SECONDS = 20

# Exactly one question is under review at a time, shown to every reviewer.
# "cards" holds one entry per reviewer so every copy can be updated together.
#   {"qid": int, "cards": [(chat_id, message_id, original_caption), ...]}
_review = {"qid": None, "cards": []}

# Who tapped Reject last and for which question, so a reason typed afterwards
# lands on the right row. Rejection itself is not held up waiting for it.
_pending_reason = {"qid": None, "admin_id": None}

# Serialises button presses. Two reviewers tapping at the same moment would
# otherwise both pass the "is this still open?" check and act twice.
_decision_lock = asyncio.Lock()

# post_queue id -> the message shown to each reviewer, same purpose as "cards".
_post_cards = {}
_sent_for_approval = set()

# Reviewers already warned about an unreachable chat, so the same advice isn't
# repeated every 20 seconds forever.
_warned_unreachable = set()

# True once "the day is covered, pausing review" has been said, so the pause is
# announced once rather than every time the push loop comes round.
_quiet_logged = False


def _looks_unreachable(exc):
    """Telegram's way of saying "this person has never opened a chat with me"."""
    text = str(exc).lower()
    return "chat not found" in text or "bot can't initiate" in text or "blocked" in text


def _looks_transient(exc):
    """A slow or flaky connection, not something anyone needs to fix."""
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text or "network" in text or "connection" in text


def _report_delivery_problem(admin_id, exc, what):
    """Explain a failed Telegram delivery in terms the reader can act on."""
    if _looks_transient(exc):
        # Uploading a card on a slow connection times out fairly often. It
        # retries by itself within seconds, so reporting it as an error would
        # train the reader to ignore the lines that do matter.
        logger.warn(COMPONENT, f"the internet was slow sending {what} — retrying shortly")
        return

    if _looks_unreachable(exc):
        if admin_id in _warned_unreachable:
            return
        _warned_unreachable.add(admin_id)
        logger.warn(COMPONENT, f"cannot message reviewer {admin_id} on Telegram yet")
        logger.detail(COMPONENT, "a Telegram bot may only write to people who have started a chat with it first")
        logger.detail(COMPONENT, "fix: open Telegram, search for the bot, open it and press Start (or send /start)")
        logger.detail(COMPONENT, "everything else keeps running; questions will arrive as soon as that is done")
        return

    logger.error(
        COMPONENT,
        f"could not send {what} to reviewer {admin_id} on Telegram",
        exc,
        next_step="the app will try again in a few seconds",
    )


def _is_admin(update, admin_user_ids):
    user = update.effective_user
    if user is None or user.id not in admin_user_ids:
        who = user.id if user else "unknown"
        logger.warn(COMPONENT, f"ignored a message from Telegram user {who} — they are not on the reviewer list")
        return False
    return True


# Telegram's hard limits. Exceeding either makes the edit fail outright, which
# used to leave live buttons on a question that had already been decided.
_CAPTION_LIMIT = 1024
_TEXT_LIMIT = 4096


def _fit(original, verdict, limit):
    """Keep original + verdict inside Telegram's limit, trimming the original."""
    tail = f"\n\n{verdict}"
    room = limit - len(tail)
    if room < 0:
        return verdict[:limit]
    if len(original) <= room:
        return original + tail
    return original[: max(0, room - 1)] + "…" + tail


async def _close_cards(bot, cards, verdict, caption_mode=True):
    """Strip the buttons from every reviewer's copy and record the outcome.

    Removing the keyboard is done as its own call, before the text edit. The two
    used to be one call, so whenever the combined text broke a Telegram limit
    the whole edit failed and the buttons survived on a question that was
    already decided — letting it be decided again and again.

    Best-effort per copy: one unreachable chat must not stop the others being
    cleared, and must never fail a decision already recorded in the database.
    """
    for chat_id, message_id, original in cards:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        except Exception:
            pass

        try:
            limit = _CAPTION_LIMIT if caption_mode else _TEXT_LIMIT
            text = _fit(original, verdict, limit)
            if caption_mode:
                await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text)
            else:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
        except Exception:
            pass  # the outcome text is cosmetic; the buttons are already gone


# ---- question review ------------------------------------------------------

def _caption_for(question):
    lines = []
    if question["lexicon_flag"]:
        lines.append("⚠️ Possible Iranian-Farsi wording — double check")
    lines.append(f"{question['public_id']}  ·  {question['lang']}")
    lines.append("")
    lines.append(question["question_text"] or "(image question)")
    return "\n".join(lines)


def _keyboard(qid):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{qid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{qid}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"skip:{qid}"),
        ],
        # Second row on its own: this one goes straight to Facebook with no
        # further confirmation, so it should not sit a thumb's width from Skip.
        [InlineKeyboardButton("🚀 Post now (straight to Facebook)", callback_data=f"postnow:{qid}")],
    ])


async def _broadcast_next_question(bot, conn, cfg, forced=False):
    """Show the next question to every reviewer at once.

    Returns False when nothing was sent, which happens for three reasons:
      * a question is already open — nothing new goes out until it is decided,
        so an undecided question is never re-sent and cannot flood the chat
      * today's posts are all covered, so there is nothing to review for
      * there is nothing left in the pool

    forced=True is /next: ask anyway even though the day is already covered.
    """
    global _quiet_logged

    if _review["qid"] is not None:
        return False  # one already open; deciding it releases the next

    if not forced and scheduler.approved_still_needed(conn, cfg) <= 0:
        if not _quiet_logged:
            _quiet_logged = True
            logger.ok(COMPONENT, "today's posts are all covered — pausing question review")
            logger.detail(COMPONENT, "no more questions will be sent until a slot opens up or a new day starts")
            logger.detail(COMPONENT, "send /next in Telegram if you want to review more anyway")
        return False

    question = db.claim_next_pending_review(conn, 0)
    if question is None:
        return False

    themes, strings = publish.themes_and_strings(cfg)
    try:
        png_path = await render.render_feed_card(question, publish.theme_for(question, themes), strings, cfg)
    except Exception as exc:
        logger.error(
            COMPONENT,
            f"could not make a preview image for question {question['public_id']}",
            exc,
            next_step="that question went back in the pool; moving on to the next one",
        )
        db.release_question(conn, question["id"])
        return False

    with open(png_path, "rb") as f:
        photo_bytes = f.read()

    caption = _caption_for(question)
    cards = []
    for admin_id in cfg["telegram"]["admin_user_ids"]:
        try:
            message = await bot.send_photo(
                chat_id=admin_id,
                photo=photo_bytes,
                caption=caption,
                reply_markup=_keyboard(question["id"]),
            )
            cards.append((admin_id, message.message_id, caption))
        except Exception as exc:
            _report_delivery_problem(admin_id, exc, "the next question")

    if not cards:
        # Nobody could be reached — put it back rather than stranding it as
        # in_review, which nothing would ever pick up again.
        db.release_question(conn, question["id"])
        return False

    _review["qid"] = question["id"]
    _review["cards"] = cards
    _quiet_logged = False
    return True


async def _post_immediately(conn, cfg, qid, who):
    """Publish one question to Facebook right now, outside the daily schedule.

    Deliberately its own queue kind ('instant'): a scheduled slot is claimed by
    an exact scheduled_at match, so reusing 'daily' here would let a post made
    at the top of a slot's minute swallow that slot and cost the day a post.
    The answer story is created the same way as any other, so it still follows
    six hours later and still only fires if this post actually succeeded.

    Returns the line to show on the reviewers' cards.
    """
    questions = db.get_questions_by_ids(conn, [qid])
    if not questions:
        return "⚠️ Could not post — the question is no longer in the database"
    question = questions[0]

    now = datetime.now()
    answer_at = now + timedelta(hours=cfg["schedule"]["answer_delay_hours"])

    db.set_question_status(conn, qid, "queued")
    entry_id = db.enqueue_post(
        conn, "instant", question["lang"], [qid], now.isoformat(timespec="seconds")
    )
    db.enqueue_post(
        conn, "story_a", question["lang"], [qid],
        answer_at.isoformat(timespec="seconds"), parent_id=entry_id,
    )
    # Claimed before the slow Facebook calls, matching the scheduled path, so a
    # restart mid-upload is recognisable as interrupted rather than pending.
    db.set_queue_status(conn, entry_id, "publishing")

    logger.info(COMPONENT, f"{who} chose to post question {question['public_id']} immediately")

    entry = db.get_queue_entry(conn, entry_id)
    published = await publish.publish_post(conn, cfg, entry)

    if published:
        return (
            f"🚀 Posted to Facebook now by {who}\n"
            f"Answer story follows at {answer_at.strftime('%H:%M')}"
        )

    # publish_post leaves a failure as 'pending' so the scheduler can retry it,
    # but nothing ever retries an instant post. Close it out and put the
    # question back in the approved pool so it still gets its turn normally.
    db.set_queue_status(conn, entry_id, "failed", "immediate post failed")
    db.cancel_child_entries(conn, entry_id, "its post never published")
    db.unqueue_questions(conn, [qid], "approved")
    logger.warn(COMPONENT, f"the immediate post of question {question['public_id']} did not go through")
    logger.detail(COMPONENT, "the question is approved and back in the pool for a normal scheduled slot")
    return f"⚠️ Facebook would not accept it. Approved instead — it will go out on the schedule."


def _clear_stale_review(conn):
    """Drop the open question if the database says it is no longer open.

    Which question is in front of the reviewers is tracked in memory, and the
    database is the truth. If a button handler ever fails partway through — it
    has happened — the two disagree: the question is decided in the database but
    memory still thinks it is open, so no new question is ever sent and every
    further tap is turned away as "already decided". This notices that and
    releases the jam instead of needing the app restarted.
    """
    qid = _review["qid"]
    if qid is None:
        return False
    row = conn.execute("SELECT status FROM questions WHERE id = ?", (qid,)).fetchone()
    if row is not None and row["status"] == "in_review":
        return False  # genuinely still open, waiting on a reviewer

    logger.warn(COMPONENT, "the question on screen had already been decided — moving on to the next one")
    _review["qid"] = None
    _review["cards"] = []
    return True


async def cmd_start(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    await update.message.reply_text(
        "Kankor bot is running.\n\n"
        "Questions arrive here automatically for you to approve or reject.\n"
        "Every reviewer sees the same question — whoever answers first decides it.\n\n"
        "/next — show the next question now\n"
        "/status — how many questions are waiting, approved and posted"
    )


async def cmd_next(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    conn = context.bot_data["conn"]
    async with _decision_lock:
        # forced: /next means "give me one anyway", even when the day is covered.
        sent = await _broadcast_next_question(context.bot, conn, cfg, forced=True)
    if not sent:
        if _review["qid"] is not None:
            await update.message.reply_text("There is already a question waiting for a decision above.")
        else:
            await update.message.reply_text("No questions left to review.")


async def cmd_status(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    conn = context.bot_data["conn"]
    q = db.count_questions_by_status(conn)
    times = scheduler.post_times(cfg)
    empty = scheduler.unfilled_slot_count(conn, cfg)
    needed = scheduler.approved_still_needed(conn, cfg)

    lines = [
        "📊 Where things stand",
        "",
        f"Waiting for your review : {q.get('pending_review', 0):,}",
        f"Approved, not yet used  : {q.get('approved', 0):,}",
        f"Scheduled to post       : {q.get('queued', 0):,}",
        f"Already posted          : {q.get('posted', 0):,}",
        f"Rejected                : {q.get('rejected', 0):,}",
        "",
        f"Today: {len(times) - empty} of {len(times)} posts filled",
        f"Times: {', '.join(times)}",
        "",
        (
            "✅ Today is fully covered — no more questions will be sent.\nUse /next if you want to review more anyway."
            if needed <= 0
            else f"Still need {needed} more approved question(s) to fill today."
        ),
    ]
    await update.message.reply_text("\n".join(lines))


async def on_button(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    query = update.callback_query

    # Acknowledge the tap before anything else. Telegram allows about fifteen
    # seconds to answer a callback and then shows the button as failed, so this
    # must never sit behind a lock that a slow Facebook upload might be holding.
    try:
        await query.answer()
    except Exception:
        pass

    action, qid_str = query.data.split(":")
    qid = int(qid_str)
    conn = context.bot_data["conn"]
    who = update.effective_user.first_name or str(update.effective_user.id)

    # Clear the buttons on the tapped copy immediately, so it cannot be pressed
    # a second time while the decision is being carried out.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    # The lock covers only the quick database work that settles who won the
    # race. Telegram edits and Facebook uploads run outside it — holding it
    # across those starved the loop that hands out the next question.
    async with _decision_lock:
        if _review["qid"] != qid:
            return  # someone else already decided this one

        cards = _review["cards"]
        _review["qid"] = None
        _review["cards"] = []

        if action == "approve":
            db.set_question_status(conn, qid, "approved")
            verdict = f"✅ Approved by {who}"
            logger.ok(COMPONENT, f"{who} approved a question — it will be scheduled for posting")
            scheduler.refill_now.set()

        elif action == "skip":
            db.release_question(conn, qid)
            verdict = f"⏭ Skipped by {who} (back in the queue)"
            logger.info(COMPONENT, f"{who} skipped a question — it goes back in the queue")

        elif action == "reject":
            # Recorded straight away. Waiting for a typed reason would hold up
            # every other reviewer, so the reason is optional and applied after.
            db.set_question_status(conn, qid, "rejected")
            _pending_reason["qid"] = qid
            _pending_reason["admin_id"] = update.effective_user.id
            verdict = f"❌ Rejected by {who}\n(optional: reply with a reason)"
            logger.info(COMPONENT, f"{who} rejected a question")

        elif action == "postnow":
            verdict = f"🚀 Posting now, requested by {who}..."

        else:
            return

    await _close_cards(context.bot, cards, verdict)

    if action == "postnow":
        result = await _post_immediately(conn, cfg, qid, who)
        await _close_cards(context.bot, cards, result)

    async with _decision_lock:
        await _broadcast_next_question(context.bot, conn, cfg)


async def cmd_noreason(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    _pending_reason["qid"] = None
    _pending_reason["admin_id"] = None


async def on_text(update, context):
    """A plain message is treated as the reason for the rejection just made."""
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    qid = _pending_reason["qid"]
    if qid is None or _pending_reason["admin_id"] != update.effective_user.id:
        return
    conn = context.bot_data["conn"]
    db.set_question_status(conn, qid, "rejected", reject_reason=update.message.text.strip())
    _pending_reason["qid"] = None
    _pending_reason["admin_id"] = None
    await update.message.reply_text("Reason saved.")


# ---- post approval --------------------------------------------------------

def _post_keyboard(entry_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Publish", callback_data=f"post_approve:{entry_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"post_reject:{entry_id}"),
    ]])


async def _send_post_for_approval(bot, cfg, conn, entry):
    question_ids = json.loads(entry["question_ids"])
    questions = db.get_questions_by_ids(conn, question_ids)
    if not questions:
        db.set_queue_status(conn, entry["id"], "failed", "question missing from database")
        db.cancel_child_entries(conn, entry["id"], "its post was never published")
        logger.error(
            COMPONENT,
            "a scheduled post was dropped — its question is no longer in the database",
            next_step="the slot will be refilled with another question shortly",
        )
        scheduler.refill_now.set()
        return

    question = questions[0]
    themes, strings = publish.themes_and_strings(cfg)
    theme = publish.theme_for(question, themes)

    try:
        feed_png = await render.render_feed_card(question, theme, strings, cfg)
        story_png = await render.render_story_question(question, theme, strings, cfg)
    except Exception as exc:
        logger.error(
            COMPONENT,
            f"could not make the images for question {question['public_id']}",
            exc,
            next_step="the app will try again in a minute",
        )
        return

    scheduled_hhmm = entry["scheduled_at"][11:16]
    prompt = "\n".join([
        f"🕐 The {scheduled_hhmm} post — question {question['public_id']}",
        "",
        "Top image = Facebook post, bottom image = story.",
        "The answer story posts automatically 6 hours later.",
        "",
        "Caption that will be posted:",
        publish.build_feed_caption(entry["lang"], strings, cfg),
    ])

    images = []
    for path in (feed_png, story_png):
        with open(path, "rb") as f:
            images.append(f.read())

    cards = []
    for admin_id in cfg["telegram"]["admin_user_ids"]:
        try:
            await bot.send_media_group(chat_id=admin_id, media=[InputMediaPhoto(img) for img in images])
            message = await bot.send_message(
                chat_id=admin_id, text=prompt, reply_markup=_post_keyboard(entry["id"])
            )
            cards.append((admin_id, message.message_id, prompt))
        except Exception as exc:
            _report_delivery_problem(admin_id, exc, f"the {scheduled_hhmm} post")

    if cards:
        _post_cards[entry["id"]] = cards
        _sent_for_approval.add(entry["id"])
        logger.ok(
            COMPONENT,
            f"the {scheduled_hhmm} post (question {question['public_id']}) is waiting for approval in Telegram",
        )


async def on_post_button(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    query = update.callback_query

    # Answered first, and the tapped copy's buttons cleared first — same reason
    # as the question buttons: publishing takes far longer than Telegram is
    # willing to wait for a callback to be acknowledged.
    try:
        await query.answer()
    except Exception:
        pass

    action, entry_id_str = query.data.split(":")
    entry_id = int(entry_id_str)
    conn = context.bot_data["conn"]
    who = update.effective_user.first_name or str(update.effective_user.id)

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Lock held only long enough to claim the entry, so the loser of a race is
    # turned away before either upload starts.
    async with _decision_lock:
        entry = db.get_queue_entry(conn, entry_id)
        if entry is None or entry["status"] != "awaiting_approval":
            return  # already handled by whoever got here first

        cards = _post_cards.pop(entry_id, [])
        _sent_for_approval.discard(entry_id)
        question_ids = json.loads(entry["question_ids"])
        scheduled_hhmm = entry["scheduled_at"][11:16]
        entry = dict(entry)

        if action == "post_approve":
            # Claimed before the slow Facebook calls so a second tap cannot
            # start a duplicate publish while this one is in flight.
            db.set_queue_status(conn, entry_id, "publishing")
        elif action == "post_reject":
            db.set_queue_status(conn, entry_id, "rejected", "rejected by reviewer")
            db.cancel_child_entries(conn, entry_id, "its post was rejected")
            db.unqueue_questions(conn, question_ids, "rejected")
        else:
            return

    if action == "post_approve":
        await _close_cards(context.bot, cards, f"⏳ Approved by {who} — publishing...", caption_mode=False)
        logger.info(COMPONENT, f"{who} approved the {scheduled_hhmm} post")

        published = await publish.publish_post(conn, cfg, entry)
        note = (
            "✅ Published. The answer story posts automatically in 6 hours."
            if published
            else "⚠️ Facebook would not accept it. The app will offer it again shortly."
        )
        await _close_cards(context.bot, cards, note, caption_mode=False)

    else:
        await _close_cards(
            context.bot, cards,
            f"❌ Rejected by {who} — finding a replacement question...", caption_mode=False,
        )
        logger.info(COMPONENT, f"{who} rejected the {scheduled_hhmm} post")
        logger.detail(COMPONENT, "picking another approved question so the day still gets its full posts")
        scheduler.refill_now.set()


# ---- background push loops ------------------------------------------------

async def _push_questions_loop(app, conn, cfg, stop_event):
    """Keep a question in front of the reviewers whenever none is open."""
    while not stop_event.is_set():
        try:
            async with _decision_lock:
                _clear_stale_review(conn)
                await _broadcast_next_question(app.bot, conn, cfg)
        except Exception as exc:
            logger.error(
                COMPONENT,
                "could not send the next question to Telegram",
                exc,
                next_step="the app will try again in a few seconds",
            )
        await _sleep_or_stop(stop_event, PUSH_INTERVAL_SECONDS)


async def _push_posts_loop(app, conn, cfg, stop_event):
    """Send due posts out for approval."""
    while not stop_event.is_set():
        try:
            for entry in db.get_awaiting_approval_entries(conn):
                if entry["id"] in _sent_for_approval:
                    continue
                await _send_post_for_approval(app.bot, cfg, conn, entry)
        except Exception as exc:
            logger.error(
                COMPONENT,
                "could not send a post for approval to Telegram",
                exc,
                next_step="the app will try again in a few seconds",
            )
        await _sleep_or_stop(stop_event, PUSH_INTERVAL_SECONDS)


async def on_telegram_error(update, context):
    """Report anything that went wrong handling a Telegram update.

    python-telegram-bot catches handler exceptions itself, so without this they
    never reach the console and a broken button looks like a broken bot with no
    explanation anywhere.
    """
    exc = context.error
    if _looks_transient(exc):
        logger.warn(COMPONENT, "the internet was slow talking to Telegram — retrying shortly")
        return
    logger.error(
        COMPONENT,
        "something went wrong handling a Telegram button or message",
        exc,
        next_step="the app keeps running and will offer the next question shortly",
    )


async def _sleep_or_stop(stop_event, seconds):
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run(conn, cfg, stop_event):
    """Start the bot and keep it polling until stop_event is set.

    Deliberately not Application.run_polling(): that takes over the event loop
    and installs its own signal handlers, which would leave no room for the
    scraper and scheduler to run in this same process.
    """
    token = cfg["telegram"]["review_bot_token"]

    app = Application.builder().token(token).build()
    app.bot_data["cfg"] = cfg
    app.bot_data["conn"] = conn

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("noreason", cmd_noreason))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(approve|reject|skip|postnow):"))
    app.add_handler(CallbackQueryHandler(on_post_button, pattern=r"^post_(approve|reject):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    # Without this, a crash inside a button handler is swallowed by the library
    # and the app looks healthy while the buttons quietly stop working.
    app.add_error_handler(on_telegram_error)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    me = await app.bot.get_me()
    logger.ok(COMPONENT, f"review bot connected as @{me.username}")
    logger.detail(COMPONENT, f"reviewers: {', '.join(str(i) for i in cfg['telegram']['admin_user_ids'])}")
    logger.detail(COMPONENT, "every reviewer sees the same question — whoever answers first decides it")

    pushers = [
        asyncio.create_task(_push_questions_loop(app, conn, cfg, stop_event)),
        asyncio.create_task(_push_posts_loop(app, conn, cfg, stop_event)),
    ]
    try:
        await stop_event.wait()
    finally:
        for task in pushers:
            task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info(COMPONENT, "review bot stopped")
