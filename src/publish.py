"""Facebook Graph API calls — uploading rendered cards and posting them.
See kankor_quiz_bot_spec.md §8.4.

Two things reach Facebook, and they are gated differently:

  publish_post()          one question -> a feed post AND a question story.
                          Only ever called after an admin taps Publish in
                          Telegram, so "now" is always the right moment to post
                          (which is why nothing uses Facebook's scheduled
                          publishing — a post approved an hour after its slot
                          would fail the "must be >=10 minutes ahead" rule).

  publish_answer_story()  the answer reveal, 6 hours later. No approval — the
                          scheduler only calls it once the parent post reached
                          'done', so approving the question approves its answer.

Every Graph API call is wrapped: on failure the post_queue row goes back to
pending for a later retry and a short, plain line goes to the console, while
the full HTTP detail goes to error.log — never a raw error on screen.
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


def themes_and_strings(cfg):
    themes = render.load_themes(cfg["paths"]["themes_file"])
    strings = render.load_strings(cfg["paths"]["strings_file"])
    return themes, strings


def theme_for(question, themes):
    """Colour palette only. The subject is never shown to readers (see
    render.py) — a mis-tagged question just gets a different colour."""
    return themes.get(question["subject"], themes["عمومی"])


def _first_question(conn, entry, what):
    """Every queue entry now carries exactly one question."""
    question_ids = json.loads(entry["question_ids"])
    questions = db.get_questions_by_ids(conn, question_ids)
    if not questions:
        db.set_queue_status(conn, entry["id"], "failed", "question missing from database", bump_attempts=True)
        logger.error(
            COMPONENT,
            f"could not post the {what} — its question is no longer in the database",
            next_step="this post has been dropped; the schedule continues normally",
        )
        return None, None
    return questions[0], question_ids


async def publish_post(conn, cfg, entry):
    """Publish one approved question: a feed post, then its question story."""
    question, question_ids = _first_question(conn, entry, "post")
    if question is None:
        return False

    themes, strings = themes_and_strings(cfg)
    theme = theme_for(question, themes)
    public_id = question["public_id"]

    try:
        logger.info(COMPONENT, f"posting question {public_id} to Facebook...")

        png_path = await render.render_feed_card(question, theme, strings, cfg)
        photo_id = _upload_unpublished_photo(png_path, cfg)
        caption = build_feed_caption(entry["lang"], strings, cfg)
        post_id = _create_feed_post([photo_id], caption, cfg)
        logger.detail(COMPONENT, "feed post published")

        story_png = await render.render_story_question(question, theme, strings, cfg)
        story_photo_id = _upload_unpublished_photo(story_png, cfg)
        story_id = _create_photo_story(story_photo_id, cfg)
        logger.detail(COMPONENT, "question story published")

        db.mark_questions_posted(conn, question_ids, fb_feed_post_id=post_id, fb_story_id=story_id)
        db.set_queue_status(conn, entry["id"], "done")
        logger.ok(COMPONENT, f"question {public_id} is live on Facebook — answer story follows in 6 hours")
        return True
    except Exception as exc:
        db.set_queue_status(conn, entry["id"], "pending", str(exc), bump_attempts=True)
        logger.error(
            COMPONENT,
            f"Facebook would not accept question {public_id}",
            exc,
            next_step="it stays in the queue and will be offered for approval again shortly",
        )
        return False


async def publish_answer_story(conn, cfg, entry):
    """The answer reveal, posted automatically 6 hours after its question."""
    question, question_ids = _first_question(conn, entry, "answer story")
    if question is None:
        return False

    themes, strings = themes_and_strings(cfg)
    public_id = question["public_id"]

    try:
        logger.info(COMPONENT, f"posting the answer story for question {public_id}...")
        png_path = await render.render_story_answer(question, theme_for(question, themes), strings, cfg)
        photo_id = _upload_unpublished_photo(png_path, cfg)
        story_id = _create_photo_story(photo_id, cfg)

        db.mark_answers_posted(conn, question_ids, fb_answer_story_id=story_id)
        db.set_queue_status(conn, entry["id"], "done")
        logger.ok(COMPONENT, f"answer story for question {public_id} is live — this question is now complete")
        return True
    except Exception as exc:
        db.set_queue_status(conn, entry["id"], "pending", str(exc), bump_attempts=True)
        logger.error(
            COMPONENT,
            f"Facebook would not accept the answer story for question {public_id}",
            exc,
            next_step="the app will try again automatically in a few minutes",
        )
        return False
