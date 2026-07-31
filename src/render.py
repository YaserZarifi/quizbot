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
import paths
import text_clean

def _pin_browser_location():
    """Force Chromium to live in the normal per-user cache.

    Left alone, a PyInstaller build resolves browsers to a .local-browsers
    folder inside the frozen app instead of %LOCALAPPDATA%\\ms-playwright. The
    installer then reports success while the launcher looks somewhere else, and
    rendering fails with "Executable doesn't exist" on a machine that plainly
    has Chromium installed. Pinning the location makes both agree, and reuses a
    copy already downloaded by any other Playwright app on the machine.

    A deliberate override is honoured; the '0' that the frozen build ends up
    with is not, because that is the value causing the problem.
    """
    current = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if current and current != "0":
        return
    local_appdata = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local"
    )
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(local_appdata, "ms-playwright")


_pin_browser_location()

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


def _installer_command():
    """How to invoke Playwright's own installer.

    From source that's `python -m playwright`. Inside the .exe there is no
    `python` to run — sys.executable is kankor-bot.exe itself — so we call the
    Node driver that ships inside the playwright package directly. Both forms
    download into the same per-user cache (%LOCALAPPDATA%\\ms-playwright), so a
    machine that already ran it from source needs no download at all.
    """
    if getattr(sys, "frozen", False):
        from playwright._impl._driver import compute_driver_executable

        node_exe, cli_js = compute_driver_executable()
        return [node_exe, cli_js, "install", "chromium"]
    return [sys.executable, "-m", "playwright", "install", "chromium"]


async def ensure_chromium_installed(component="render"):
    """Make sure the image renderer can start, downloading it once if needed."""
    try:
        await _get_browser()
        logger.ok(component, "image renderer ready")
        return
    except Exception:
        pass

    logger.setup(component, "first run: downloading the image renderer (about 200 MB, one time only)")
    logger.detail(component, "this needs internet; every later start is instant and works offline")
    try:
        result = subprocess.run(_installer_command(), capture_output=True, text=True)
    except Exception as exc:
        logger.error(
            component,
            "could not start the image renderer download",
            exc,
            next_step="the app cannot make images until this succeeds — check your internet and restart",
        )
        raise RuntimeError("chromium install failed") from exc

    if result.returncode != 0:
        detail = (result.stdout or "") + (result.stderr or "")
        logger.error(
            component,
            "the image renderer failed to download",
            RuntimeError(detail),
            next_step="check your internet connection and restart the app; details are in logs\\error.log",
        )
        raise RuntimeError("chromium install failed")

    logger.ok(component, "image renderer installed — future starts will be instant")
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


# ---- colour helpers -------------------------------------------------------
# themes.json only gives three flat colours per palette. The card design needs
# tints, glows and gradients derived from those, so they are computed here
# rather than asking anyone to hand-write a dozen more hex codes per subject.

def _rgb(hex_colour):
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgba(hex_colour, alpha):
    r, g, b = _rgb(hex_colour)
    return f"rgba({r}, {g}, {b}, {alpha})"


def _mix(hex_a, hex_b, t):
    """Blend two colours. t=0 gives hex_a, t=1 gives hex_b."""
    a, b = _rgb(hex_a), _rgb(hex_b)
    blended = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return "#%02X%02X%02X" % blended


def _lighten(hex_colour, t):
    return _mix(hex_colour, "#FFFFFF", t)


def _darken(hex_colour, t):
    return _mix(hex_colour, "#000000", t)


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
    -webkit-font-smoothing: antialiased;
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
    """Image questions embed their downloaded photo; text questions show text.

    Stored image paths are relative to the app folder, and the app is not
    guaranteed to be *running* from there (a shortcut or Task Scheduler entry
    sets its own working directory), so they are anchored explicitly rather
    than left to chance.
    """
    if question.get("question_type") == "image" and question.get("image_path"):
        image_path = paths.writable(question["image_path"])
        if os.path.exists(image_path):
            img_uri = _data_uri(image_path)
            return f'<div class="question-image-frame"><img src="{img_uri}"></div>'
        # One missing photo must not sink the whole post — fall through to
        # whatever text the question has, and say so plainly in the log.
        logger.warn(
            "render",
            f"question {question.get('public_id')} has a picture that is missing from disk "
            f"({image_path}) — showing its text only",
        )
    return f'<div class="question-text">{_escape(question.get("question_text"))}</div>'


def _options_html(question, theme, highlight_index=None):
    """Option rows. With highlight_index set (the answer card) the right option
    is filled and ticked, and the others are dimmed instead of hidden."""
    options = [question.get("option_a"), question.get("option_b"), question.get("option_c"), question.get("option_d")]
    rows = []
    for i, (letter, text) in enumerate(zip(LETTERS, options)):
        if not text:
            continue
        if highlight_index is None:
            css_class, tick = "option-row", ""
        elif i == highlight_index:
            css_class, tick = "option-row correct", '<div class="tick">✓</div>'
        else:
            css_class, tick = "option-row muted", ""
        rows.append(f"""
        <div class="{css_class}">
            <div class="option-letter">{letter}</div>
            <div class="option-text">{_escape(text)}</div>
            {tick}
        </div>""")
    return "\n".join(rows)


def _card_shell(inner, theme, scale):
    """The frame every card shares: background, soft orbs, glass panel."""
    return f"""
    <style>{_card_css(theme, scale)}</style>
    <div class="stage">
      <div class="orb orb-a"></div>
      <div class="orb orb-b"></div>
      <div class="card">
        {inner}
      </div>
    </div>
    """


def _card_top(question):
    return f"""
        <div class="card-top">
          <div class="mark"><i></i><i></i><i></i></div>
          {_counter_html(question)}
        </div>"""


def _card_footer(question, right_text=""):
    right = f'<span class="answer-notice">{_escape(right_text)}</span>' if right_text else "<span></span>"
    return f"""
        <div class="footer">
          <span class="public-id">{question['public_id']}</span>
          {right}
        </div>"""


def _card_css(theme, scale=1.0):
    """The card's look. scale nudges type up slightly on the taller story canvas.

    Everything is derived from the palette's three colours, so a new subject
    palette in themes.json gets the full treatment automatically.

    No subject is ever named on the card — subject detection is unreliable, and
    the palette is the only thing it still influences. A wrong guess means a
    different colour, never a wrong label in front of readers.
    """
    accent = theme["accent"]
    dark = theme["text_dark"]
    bg = theme["bg"]

    def px(value):
        return f"{round(value * scale)}px"

    return f"""
    .stage {{
        position: relative; width: 100%; height: 100%; padding: 52px; overflow: hidden;
        background:
            radial-gradient(circle at 82% 10%, {_rgba(accent, 0.16)} 0%, transparent 46%),
            radial-gradient(circle at 12% 92%, {_rgba(accent, 0.12)} 0%, transparent 44%),
            linear-gradient(158deg, {_lighten(bg, 0.35)} 0%, {bg} 52%, {_mix(bg, accent, 0.10)} 100%);
    }}
    /* Soft out-of-focus shapes. They sit behind the card and give the flat
       background some depth without competing with the text. */
    .orb {{ position: absolute; border-radius: 50%; filter: blur(60px); }}
    .orb-a {{ width: 460px; height: 460px; top: -150px; left: -120px;
        background: {_rgba(accent, 0.20)}; }}
    .orb-b {{ width: 360px; height: 360px; bottom: -130px; right: -90px;
        background: {_rgba(_lighten(accent, 0.35), 0.22)}; }}

    .card {{
        position: relative; width: 100%; height: 100%;
        display: flex; flex-direction: column;
        padding: 56px 52px 44px;
        border-radius: 44px;
        background: {_rgba('#FFFFFF', 0.86)};
        border: 1px solid {_rgba('#FFFFFF', 0.9)};
        box-shadow: 0 40px 90px {_rgba(dark, 0.16)}, 0 2px 0 {_rgba('#FFFFFF', 0.6)} inset;
    }}

    .card-top {{ display: flex; justify-content: space-between; align-items: center;
        margin-bottom: 34px; }}
    /* A wordless mark: three accent bars. Deliberately not text, so nothing on
       the card can be wrong in a language the code does not read. */
    .mark {{ display: flex; align-items: center; gap: 7px; }}
    .mark i {{ display: block; width: 9px; border-radius: 999px; background: {accent}; }}
    .mark i:nth-child(1) {{ height: 20px; opacity: 0.35; }}
    .mark i:nth-child(2) {{ height: 32px; opacity: 0.65; }}
    .mark i:nth-child(3) {{ height: 26px; opacity: 1; }}

    .counter {{ font-size: {px(25)}; font-weight: 500; color: {_mix(dark, accent, 0.3)};
        background: {_rgba(accent, 0.10)}; padding: 9px 22px; border-radius: 999px; }}

    .body {{ flex: 1; display: flex; flex-direction: column; justify-content: center; min-height: 0; }}

    .question-text {{ font-size: {px(43)}; font-weight: 500; color: {dark};
        line-height: 1.62; margin-bottom: {px(40)}; }}
    .question-image-frame {{ border-radius: 26px; padding: 18px; margin-bottom: {px(36)};
        background: {_rgba(accent, 0.06)}; border: 1px solid {_rgba(accent, 0.16)};
        display: flex; align-items: center; justify-content: center;
        max-height: 430px; overflow: hidden; }}
    .question-image-frame img {{ max-width: 100%; max-height: 394px; object-fit: contain;
        border-radius: 14px; }}

    .options {{ display: flex; flex-direction: column; gap: {px(17)}; }}
    .option-row {{ display: flex; align-items: center; gap: 20px;
        background: #FFFFFF; border: 1.5px solid {_rgba(accent, 0.13)}; border-radius: 22px;
        padding: {px(20)} {px(26)};
        box-shadow: 0 6px 18px {_rgba(dark, 0.06)}; }}
    .option-letter {{ width: {px(54)}; height: {px(54)}; border-radius: 50%; flex-shrink: 0;
        display: flex; align-items: center; justify-content: center;
        font-size: {px(25)}; font-weight: 500; color: #FFFFFF;
        background: linear-gradient(140deg, {_lighten(accent, 0.18)} 0%, {accent} 60%, {_darken(accent, 0.12)} 100%);
        box-shadow: 0 8px 18px {_rgba(accent, 0.34)}; }}
    .option-text {{ font-size: {px(31)}; color: {dark}; line-height: 1.45; }}

    /* Answer card: the right option is filled, the rest recede rather than
       disappear, so the reader can still see what they picked. */
    .option-row.correct {{
        background: linear-gradient(135deg, {accent} 0%, {_darken(accent, 0.16)} 100%);
        border-color: transparent;
        box-shadow: 0 16px 34px {_rgba(accent, 0.40)}; }}
    .option-row.correct .option-text {{ color: #FFFFFF; font-weight: 500; }}
    .option-row.correct .option-letter {{
        background: #FFFFFF; color: {accent};
        box-shadow: 0 6px 14px {_rgba(dark, 0.22)}; }}
    .option-row.correct .tick {{ margin-inline-start: auto; font-size: {px(30)}; color: #FFFFFF; }}
    .option-row.muted {{ opacity: 0.5; box-shadow: none; }}

    .banner {{ display: inline-flex; align-items: center; gap: 14px; align-self: flex-start;
        background: {_rgba(accent, 0.12)}; color: {_mix(dark, accent, 0.35)};
        font-size: {px(30)}; font-weight: 500;
        padding: 14px 28px; border-radius: 999px; margin-bottom: {px(34)}; }}
    .banner .dot {{ width: 12px; height: 12px; border-radius: 50%; background: {accent}; }}

    .footer {{ display: flex; justify-content: space-between; align-items: center;
        margin-top: auto; padding-top: 26px;
        border-top: 1px solid {_rgba(dark, 0.10)};
        font-size: {px(23)}; color: {_rgba(dark, 0.6)}; }}
    .footer .public-id {{ font-weight: 500; letter-spacing: 0.5px; }}
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


def _question_card_html(question, theme, strings, cfg, viewport, scale):
    s = strings.get(question["lang"], strings["fa"])
    inner = f"""
        {_card_top(question)}
        <div class="body">
          {_question_body_html(question)}
          <div class="options">{_options_html(question, theme)}</div>
        </div>
        {_card_footer(question, s['answer_notice'])}"""
    return _base_html(
        _card_shell(inner, theme, scale),
        viewport["width"], viewport["height"], cfg["paths"]["fonts_dir"],
    )


async def render_feed_card(question, theme, strings, cfg):
    html = _question_card_html(question, theme, strings, cfg, FEED_VIEWPORT, 1.0)
    return await _screenshot(html, FEED_VIEWPORT, cfg["paths"]["output_dir"], question["public_id"], "feed")


async def render_story_question(question, theme, strings, cfg):
    # The story canvas is nearly twice as tall, so type steps up to match.
    html = _question_card_html(question, theme, strings, cfg, STORY_VIEWPORT, 1.18)
    return await _screenshot(html, STORY_VIEWPORT, cfg["paths"]["output_dir"], question["public_id"], "story_q")


async def render_story_answer(question, theme, strings, cfg):
    s = strings.get(question["lang"], strings["fa"])
    correct_index = question.get("correct_index")
    letter = LETTERS[correct_index] if correct_index is not None and correct_index < len(LETTERS) else "?"
    prefix = s["correct_prefix"].format(opt=letter)

    inner = f"""
        {_card_top(question)}
        <div class="body">
          <div class="banner"><span class="dot"></span>{_escape(prefix)}</div>
          {_question_body_html(question)}
          <div class="options">{_options_html(question, theme, highlight_index=correct_index)}</div>
        </div>
        {_card_footer(question)}"""

    html = _base_html(
        _card_shell(inner, theme, 1.18),
        STORY_VIEWPORT["width"], STORY_VIEWPORT["height"], cfg["paths"]["fonts_dir"],
    )
    return await _screenshot(html, STORY_VIEWPORT, cfg["paths"]["output_dir"], question["public_id"], "story_a")
