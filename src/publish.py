"""Facebook Graph API calls — uploading rendered cards and posting them.
See kankor_quiz_bot_spec.md §8.4.

Every post_queue entry now needs an admin's explicit Approve tap in Telegram
before this module is ever called (see main.py / review_bot.py) — so by the
time these functions run, "now" already is the right moment to publish.
That's why feed posts publish immediately rather than using Facebook's
scheduled_publish_time: a post approved minutes or hours after its original
slot would otherwise fail Facebook's "must be >=10 minutes in the future"
scheduling rule.

Every Graph API call is wrapped in try/except: on failure the post_queue row
is bumped for retry and a short friendly line goes to console, full response
detail goes to error.log — never a raw HTTP error on screen.
"""

import json
import os

import requests

import db
import logger
import render

COMPONENT = "publish"
GRAPH_API_BASE = "https://graph.facebook.com/v19.0"


def _upload_unpublished_photo(image_path, cfg):
    page_id = cfg["facebook"]["page_id"]
    token = cfg["facebook"]["page_access_token"]
    with open(image_path, "rb") as f:
        resp = requests.post(
            f"{GRAPH_API_BASE}/{page_id}/photos",
            data={"published": "false", "access_token": token},
            files={"source": f},
            timeout=60,
        )
    resp.raise_for_status()
    return resp.json()["id"]


def build_feed_caption(lang, strings, cfg):
    """The exact caption text a feed post will be published with — also used
    by review_bot.py to show the admin what they're approving."""
    s = strings.get(lang, strings["fa"])
    hashtags = cfg["facebook"].get("hashtags", "")
    parts = [s["post_caption"]]
    if hashtags:
        parts.append(hashtags)
    return "\n\n".join(parts)


def _create_feed_post(photo_ids, caption, cfg):
    page_id = cfg["facebook"]["page_id"]
    token = cfg["facebook"]["page_access_token"]
    data = {
        "attached_media": json.dumps([{"media_fbid": pid} for pid in photo_ids]),
        "message": caption,
        "published": "true",
        "access_token": token,
    }
    resp = requests.post(f"{GRAPH_API_BASE}/{page_id}/feed", data=data, timeout=60)
    resp.raise_for_status()
    return resp.json()["id"]


def _create_photo_story(photo_id, cfg):
    page_id = cfg["facebook"]["page_id"]
    token = cfg["facebook"]["page_access_token"]
    resp = requests.post(
        f"{GRAPH_API_BASE}/{page_id}/photo_stories",
        data={"photo_id": photo_id, "access_token": token},
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("post_id") or body.get("id")


def _themes_and_strings(cfg):
    themes = render.load_themes(cfg["paths"]["themes_file"])
    strings = render.load_strings(cfg["paths"]["strings_file"])
    return themes, strings


def _theme_for(question, themes):
    return themes.get(question["subject"], themes["عمومی"])


async def publish_feed(conn, cfg, entry):
    """entry.kind == 'feed': render each question's card, upload as unpublished
    photos, then create one multi-photo Page post with the standard caption
    + hashtags, published immediately (admin approval already gated the timing)."""
    question_ids = json.loads(entry["question_ids"])
    questions = db.get_questions_by_ids(conn, question_ids)
    if not questions:
        db.set_queue_status(conn, entry["id"], "failed", "no matching questions in database", bump_attempts=True)
        logger.error(COMPONENT, f"feed post #{entry['id']} skipped — no matching questions in database")
        return False

    themes, strings = _themes_and_strings(cfg)
    try:
        photo_ids = []
        for q in questions:
            png_path = await render.render_feed_card(q, _theme_for(q, themes), strings, cfg)
            photo_ids.append(_upload_unpublished_photo(png_path, cfg))

        caption = build_feed_caption(entry["lang"], strings, cfg)
        post_id = _create_feed_post(photo_ids, caption, cfg)

        db.mark_questions_posted(conn, question_ids, fb_feed_post_id=post_id)
        db.set_queue_status(conn, entry["id"], "done")
        logger.ok(COMPONENT, f"feed post published for {entry['lang']} — {len(questions)} questions")
        return True
    except Exception as exc:
        db.set_queue_status(conn, entry["id"], "pending", str(exc), bump_attempts=True)
        logger.error(COMPONENT, "Facebook rejected the feed post — see error.log for details", exc)
        return False


async def publish_story_question(conn, cfg, entry):
    """entry.kind == 'story_q': one Story per question, posted right now
    (no server-side scheduling exists for stories)."""
    question_ids = json.loads(entry["question_ids"])
    questions = db.get_questions_by_ids(conn, question_ids)
    if not questions:
        db.set_queue_status(conn, entry["id"], "failed", "no matching questions in database", bump_attempts=True)
        logger.error(COMPONENT, f"question story #{entry['id']} skipped — no matching questions in database")
        return False

    themes, strings = _themes_and_strings(cfg)
    try:
        story_id = None
        for q in questions:
            png_path = await render.render_story_question(q, _theme_for(q, themes), strings, cfg)
            photo_id = _upload_unpublished_photo(png_path, cfg)
            story_id = _create_photo_story(photo_id, cfg)

        db.mark_questions_posted(conn, question_ids, fb_story_id=story_id)
        db.set_queue_status(conn, entry["id"], "done")
        logger.ok(COMPONENT, f"question story posted for {entry['lang']} — {len(questions)} questions")
        return True
    except Exception as exc:
        db.set_queue_status(conn, entry["id"], "pending", str(exc), bump_attempts=True)
        logger.error(COMPONENT, "Facebook rejected the question story — see error.log for details", exc)
        return False


async def publish_story_answer(conn, cfg, entry):
    """entry.kind == 'story_a': the answer-reveal Story, posted answer_delay_hours
    after the question story, matched by question id."""
    question_ids = json.loads(entry["question_ids"])
    questions = db.get_questions_by_ids(conn, question_ids)
    if not questions:
        db.set_queue_status(conn, entry["id"], "failed", "no matching questions in database", bump_attempts=True)
        logger.error(COMPONENT, f"answer story #{entry['id']} skipped — no matching questions in database")
        return False

    themes, strings = _themes_and_strings(cfg)
    try:
        story_id = None
        for q in questions:
            png_path = await render.render_story_answer(q, _theme_for(q, themes), strings, cfg)
            photo_id = _upload_unpublished_photo(png_path, cfg)
            story_id = _create_photo_story(photo_id, cfg)

        db.mark_answers_posted(conn, question_ids, fb_answer_story_id=story_id)
        db.set_queue_status(conn, entry["id"], "done")
        logger.ok(COMPONENT, f"answer story posted for {entry['lang']} — {len(questions)} questions")
        return True
    except Exception as exc:
        db.set_queue_status(conn, entry["id"], "pending", str(exc), bump_attempts=True)
        logger.error(COMPONENT, "Facebook rejected the answer story — see error.log for details", exc)
        return False
