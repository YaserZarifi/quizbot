"""Entry point — this is what kankor-bot.exe runs. See spec §8.5.

Everything the bot does happens inside this one process, on one event loop,
at the same time:

  Scraper    reads the Telegram channels for new quiz questions, and keeps
             working through their old history until it has all of it
  Reviewer   shows you questions in Telegram to approve or reject, and shows
             you each finished post before it goes to Facebook
  Scheduler  keeps every posting slot in the day filled with an approved
             question, and posts each answer story 6 hours after its question

They run as three independent tasks. One of them hitting a problem does not
stop the other two — each reports what happened in plain language and carries
on, with the technical detail written to logs\\error.log.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import appconfig
import background
import db
import ingest
import logger
import paths
import render
import review_bot
import scheduler

COMPONENT = "main"


def _check_config(cfg):
    """Return a list of plain-language problems that stop the app from running."""
    problems = []
    tg = cfg.get("telegram", {})

    if not tg.get("api_id") or not tg.get("api_hash"):
        problems.append("telegram.api_id and telegram.api_hash are not filled in (get them from my.telegram.org)")
    if not tg.get("review_bot_token"):
        problems.append("telegram.review_bot_token is not filled in (get it from @BotFather on Telegram)")
    if not tg.get("admin_user_ids"):
        problems.append("telegram.admin_user_ids is empty — add your own Telegram user id so you can review")
    if not cfg.get("sources"):
        problems.append("sources is empty — add at least one Telegram channel to read questions from")

    fb = cfg.get("facebook", {})
    if not fb.get("page_id") or not fb.get("page_access_token"):
        problems.append("facebook.page_id and facebook.page_access_token are not filled in")

    sched = cfg.get("schedule", {})
    if not sched.get("post_times"):
        problems.append('schedule.post_times is missing — list the times to post at, e.g. ["08:00", "11:00"]')
    if sched.get("answer_delay_hours") is None:
        problems.append("schedule.answer_delay_hours is missing (6 means answers post 6 hours after the question)")

    for missing in appconfig.missing_assets(cfg):
        problems.append(f"a required file is missing: {missing}")

    return problems


def _print_startup_summary(cfg, conn):
    counts = db.count_questions_by_status(conn)
    logger.summary(COMPONENT, "ready — here is where things stand", [
        ("app folder", paths.base_dir()),
        ("channels watched", ", ".join(s["handle"] for s in cfg["sources"])),
        ("posts per day", f"{len(cfg['schedule']['post_times'])} at {', '.join(cfg['schedule']['post_times'])}"),
        ("answer story", f"{cfg['schedule']['answer_delay_hours']} hours after each question"),
        ("questions collected", f"{sum(counts.values()):,}"),
        ("waiting for review", f"{counts.get('pending_review', 0):,}"),
        ("approved, ready to post", f"{counts.get('approved', 0):,}"),
        ("already posted", f"{counts.get('posted', 0):,}"),
    ])


async def run_app(stop_event, shared=None):
    """Start everything and run until stop_event is set.

    shared is a plain dict the tray icon reads to show live numbers; it stays
    empty until the database is actually open.
    """
    shared = shared if shared is not None else {}
    logger.section("Kankor Quiz Bot — starting up")

    cfg = appconfig.load()

    problems = _check_config(cfg)
    if problems:
        logger.error(COMPONENT, "the app cannot start until config.yaml is fixed")
        for problem in problems:
            logger.detail(COMPONENT, f"- {problem}")
        logger.detail(COMPONENT, f"edit: {os.path.join(paths.base_dir(), 'config.yaml')}")
        return 1

    conn = db.connect(cfg["paths"]["db_file"])
    shared["conn"] = conn
    logger.ok(COMPONENT, f"database opened: {cfg['paths']['db_file']}")

    recovered = db.release_all_in_review(conn)
    if recovered:
        logger.info(COMPONENT, f"{recovered:,} questions were mid-review when the app last stopped — back in the queue")

    requeued = db.recover_interrupted_publishes(conn)
    if requeued:
        logger.info(COMPONENT, f"{requeued:,} posts were mid-publish when the app last stopped — queued again")

    await render.ensure_chromium_installed()

    try:
        client = await ingest.connect(cfg)
    except Exception as exc:
        logger.error(
            COMPONENT,
            "could not sign in to Telegram",
            exc,
            next_step="check telegram.api_id and api_hash in config.yaml, then start the app again",
        )
        return 1

    _print_startup_summary(cfg, conn)
    logger.section("Running — leave this window open, or close it to keep running in the background")

    tasks = {
        "scraper": asyncio.create_task(ingest.run(conn, cfg, stop_event, client)),
        "reviewer": asyncio.create_task(review_bot.run(conn, cfg, stop_event)),
        "scheduler": asyncio.create_task(scheduler.run(conn, cfg, stop_event)),
    }

    try:
        # If any one task dies outright, stop the rest rather than leaving the
        # app half-running and looking healthy.
        done, _ = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
        for name, task in tasks.items():
            if task in done and not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    logger.error(
                        COMPONENT,
                        f"the {name} stopped unexpectedly, so the app is shutting down",
                        exc,
                        next_step="start the app again; if it keeps happening, check logs\\error.log",
                    )
    finally:
        stop_event.set()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await client.disconnect()
        await render.close_browser()
        conn.close()
        logger.info(COMPONENT, "stopped cleanly")

    return 0


async def _amain(shared=None, control=None):
    stop_event = asyncio.Event()
    if control is not None:
        # Handed out so the tray icon's Quit can reach into this loop from the
        # thread it runs on. asyncio.Event is not thread-safe on its own.
        control["loop"] = asyncio.get_running_loop()
        control["stop"] = stop_event
    try:
        return await run_app(stop_event, shared)
    except asyncio.CancelledError:
        stop_event.set()
        return 0


def _run_and_report(shared=None, control=None):
    """Run the app, turning any failure into a plain-language console message."""
    try:
        return asyncio.run(_amain(shared, control))
    except KeyboardInterrupt:
        logger.info(COMPONENT, "stopped")
        return 0
    except appconfig.ConfigError as exc:
        logger.error(
            COMPONENT,
            str(exc),
            next_step="put config.yaml in the same folder as kankor-bot.exe and start it again",
        )
        return 1
    except Exception as exc:
        logger.error(
            COMPONENT,
            "the app could not start",
            exc,
            next_step="the reason is written to logs\\error.log",
        )
        return 1


def _telegram_signed_in():
    """Has the Telegram account been logged in on this machine before?

    First-time login asks for a phone number and the code Telegram texts you,
    which needs a console. A background process has none, so that first run has
    to happen in a visible window.
    """
    try:
        cfg = appconfig.load()
    except Exception:
        return False
    session = cfg.get("telegram", {}).get("session_name")
    return bool(session) and os.path.exists(f"{session}.session")


def run_foreground():
    """Run the bot in this window — used for first-time Telegram login."""
    code = _run_and_report()
    if code:
        _hold_window_open()
    return code


def run_worker():
    """The background bot: no console, a tray icon, runs until told to quit."""
    if not background.claim_single_instance():
        return 0  # another copy is already running; do not double-post

    shared = {}
    control = {}
    result = {}

    def app_main():
        result["code"] = _run_and_report(shared, control)

    def request_stop():
        loop, stop_event = control.get("loop"), control.get("stop")
        if loop and stop_event:
            loop.call_soon_threadsafe(stop_event.set)

    try:
        cfg = appconfig.load()
    except Exception:
        cfg = None

    try:
        import tray

        tray.run_with_tray(app_main, shared, cfg, request_stop)
    except Exception as exc:
        # No tray icon (missing display, blocked by policy) is not a reason to
        # refuse to run — the bot still works, you just stop it via Task Manager.
        logger.error(
            COMPONENT,
            "could not show the tray icon, so the bot is running without one",
            exc,
            next_step="everything else works normally; to stop it, end kankor-bot.exe in Task Manager",
        )
        app_main()

    return result.get("code", 0)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--worker" in argv:
        return run_worker()
    if "--console" in argv or "--foreground" in argv:
        return run_foreground()

    # Plain double-click. Until Telegram has been logged in once, that login
    # needs this window; after that the bot moves to the background.
    if not _telegram_signed_in():
        print()
        print("  First run: Telegram needs to sign in.")
        print("  Enter your phone number and the code Telegram sends you.")
        print("  After this, the bot starts in the background automatically.")
        print()
        return run_foreground()

    return background.run_viewer()


def _hold_window_open():
    """A double-clicked exe closes its console the instant it exits, taking the
    error message with it. Only pause when there is a console to read."""
    if not paths.is_frozen():
        return
    try:
        input("\nPress Enter to close...")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    sys.exit(main())
