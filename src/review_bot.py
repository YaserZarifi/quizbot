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


def _looks_unreachable(exc):
    """Telegram's way of saying "this person has never opened a chat with me"."""
    text = str(exc).lower()
    return "chat not found" in text or "bot can't initiate" in text or "blocked" in text


def _report_delivery_problem(admin_id, exc, what):
    """Explain a failed Telegram delivery in terms the reader can act on."""
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


async def _close_cards(bot, cards, verdict, caption_mode=True):
    """Strip the buttons from every reviewer's copy and record the outcome.

    Best-effort by design: a copy that cannot be edited (reviewer deleted the
    chat, message too old) must not stop the others being cleared, and must not
    fail the decision that has already been recorded in the database.
    """
    for chat_id, message_id, original in cards:
        try:
            text = f"{original}\n\n{verdict}"
            if caption_mode:
                await bot.edit_message_caption(
                    chat_id=chat_id, message_id=message_id, caption=text, reply_markup=None
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text, reply_markup=None
                )
        except Exception:
            pass  # a stale copy is cosmetic; the decision itself already stands


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


async def _broadcast_next_question(bot, conn, cfg):
    """Show the next question to every reviewer at once.

    Returns False when nothing was sent — either a question is already open for
    decision, or there is nothing left to review.
    """
    if _review["qid"] is not None:
        return False  # one already open; deciding it releases the next

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


async def _decide_question(bot, conn, cfg, verdict):
    """Close the open question for everyone, then offer the next one.

    The open question is cleared before any Telegram call, so a second
    reviewer's tap arriving mid-edit sees nothing open and is turned away
    instead of decided twice.
    """
    cards = _review["cards"]
    _review["qid"] = None
    _review["cards"] = []
    await _close_cards(bot, cards, verdict)
    await _broadcast_next_question(bot, conn, cfg)


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
        sent = await _broadcast_next_question(context.bot, conn, cfg)
    if not sent:
        if _review["qid"] is not None:
            await update.message.reply_text("There is already a question waiting for a decision above.")
        else:
            await update.message.reply_text("No questions waiting for review right now.")


async def cmd_status(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    conn = context.bot_data["conn"]
    q = db.count_questions_by_status(conn)
    lines = [
        "📊 Where things stand",
        "",
        f"Waiting for your review : {q.get('pending_review', 0):,}",
        f"Approved, not yet used  : {q.get('approved', 0):,}",
        f"Scheduled to post       : {q.get('queued', 0):,}",
        f"Already posted          : {q.get('posted', 0):,}",
        f"Rejected                : {q.get('rejected', 0):,}",
        "",
        f"Posts per day: {len(scheduler.post_times(cfg))} "
        f"({', '.join(scheduler.post_times(cfg))})",
    ]
    await update.message.reply_text("\n".join(lines))


async def on_button(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    query = update.callback_query
    action, qid_str = query.data.split(":")
    qid = int(qid_str)
    conn = context.bot_data["conn"]
    who = update.effective_user.first_name or str(update.effective_user.id)

    async with _decision_lock:
        if _review["qid"] != qid:
            # Someone else already decided this one and the buttons on this copy
            # were a fraction too slow to disappear.
            await query.answer("Already decided by another reviewer", show_alert=False)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        await query.answer()

        if action == "approve":
            db.set_question_status(conn, qid, "approved")
            logger.ok(COMPONENT, f"{who} approved a question — it will be scheduled for posting")
            scheduler.refill_now.set()
            await _decide_question(context.bot, conn, cfg, f"✅ Approved by {who}")

        elif action == "skip":
            db.release_question(conn, qid)
            logger.info(COMPONENT, f"{who} skipped a question — it goes back in the queue")
            await _decide_question(context.bot, conn, cfg, f"⏭ Skipped by {who} (back in the queue)")

        elif action == "reject":
            # Recorded straight away. Waiting for a typed reason would hold up
            # every other reviewer, so the reason is optional and applied after.
            db.set_question_status(conn, qid, "rejected")
            _pending_reason["qid"] = qid
            _pending_reason["admin_id"] = update.effective_user.id
            logger.info(COMPONENT, f"{who} rejected a question")
            await _decide_question(
                context.bot, conn, cfg, qid,
                f"❌ Rejected by {who}\n(optional: reply with a reason)", who,
            )


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
    action, entry_id_str = query.data.split(":")
    entry_id = int(entry_id_str)
    conn = context.bot_data["conn"]
    who = update.effective_user.first_name or str(update.effective_user.id)

    async with _decision_lock:
        entry = db.get_queue_entry(conn, entry_id)
        if entry is None or entry["status"] != "awaiting_approval":
            await query.answer("Already handled by another reviewer", show_alert=False)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        await query.answer()

        cards = _post_cards.pop(entry_id, [])
        _sent_for_approval.discard(entry_id)
        question_ids = json.loads(entry["question_ids"])
        scheduled_hhmm = entry["scheduled_at"][11:16]

        if action == "post_approve":
            # Claim it before the slow Facebook calls so a second reviewer's tap
            # cannot start a duplicate publish while this one is in flight.
            db.set_queue_status(conn, entry_id, "publishing")
            await _close_cards(
                context.bot, cards, f"⏳ Approved by {who} — publishing...", caption_mode=False
            )
            logger.info(COMPONENT, f"{who} approved the {scheduled_hhmm} post")

            entry = dict(entry)
            published = await publish.publish_post(conn, cfg, entry)
            note = (
                "✅ Published. The answer story posts automatically in 6 hours."
                if published
                else "⚠️ Facebook would not accept it. The app will offer it again shortly."
            )
            await _close_cards(context.bot, cards, f"{'✅' if published else '⚠️'} {note}", caption_mode=False)

        elif action == "post_reject":
            db.set_queue_status(conn, entry_id, "rejected", "rejected by reviewer")
            db.cancel_child_entries(conn, entry_id, "its post was rejected")
            db.unqueue_questions(conn, question_ids, "rejected")
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
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(approve|reject|skip):"))
    app.add_handler(CallbackQueryHandler(on_post_button, pattern=r"^post_(approve|reject):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

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
