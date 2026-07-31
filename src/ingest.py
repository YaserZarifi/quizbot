"""Telegram scraper (Telethon userbot). Pulls quiz polls from configured
sources into the local database — see kankor_quiz_bot_spec.md §8.1.

First run per source does a full history backfill (resumable); later runs
only fetch messages newer than the stored last_seen_message_id.
"""

import asyncio
import logging as _logging
import os
import random
import re
import sys

import yaml
from telethon import TelegramClient
from telethon.tl.functions.messages import SendVoteRequest
from telethon.tl.types import MessageMediaPhoto, MessageMediaPoll

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import logger
import subject_tagger
import text_clean

COMPONENT = "ingest"
SET_POSITION_RE = re.compile(r"\[?(\d{1,3})\s*/\s*(\d{1,3})\]?")
BACKFILL_CHECKPOINT_EVERY = 100

# Telethon auto-retries transient server errors (RpcMcgetFailError etc.)
# internally and logs a WARNING while doing so — harmless, but it's a raw
# library log line, not our structured format, so keep it off the console.
_logging.getLogger("telethon").setLevel(_logging.ERROR)


def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


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
    promo_phrases, flag_words, position_ranges, subject_keywords,
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
        os.makedirs("question_images", exist_ok=True)
        image_path = os.path.join("question_images", f"{handle}_{message.id}.jpg")
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


async def process_source(client, conn, cfg, source_cfg):
    handle = source_cfg["handle"]
    lang_default = source_cfg.get("lang_default", "fa")

    state = db.get_source_state(conn, handle)
    entity = await client.get_entity(handle)
    # backfill_complete is the authoritative flag — last_seen_message_id alone
    # isn't enough, since it's already nonzero the moment a backfill resumes
    # after an interruption (checkpointed every 100 messages), which would
    # otherwise be mistaken for "backfill already done, only check new ones".
    is_backfill = not state["backfill_complete"]

    if is_backfill and state["last_seen_message_id"] == 0:
        logger.info(COMPONENT, f'starting full backfill of "{handle}"...')
    elif is_backfill:
        logger.info(COMPONENT, f'resuming full backfill of "{handle}"...')
    else:
        logger.info(COMPONENT, f'checking "{handle}" for new messages...')

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

    async for message in client.iter_messages(entity, reverse=True, min_id=min_id):
        messages_scanned += 1
        last_id_seen = message.id

        if isinstance(message.media, MessageMediaPoll) and message.media.poll.quiz:
            try:
                result, flagged = await handle_quiz_message(
                    client, conn, entity, message, prev_message, handle, lang_default,
                    promo_phrases, flag_words, position_ranges, subject_keywords,
                )
                if result == "saved":
                    questions_saved += 1
                    if flagged:
                        flagged_for_review += 1
                elif result == "duplicate":
                    duplicates_skipped += 1
            except Exception as exc:
                logger.error(COMPONENT, f'failed to process a quiz in "{handle}" — see error.log', exc)

        prev_message = message

        if messages_scanned % BACKFILL_CHECKPOINT_EVERY == 0:
            db.update_source_state(
                conn, handle,
                last_seen_message_id=last_id_seen,
                messages_scanned=state["messages_scanned"] + messages_scanned,
                questions_found=state["questions_found"] + questions_saved,
            )
            if approx_total:
                logger.info(COMPONENT, f"  ...{messages_scanned:,} / ~{approx_total:,} messages scanned so far")
            else:
                logger.info(COMPONENT, f"  ...{messages_scanned:,} messages scanned so far")

    db.update_source_state(
        conn, handle,
        last_seen_message_id=last_id_seen,
        backfill_complete=1,
        messages_scanned=state["messages_scanned"] + messages_scanned,
        questions_found=state["questions_found"] + questions_saved,
    )

    logger.summary(COMPONENT, f'finished "{handle}"', [
        ("messages scanned", f"{messages_scanned:,}"),
        ("questions saved", f"{questions_saved:,}"),
        ("duplicates skipped", f"{duplicates_skipped:,}"),
        ("flagged for review", f"{flagged_for_review:,}"),
    ])
    logger.blank()


async def run_ingest(config_path="config.yaml"):
    cfg = load_config(config_path)
    os.environ.setdefault("KANKOR_LOG_DIR", cfg["paths"].get("log_dir", "logs"))
    conn = db.connect(cfg["paths"]["db_file"])

    tg_cfg = cfg["telegram"]
    if not tg_cfg.get("api_id") or not tg_cfg.get("api_hash"):
        logger.error(COMPONENT, "telegram.api_id / api_hash are not set in config.yaml")
        return

    client = TelegramClient(tg_cfg["session_name"], tg_cfg["api_id"], tg_cfg["api_hash"])
    logger.setup(COMPONENT, "connecting to Telegram (first run may ask for phone + login code)...")
    await client.start()
    logger.ok(COMPONENT, "connected")
    logger.blank()

    for source_cfg in cfg["sources"]:
        try:
            await process_source(client, conn, cfg, source_cfg)
        except Exception as exc:
            logger.error(COMPONENT, f'failed to scrape "{source_cfg["handle"]}" — see error.log', exc)
            logger.blank()

    await client.disconnect()
    conn.close()


def main():
    asyncio.run(run_ingest())


if __name__ == "__main__":
    main()
