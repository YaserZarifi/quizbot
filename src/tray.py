"""The taskbar tray icon shown while the bot runs in the background.

The bot's real work happens in a process with no console window, so once you
close the log window this icon — down by the clock, often behind the little
"show hidden icons" arrow — is the only sign it is still running, and the only
way to stop it.

pystray wants the main thread on Windows, and the bot wants the asyncio event
loop, so the two swap places: the icon owns the main thread and the whole
asyncio app runs on a worker thread beside it.
"""

import os
import subprocess
import threading

import db
import logger
import paths

COMPONENT = "main"

# Palette borrowed from the question cards so the icon looks like the app.
_ICON_BG = (83, 74, 183)
_ICON_FG = (255, 255, 255)


def _make_image():
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGB", (size, size), _ICON_BG)
    draw = ImageDraw.Draw(image)
    # A "K" drawn from rectangles — no font file needed, so this can never fail
    # for want of a glyph on someone else's machine.
    draw.rectangle([14, 14, 22, 50], fill=_ICON_FG)
    draw.line([(24, 32), (46, 14)], fill=_ICON_FG, width=7)
    draw.line([(24, 32), (46, 50)], fill=_ICON_FG, width=7)
    return image


def _status_text(conn, cfg):
    try:
        counts = db.count_questions_by_status(conn)
    except Exception:
        return "Kankor Quiz Bot — running"
    return (
        "Kankor Quiz Bot — running\n"
        f"{counts.get('pending_review', 0):,} to review · "
        f"{counts.get('approved', 0):,} approved · "
        f"{counts.get('posted', 0):,} posted"
    )


def _open_folder(path):
    try:
        os.startfile(path)  # noqa: S606 - Windows shell open, path is ours
    except Exception as exc:
        logger.error(COMPONENT, "could not open that folder", exc)


def run_with_tray(app_main, conn_holder, cfg, request_stop):
    """Run app_main() on a background thread with a tray icon on the main one.

    app_main    callable that blocks until the bot shuts down
    conn_holder dict that gains a "conn" key once the database is open, so the
                status menu can report live numbers without being handed a
                connection that does not exist yet at startup
    request_stop callable that asks the bot to shut down cleanly
    """
    import pystray

    worker_error = {}

    def worker():
        try:
            app_main()
        except Exception as exc:  # pragma: no cover - defensive
            worker_error["exc"] = exc
        finally:
            icon.stop()

    def on_status(_icon, _item):
        conn = conn_holder.get("conn")
        _icon.notify(_status_text(conn, cfg) if conn else "Kankor Quiz Bot — starting up", "Kankor Quiz Bot")

    def on_open_logs(_icon, _item):
        _open_folder(cfg["paths"]["log_dir"] if cfg else paths.base_dir())

    def on_open_folder(_icon, _item):
        _open_folder(paths.base_dir())

    def on_quit(icon_, _item):
        logger.info(COMPONENT, "stopping — you asked to quit from the tray icon")
        request_stop()

    icon = pystray.Icon(
        "kankor-bot",
        _make_image(),
        "Kankor Quiz Bot — running",
        menu=pystray.Menu(
            pystray.MenuItem("Kankor Quiz Bot", on_status, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Show log folder", on_open_logs),
            pystray.MenuItem("Show app folder", on_open_folder),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        ),
    )

    thread = threading.Thread(target=worker, name="kankor-app", daemon=True)
    thread.start()
    icon.run()
    thread.join(timeout=30)

    if "exc" in worker_error:
        raise worker_error["exc"]
