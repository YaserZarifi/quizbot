"""Telegram scraper (Telethon userbot). Pulls quiz polls from the configured
channels into the local database — see kankor_quiz_bot_spec.md §8.1.

Runs continuously as one of main.py's background tasks. The first pass over a
channel walks its entire history, which for a large channel is hours of work;
it checkpoints every 100 messages so switching the app off and on again resumes
where it stopped rather than starting over. Once a channel's history is fully
read, later passes only fetch messages newer than the last one seen.
"""

import asyncio
import logging as _logging
import os
import random
import re

from telethon import TelegramClient
from telethon.tl.functions.messages import SendVoteRequest
from telethon.tl.types import MessageMediaPhoto, MessageMediaPoll

import db
import logger
import subject_tagger
import text_clean

COMPONENT = "ingest"
SET_POSITION_RE = re.compile(r"\[?(\d{1,3})\s*/\s*(\d{1,3})\]?")
BACKFILL_CHECKPOINT_EVERY = 100
IDLE_SECONDS = 15 * 60

# Telethon auto-retries transient server errors (RpcMcgetFailError etc.)
# internally and logs a WARNING while doing so — harmless, but it's a raw
# library log line, not our structured format, so keep it off the console.
_logging.getLogger("telethon").setLevel(_logging.ERROR)


def _plain_text(value):
    """Poll question/answer text comes back as a TextWithEntities object
    (has a .text attribute), not a plain str, on current Telegram layers."""
    if value is None:
        return None
    return value.text if hasattr(value, "text") else value


def extract_set_position(text):
    if not text:
        return None, None
    m = SET_POSITION_RE.search(text_clean.normalize_digits(text))
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _find_correct_index(poll_results, poll):
    if not poll_results or not poll_results.results:
        return None
    for ans in poll_results.results:
        if getattr(ans, "correct", False):
            for i, opt in enumerate(poll.answers):
                if opt.option == ans.option:
                    return i
    return None


async def resolve_correct_answer(client, entity, message):
    media = message.media
    idx = _find_correct_index(media.results, media.poll)
    if idx is not None:
        return idx

    # Poll still open — vote option 0 to force Telegram to reveal the answer.
    option0 = media.poll.answers[0].option
    try:
        await client(SendVoteRequest(peer=entity, msg_id=message.id, options=[option0]))
    except Exception:
        return None
    await asyncio.sleep(random.uniform(2, 5))
    try:
        updated = await client.get_messages(entity, ids=message.id)
    except Exception:
        return None
    if updated and isinstance(updated.media, MessageMediaPoll):
        return _find_correct_index(updated.media.results, updated.media.poll)
    return None


def _is_paired_photo(prev_message, poll_message):
    if prev_message is None or not isinstance(prev_message.media, MessageMediaPhoto):
        return False
    if prev_message.sender_id != poll_message.sender_id:
        return False
    delta = abs((poll_message.date - prev_message.date).total_seconds())
    return delta <= 30


async def handle_quiz_message(
    client, conn, entity, message, prev_message, handle, lang_default,
    promo_phrases, flag_words, position_ranges, subject_keywords, images_dir,
):
    poll = message.media.poll
    options_text = [_plain_text(a.text) for a in poll.answers][:4]
    while len(options_text) < 4:
        options_text.append(None)

    raw_question_text = _plain_text(poll.question) or ""
    set_position, set_total = extract_set_position(raw_question_text)

    question_type = "text"
    image_path = None
    if _is_paired_photo(prev_message, message):
        question_type = "image"
        os.makedirs(images_dir, exist_ok=True)
        image_path = os.path.join(images_dir, f"{handle}_{message.id}.jpg")
        await prev_message.download_media(file=image_path)

    cleaned_text = text_clean.clean_question_text(raw_question_text, promo_phrases)
    cleaned_options = [
        text_clean.clean_question_text(o, promo_phrases) if o else o for o in options_text
    ]

    fingerprint = text_clean.compute_fingerprint(cleaned_text, cleaned_options[0] or "")
    if db.fingerprint_exists(conn, fingerprint):
        return "duplicate", False

    correct_index = await resolve_correct_answer(client, entity, message)

    subject, subject_method, subject_conf = subject_tagger.tag_subject(
        cleaned_text, set_position, position_ranges, subject_keywords
    )
    lexicon_flag = text_clean.check_lexicon_flag(cleaned_text, flag_words)

    db.insert_question(
        conn,
        source_channel=handle,
        tg_chat_id=entity.id,
        tg_message_id=message.id,
        lang=lang_default,
        question_type=question_type,
        question_text=cleaned_text,
        image_path=image_path,
        option_a=cleaned_options[0],
        option_b=cleaned_options[1],
        option_c=cleaned_options[2],
        option_d=cleaned_options[3],
        correct_index=correct_index,
        subject=subject,
        subject_method=subject_method,
        subject_conf=subject_conf,
        set_position=set_position,
        set_total=set_total,
        fingerprint=fingerprint,
        lexicon_flag=1 if lexicon_flag else 0,
    )
    return "saved", lexicon_flag


async def process_source(client, conn, cfg, source_cfg, stop_event):
    handle = source_cfg["handle"]
    lang_default = source_cfg.get("lang_default", "fa")
    images_dir = cfg["paths"]["question_images_dir"]

    state = db.get_source_state(conn, handle)
    entity = await client.get_entity(handle)
    # backfill_complete is the authoritative flag — last_seen_message_id alone
    # isn't enough, since it's already nonzero the moment a backfill resumes
    # after an interruption (checkpointed every 100 messages), which would
    # otherwise be mistaken for "backfill already done, only check new ones".
    is_backfill = not state["backfill_complete"]

    if is_backfill and state["last_seen_message_id"] == 0:
        logger.info(COMPONENT, f'reading the full history of "{handle}" for the first time')
        logger.detail(COMPONENT, "this takes a while on a big channel, and picks up where it left off if you stop the app")
    elif is_backfill:
        logger.info(
            COMPONENT,
            f'continuing to read the history of "{handle}" '
            f'({state["messages_scanned"]:,} messages read so far)',
        )
    else:
        logger.info(COMPONENT, f'checking "{handle}" for new questions')

    promo_phrases = text_clean.load_promo_phrases(cfg["paths"]["promo_phrases_file"])
    flag_words = text_clean.load_lexicon_flag_words(cfg["paths"]["lexicon_file"])
    position_ranges = subject_tagger.load_position_ranges(cfg["paths"]["subject_positions_file"])
    subject_keywords = subject_tagger.load_subject_keywords(cfg["paths"]["subject_keywords_file"])

    messages_scanned = 0
    questions_saved = 0
    duplicates_skipped = 0
    flagged_for_review = 0
    last_id_seen = state["last_seen_message_id"]
    prev_message = None

    approx_total = None
    if is_backfill:
        try:
            first_batch = await client.get_messages(entity, limit=1)
            approx_total = first_batch.total
        except Exception:
            approx_total = None

    min_id = state["last_seen_message_id"]

    interrupted = False
    async for message in client.iter_messages(entity, reverse=True, min_id=min_id):
        if stop_event.is_set():
            interrupted = True
            break

        messages_scanned += 1
        last_id_seen = message.id

        if isinstance(message.media, MessageMediaPoll) and message.media.poll.quiz:
            try:
                result, flagged = await handle_quiz_message(
                    client, conn, entity, message, prev_message, handle, lang_default,
                    promo_phrases, flag_words, position_ranges, subject_keywords, images_dir,
                )
                if result == "saved":
                    questions_saved += 1
                    if flagged:
                        flagged_for_review += 1
                elif result == "duplicate":
                    duplicates_skipped += 1
            except Exception as exc:
                logger.error(
                    COMPONENT,
                    f'could not read one question from "{handle}" — skipping just that one',
                    exc,
                    next_step="reading continues normally with the next message",
                )

        prev_message = message

        if messages_scanned % BACKFILL_CHECKPOINT_EVERY == 0:
            db.update_source_state(
                conn, handle,
                last_seen_message_id=last_id_seen,
                messages_scanned=state["messages_scanned"] + messages_scanned,
                questions_found=state["questions_found"] + questions_saved,
            )
            done_total = state["messages_scanned"] + messages_scanned
            if approx_total:
                pct = min(100, int(done_total * 100 / max(approx_total, 1)))
                logger.detail(
                    COMPONENT,
                    f'"{handle}": {done_total:,} of about {approx_total:,} messages read ({pct}%) '
                    f"— {questions_saved:,} new questions found so far",
                )
            else:
                logger.detail(
                    COMPONENT,
                    f'"{handle}": {done_total:,} messages read — {questions_saved:,} new questions found so far',
                )

    db.update_source_state(
        conn, handle,
        last_seen_message_id=last_id_seen,
        backfill_complete=0 if interrupted else 1,
        messages_scanned=state["messages_scanned"] + messages_scanned,
        questions_found=state["questions_found"] + questions_saved,
    )

    if interrupted:
        logger.info(COMPONENT, f'paused reading "{handle}" — it will resume from here next time')
        return

    if messages_scanned == 0:
        logger.detail(COMPONENT, f'"{handle}": nothing new since last check')
        return

    logger.summary(COMPONENT, f'finished reading "{handle}"', [
        ("messages read", f"{messages_scanned:,}"),
        ("new questions saved", f"{questions_saved:,}"),
        ("already had these", f"{duplicates_skipped:,}"),
        ("marked for a closer look", f"{flagged_for_review:,}"),
    ])


def _all_history_read(conn, cfg):
    return all(
        db.get_source_state(conn, s["handle"])["backfill_complete"]
        for s in cfg["sources"]
    )


async def connect(cfg):
    """Sign in to Telegram. The first run asks for a phone number and the login
    code on the console; after that the saved session file logs in silently."""
    tg_cfg = cfg["telegram"]
    client = TelegramClient(tg_cfg["session_name"], tg_cfg["api_id"], tg_cfg["api_hash"])
    logger.setup(COMPONENT, "signing in to Telegram...")
    await client.start()
    me = await client.get_me()
    who = me.username or me.phone or "your account"
    logger.ok(COMPONENT, f"signed in to Telegram as {who}")
    return client


async def run(conn, cfg, stop_event, client):
    """Keep reading the channels for as long as the app is running.

    While any channel still has unread history this loops back immediately and
    keeps going. Once every channel is fully read it settles into a check every
    15 minutes for newly posted questions.
    """
    handles = ", ".join(f'"{s["handle"]}"' for s in cfg["sources"])
    logger.ok(COMPONENT, f"scraper running — watching {len(cfg['sources'])} channels: {handles}")

    while not stop_event.is_set():
        for source_cfg in cfg["sources"]:
            if stop_event.is_set():
                break
            try:
                await process_source(client, conn, cfg, source_cfg, stop_event)
            except Exception as exc:
                logger.error(
                    COMPONENT,
                    f'could not read the channel "{source_cfg["handle"]}"',
                    exc,
                    next_step="the app will try this channel again in 15 minutes; other channels are unaffected",
                )

        if stop_event.is_set():
            break

        if _all_history_read(conn, cfg):
            counts = db.count_questions_by_status(conn)
            logger.info(
                COMPONENT,
                f"all channel history has been read — {counts.get('pending_review', 0):,} questions "
                "are waiting for your review",
            )
            logger.detail(COMPONENT, "checking again for new questions in 15 minutes")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=IDLE_SECONDS)
            except asyncio.TimeoutError:
                pass
