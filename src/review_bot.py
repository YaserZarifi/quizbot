"""Telegram approval bot. Two independent jobs run in this one process
(a single bot token can only have one poller, so both live here):

1. Question review — sends new questions one at a time with Approve/Reject/
   Skip buttons (spec §8.2). Any user id in config.telegram.admin_user_ids is
   an equal reviewer; everyone else is ignored and logged. /next atomically
   claims a row (status -> in_review, see db.py's status-flow note) so a
   second admin's /next never gets the same question.

2. Post approval — main.py flips due post_queue rows to 'awaiting_approval';
   this bot renders the actual images that would be posted, shows them (plus
   the caption/hashtags for feed posts) to every admin with Publish/Reject
   buttons, and only calls publish.py — the real Facebook API call — once an
   admin taps Publish. Nothing reaches Facebook without that tap.

Long-running polling bot — no webhooks.
"""

import asyncio
import json
import os
import sys

import yaml
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import logger
import publish
import render

COMPONENT = "review"
POLL_INTERVAL_SECONDS = 20

KIND_LABELS = {"feed": "Feed post", "story_q": "Question story", "story_a": "Answer-reveal story"}
PUBLISH_HANDLERS = {
    "feed": publish.publish_feed,
    "story_q": publish.publish_story_question,
    "story_a": publish.publish_story_answer,
}

# Per-admin state, keyed by Telegram user id. In-memory only — resets on
# restart, which is fine: a claimed-but-undecided row just stays in_review
# and reappears in /next once the admin acts on it again.
_admin_state = {}

# post_queue entry ids already sent for approval this run, so the poll job
# doesn't resend the same post every tick while it's awaiting a decision.
_sent_for_approval = set()


def _state_for(admin_id):
    return _admin_state.setdefault(admin_id, {"current_qid": None, "awaiting_reason_qid": None})


def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _is_admin(update, admin_user_ids):
    user = update.effective_user
    if user is None or user.id not in admin_user_ids:
        who = user.id if user else "unknown"
        logger.warn(COMPONENT, f"ignored message from unauthorized user {who}")
        return False
    return True


def _caption_for(question):
    lines = []
    if question["lexicon_flag"]:
        lines.append("⚠️ Possible Iranian-Farsi wording — double check")
    lines.append(f"{question['public_id']}  ·  {question['subject']}  ·  {question['lang']}")
    lines.append("")
    lines.append(question["question_text"] or "(image question)")
    return "\n".join(lines)


def _keyboard(qid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{qid}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject:{qid}"),
        InlineKeyboardButton("⏭ Skip", callback_data=f"skip:{qid}"),
    ]])


async def _send_next(context, chat_id, conn, cfg, admin_id):
    question = db.claim_next_pending_review(conn, admin_id)
    if question is None:
        _state_for(admin_id)["current_qid"] = None
        return False

    themes = render.load_themes(cfg["paths"]["themes_file"])
    strings = render.load_strings(cfg["paths"]["strings_file"])
    theme = themes.get(question["subject"], themes["عمومی"])

    try:
        png_path = await render.render_feed_card(question, theme, strings, cfg)
    except Exception as exc:
        logger.error(COMPONENT, f"could not render preview for {question['public_id']} — see error.log", exc)
        db.release_question(conn, question["id"])
        return False

    _state_for(admin_id)["current_qid"] = question["id"]
    with open(png_path, "rb") as f:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=f,
            caption=_caption_for(question),
            reply_markup=_keyboard(question["id"]),
        )
    return True


async def cmd_start(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    await update.message.reply_text("Kankor review bot is running. Use /next to see the next question.")


async def cmd_next(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    conn = context.bot_data["conn"]
    admin_id = update.effective_user.id
    sent = await _send_next(context, update.effective_chat.id, conn, cfg, admin_id)
    if not sent:
        await update.message.reply_text("No questions waiting for review right now.")


async def on_button(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    query = update.callback_query
    await query.answer()
    action, qid_str = query.data.split(":")
    qid = int(qid_str)
    conn = context.bot_data["conn"]
    admin_id = update.effective_user.id
    state = _state_for(admin_id)

    if action == "approve":
        db.set_question_status(conn, qid, "approved")
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ Approved")
        state["current_qid"] = None
        await _send_next(context, update.effective_chat.id, conn, cfg, admin_id)

    elif action == "skip":
        db.release_question(conn, qid)
        state["current_qid"] = None
        await query.edit_message_caption(caption=query.message.caption + "\n\n⏭ Skipped (back in the queue)")
        await _send_next(context, update.effective_chat.id, conn, cfg, admin_id)

    elif action == "reject":
        state["awaiting_reason_qid"] = qid
        await query.edit_message_caption(
            caption=query.message.caption + "\n\n❌ Rejected — reply with a short reason, or send /noreason"
        )


async def cmd_noreason(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    admin_id = update.effective_user.id
    state = _state_for(admin_id)
    qid = state["awaiting_reason_qid"]
    if qid is None:
        return
    conn = context.bot_data["conn"]
    db.set_question_status(conn, qid, "rejected")
    state["awaiting_reason_qid"] = None
    state["current_qid"] = None
    await _send_next(context, update.effective_chat.id, conn, cfg, admin_id)


async def on_text(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    admin_id = update.effective_user.id
    state = _state_for(admin_id)
    qid = state["awaiting_reason_qid"]
    if qid is None:
        return
    conn = context.bot_data["conn"]
    db.set_question_status(conn, qid, "rejected", reject_reason=update.message.text.strip())
    state["awaiting_reason_qid"] = None
    state["current_qid"] = None
    await update.message.reply_text("Reason saved.")
    await _send_next(context, update.effective_chat.id, conn, cfg, admin_id)


async def poll_job(context):
    cfg = context.bot_data["cfg"]
    conn = context.bot_data["conn"]
    for admin_id in cfg["telegram"]["admin_user_ids"]:
        state = _state_for(admin_id)
        if state["current_qid"] is not None or state["awaiting_reason_qid"] is not None:
            continue  # this admin already has a card pending their decision
        await _send_next(context, admin_id, conn, cfg, admin_id)


def _post_keyboard(entry_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Publish", callback_data=f"post_approve:{entry_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"post_reject:{entry_id}"),
    ]])


async def _send_post_for_approval(context, cfg, conn, entry):
    question_ids = json.loads(entry["question_ids"])
    questions = db.get_questions_by_ids(conn, question_ids)
    if not questions:
        db.set_queue_status(conn, entry["id"], "failed", "no matching questions in database")
        logger.error(COMPONENT, f"post #{entry['id']} ({entry['kind']}) skipped — no matching questions in database")
        return

    themes = render.load_themes(cfg["paths"]["themes_file"])
    strings = render.load_strings(cfg["paths"]["strings_file"])

    try:
        png_paths = []
        for q in questions:
            theme = themes.get(q["subject"], themes["عمومی"])
            if entry["kind"] == "feed":
                png_paths.append(await render.render_feed_card(q, theme, strings, cfg))
            elif entry["kind"] == "story_q":
                png_paths.append(await render.render_story_question(q, theme, strings, cfg))
            else:
                png_paths.append(await render.render_story_answer(q, theme, strings, cfg))
    except Exception as exc:
        logger.error(COMPONENT, f"could not render preview for post #{entry['id']} — see error.log", exc)
        return

    prompt_lines = [f"{KIND_LABELS[entry['kind']]} — {entry['lang']} — {len(questions)} questions"]
    if entry["kind"] == "feed":
        prompt_lines += ["", "Caption that will be posted:", publish.build_feed_caption(entry["lang"], strings, cfg)]
    prompt_text = "\n".join(prompt_lines)

    photos = []
    for p in png_paths[:10]:
        with open(p, "rb") as f:
            photos.append(f.read())

    for admin_id in cfg["telegram"]["admin_user_ids"]:
        try:
            if len(photos) > 1:
                await context.bot.send_media_group(chat_id=admin_id, media=[InputMediaPhoto(p) for p in photos])
                await context.bot.send_message(
                    chat_id=admin_id, text=prompt_text, reply_markup=_post_keyboard(entry["id"])
                )
            else:
                await context.bot.send_photo(
                    chat_id=admin_id, photo=photos[0], caption=prompt_text, reply_markup=_post_keyboard(entry["id"])
                )
        except Exception as exc:
            logger.error(COMPONENT, f"could not send post #{entry['id']} preview to admin {admin_id} — see error.log", exc)

    _sent_for_approval.add(entry["id"])


async def poll_post_approvals(context):
    cfg = context.bot_data["cfg"]
    conn = context.bot_data["conn"]
    for entry in db.get_awaiting_approval_entries(conn):
        if entry["id"] in _sent_for_approval:
            continue
        await _send_post_for_approval(context, cfg, conn, entry)


async def on_post_button(update, context):
    cfg = context.bot_data["cfg"]
    if not _is_admin(update, cfg["telegram"]["admin_user_ids"]):
        return
    query = update.callback_query
    await query.answer()
    action, entry_id_str = query.data.split(":")
    entry_id = int(entry_id_str)
    conn = context.bot_data["conn"]

    entry = db.get_queue_entry(conn, entry_id)
    if entry is None or entry["status"] != "awaiting_approval":
        await query.edit_message_text(query.message.text + "\n\n(already handled)")
        return

    who = update.effective_user.first_name or str(update.effective_user.id)

    if action == "post_approve":
        await query.edit_message_text(query.message.text + f"\n\n⏳ Approved by {who} — publishing...")
        handler = PUBLISH_HANDLERS[entry["kind"]]
        published = await handler(conn, cfg, entry)
        note = "✅ Published to Facebook" if published else "⚠️ Facebook rejected it — will retry automatically"
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=f"{KIND_LABELS[entry['kind']]} ({entry['lang']}): {note}"
        )
        _sent_for_approval.discard(entry_id)

    elif action == "post_reject":
        db.set_queue_status(conn, entry_id, "rejected", "rejected by admin")
        await query.edit_message_text(query.message.text + f"\n\n❌ Rejected by {who} — will not be posted")
        _sent_for_approval.discard(entry_id)


def main():
    cfg = load_config()
    os.environ.setdefault("KANKOR_LOG_DIR", cfg["paths"].get("log_dir", "logs"))
    conn = db.connect(cfg["paths"]["db_file"])

    token = cfg["telegram"]["review_bot_token"]
    if not token:
        logger.error(COMPONENT, "telegram.review_bot_token is not set in config.yaml")
        return
    if not cfg["telegram"].get("admin_user_ids"):
        logger.error(COMPONENT, "telegram.admin_user_ids is empty in config.yaml")
        return

    asyncio.run(render.ensure_chromium_installed(COMPONENT))

    app = Application.builder().token(token).build()
    app.bot_data["cfg"] = cfg
    app.bot_data["conn"] = conn

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("noreason", cmd_noreason))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(approve|reject|skip):"))
    app.add_handler(CallbackQueryHandler(on_post_button, pattern=r"^post_(approve|reject):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL_SECONDS, first=5)
    app.job_queue.run_repeating(poll_post_approvals, interval=POLL_INTERVAL_SECONDS, first=8)

    logger.ok(COMPONENT, "review bot started — waiting for questions to review and posts to approve")
    app.run_polling()


if __name__ == "__main__":
    main()
