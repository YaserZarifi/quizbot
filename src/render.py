"""HTML/CSS templates rendered to PNG cards via Playwright headless Chromium.
See kankor_quiz_bot_spec.md §8.3.

One shared browser instance is reused across a whole batch of renders in a
single process — launched lazily on first use, left open for the life of
the process (call close_browser() on clean shutdown).
"""

import asyncio
import base64
import functools
import json
import mimetypes
import os
import subprocess
import sys

from playwright.async_api import async_playwright

import logger
import text_clean

FEED_VIEWPORT = {"width": 1080, "height": 1080}
STORY_VIEWPORT = {"width": 1080, "height": 1920}
LETTERS = ["الف", "ب", "ج", "د"]

_playwright_ctx = None
_browser = None
_lock = asyncio.Lock()


def load_themes(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_strings(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


async def _get_browser():
    global _playwright_ctx, _browser
    async with _lock:
        if _browser is None:
            _playwright_ctx = await async_playwright().start()
            _browser = await _playwright_ctx.chromium.launch()
    return _browser


async def ensure_chromium_installed(component="main"):
    """Try launching Chromium; if it's not installed yet, run Playwright's own
    installer once (first run only — later runs find it already cached in the
    default per-user location, see spec §3). Never bundled into the .exe."""
    try:
        await _get_browser()
        return
    except Exception:
        pass

    logger.setup(component, "downloading rendering engine (one-time, ~200 MB)...")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        detail = (result.stdout or "") + (result.stderr or "")
        logger.error(
            component,
            "automatic download failed — run 'playwright install chromium' manually once, see error.log",
            RuntimeError(detail),
        )
        raise RuntimeError("chromium install failed")

    logger.ok(component, "done — future runs will start instantly and work offline")
    await _get_browser()


async def close_browser():
    global _playwright_ctx, _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright_ctx is not None:
        await _playwright_ctx.stop()
        _playwright_ctx = None


def _data_uri(path, mime=None):
    """Base64-embed a local file as a data: URI. Chromium's set_content() runs
    pages at an about:blank origin, which blocks file:// resource loading for
    <img>/@font-face — data URIs sidestep that entirely and stay fully offline."""
    if mime is None:
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _escape(text):
    if text is None:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@functools.lru_cache(maxsize=4)
def _font_face_css(fonts_dir):
    regular = _data_uri(os.path.join(fonts_dir, "Vazirmatn-Regular.ttf"), "font/ttf")
    medium = _data_uri(os.path.join(fonts_dir, "Vazirmatn-Medium.ttf"), "font/ttf")
    naskh = _data_uri(os.path.join(fonts_dir, "NotoNaskhArabic-Regular.ttf"), "font/ttf")
    return f"""
    @font-face {{ font-family: 'Vazirmatn'; src: url({regular}); font-weight: 400; }}
    @font-face {{ font-family: 'Vazirmatn'; src: url({medium}); font-weight: 500; }}
    @font-face {{ font-family: 'NotoNaskhArabic'; src: url({naskh}); font-weight: 400; }}
    """


def _base_html(body_and_style, width, height, fonts_dir):
    return f"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<style>
{_font_face_css(fonts_dir)}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ width: {width}px; height: {height}px; }}
body {{
    font-family: 'Vazirmatn', 'NotoNaskhArabic', sans-serif;
    direction: rtl;
    overflow: hidden;
}}
</style>
</head>
<body>
{body_and_style}
</body>
</html>"""


def _counter_html(question):
    if question.get("set_position") and question.get("set_total"):
        counter = text_clean.to_farsi_digits(f"{question['set_position']} / {question['set_total']}")
        return f'<div class="counter">{counter}</div>'
    return ""


def _question_body_html(question):
    if question.get("question_type") == "image" and question.get("image_path"):
        img_uri = _data_uri(question["image_path"])
        return f'<div class="question-image-frame"><img src="{img_uri}"></div>'
    return f'<div class="question-text">{_escape(question.get("question_text"))}</div>'


def _options_html(question, theme, highlight_index=None):
    options = [question.get("option_a"), question.get("option_b"), question.get("option_c"), question.get("option_d")]
    rows = []
    for i, (letter, text) in enumerate(zip(LETTERS, options)):
        if not text:
            continue
        extra_style = ""
        if highlight_index is not None and i == highlight_index:
            extra_style = f'style="background:{theme["accent"]}; color: white;"'
        rows.append(f"""
        <div class="option-row" {extra_style}>
            <div class="option-letter">{letter}</div>
            <div class="option-text">{_escape(text)}</div>
        </div>""")
    return "\n".join(rows)


def _card_css(theme):
    return f"""
    .card {{ width: 100%; height: 100%; position: relative; padding: 48px; }}
    .accent-bar {{ position: absolute; top: 0; left: 0; right: 0; height: 16px; background: {theme['accent']}; }}
    .content {{ margin-top: 40px; display: flex; flex-direction: column; height: calc(100% - 40px); }}
    .header-row {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }}
    .subject-badge {{ color: white; background: {theme['accent']}; padding: 10px 24px; border-radius: 999px; font-size: 28px; font-weight: 500; }}
    .counter {{ font-size: 28px; color: {theme['text_dark']}; }}
    .question-text {{ font-size: 40px; color: {theme['text_dark']}; line-height: 1.5; margin-bottom: 32px; }}
    .question-image-frame {{ border: 3px dashed {theme['accent']}; border-radius: 16px; padding: 16px;
        margin-bottom: 32px; display: flex; align-items: center; justify-content: center;
        max-height: 420px; overflow: hidden; }}
    .question-image-frame img {{ max-width: 100%; max-height: 400px; object-fit: contain; }}
    .options {{ display: flex; flex-direction: column; gap: 16px; }}
    .option-row {{ display: flex; align-items: center; gap: 16px; background: white; border-radius: 16px; padding: 20px 24px; }}
    .option-letter {{ width: 48px; height: 48px; border-radius: 50%; background: {theme['accent']}; color: white;
        display: flex; align-items: center; justify-content: center; font-size: 26px; font-weight: 500; flex-shrink: 0; }}
    .option-text {{ font-size: 32px; color: {theme['text_dark']}; }}
    .footer {{ display: flex; justify-content: space-between; align-items: center; margin-top: 24px;
        font-size: 24px; color: {theme['text_dark']}; opacity: 0.8; }}
    """


async def _screenshot(html_content, viewport, output_dir, public_id, kind):
    os.makedirs(output_dir, exist_ok=True)
    browser = await _get_browser()
    page = await browser.new_page(viewport=viewport)
    try:
        await page.set_content(html_content, wait_until="load")
        png_path = os.path.join(output_dir, f"{public_id}_{kind}.png")
        await page.screenshot(path=png_path)
    finally:
        await page.close()
    return png_path


async def render_feed_card(question, theme, strings, cfg):
    s = strings.get(question["lang"], strings["fa"])
    body = f"""
    <style>{_card_css(theme)}</style>
    <div class="card" style="background:{theme['bg']};">
      <div class="accent-bar"></div>
      <div class="content">
        <div class="header-row">
          <div class="subject-badge">{_escape(question['subject'])}</div>
          {_counter_html(question)}
        </div>
        {_question_body_html(question)}
        <div class="options">{_options_html(question, theme)}</div>
        <div class="footer">
          <span class="public-id">{question['public_id']}</span>
          <span class="answer-notice">{_escape(s['answer_notice'])}</span>
        </div>
      </div>
    </div>
    """
    html = _base_html(body, FEED_VIEWPORT["width"], FEED_VIEWPORT["height"], cfg["paths"]["fonts_dir"])
    return await _screenshot(html, FEED_VIEWPORT, cfg["paths"]["output_dir"], question["public_id"], "feed")


async def render_story_question(question, theme, strings, cfg):
    s = strings.get(question["lang"], strings["fa"])
    body = f"""
    <style>{_card_css(theme)}</style>
    <div class="card" style="background:{theme['bg']};">
      <div class="accent-bar"></div>
      <div class="content">
        <div class="header-row">
          <div class="subject-badge">{_escape(question['subject'])}</div>
          {_counter_html(question)}
        </div>
        {_question_body_html(question)}
        <div class="options">{_options_html(question, theme)}</div>
        <div class="footer">
          <span class="public-id">{question['public_id']}</span>
          <span class="answer-notice">{_escape(s['answer_notice'])}</span>
        </div>
      </div>
    </div>
    """
    html = _base_html(body, STORY_VIEWPORT["width"], STORY_VIEWPORT["height"], cfg["paths"]["fonts_dir"])
    return await _screenshot(html, STORY_VIEWPORT, cfg["paths"]["output_dir"], question["public_id"], "story_q")


async def render_story_answer(question, theme, strings, cfg):
    s = strings.get(question["lang"], strings["fa"])
    correct_index = question.get("correct_index")
    letter = LETTERS[correct_index] if correct_index is not None and correct_index < len(LETTERS) else "?"
    prefix = s["correct_prefix"].format(opt=letter)
    body = f"""
    <style>{_card_css(theme)}</style>
    <div class="card" style="background:{theme['bg']};">
      <div class="accent-bar"></div>
      <div class="content">
        <div class="header-row">
          <div class="subject-badge">{_escape(question['subject'])}</div>
          {_counter_html(question)}
        </div>
        <div class="question-text">{_escape(prefix)}</div>
        <div class="options">{_options_html(question, theme, highlight_index=correct_index)}</div>
        <div class="footer">
          <span class="public-id">{question['public_id']}</span>
        </div>
      </div>
    </div>
    """
    html = _base_html(body, STORY_VIEWPORT["width"], STORY_VIEWPORT["height"], cfg["paths"]["fonts_dir"])
    return await _screenshot(html, STORY_VIEWPORT, cfg["paths"]["output_dir"], question["public_id"], "story_a")
