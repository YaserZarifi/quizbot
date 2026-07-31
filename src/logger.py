"""Shared console logger. Everything the app says goes through here so the
window reads like a status report rather than programmer output.

Rules this module exists to enforce:
  * Plain language. No tracebacks, no exception class names, no jargon on
    screen — those go to logs/error.log, which is there for debugging.
  * Every line says which part of the app is talking, in words ("Scraper",
    "Facebook"), not module names.
  * Errors always say what failed AND what happens next, so the reader is
    never left wondering whether the app is stuck or recovering.
"""

import logging
import logging.handlers
import os
import sys
import time
import traceback

# Windows consoles default to a legacy codepage, so printing Persian/Pashto
# question text would raise UnicodeEncodeError and kill the process.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        # line_buffering keeps progress visible when output is redirected to a file.
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# Friendly names for the parts of the app, so the console never shows a
# module name. Anything not listed here prints as-is.
COMPONENT_NAMES = {
    "main": "Startup",
    "scheduler": "Scheduler",
    "ingest": "Scraper",
    "review": "Reviewer",
    "publish": "Facebook",
    "render": "Images",
}

_MARKERS = {
    "info": "  ",
    "ok": "OK",
    "warn": "! ",
    "error": "XX",
    "setup": "..",
}

_NAME_WIDTH = 9  # "Scheduler" is the longest friendly name


def _log_dir():
    return os.environ.get("KANKOR_LOG_DIR", "logs")


def _error_log_path():
    d = _log_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "error.log")


_file_logger = None


def _get_file_logger():
    global _file_logger
    if _file_logger is not None:
        return _file_logger
    fl = logging.getLogger("kankor.error_log")
    fl.setLevel(logging.ERROR)
    fl.propagate = False
    if not fl.handlers:
        handler = logging.handlers.RotatingFileHandler(
            _error_log_path(), maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        fl.addHandler(handler)
    _file_logger = fl
    return fl


def _friendly(component):
    return COMPONENT_NAMES.get(component, component)


def activity_log_path():
    return os.path.join(_log_dir(), "activity.log")


_activity_file = None
_activity_failed = False


def _mirror(text):
    """Append a line to activity.log.

    When the app runs in the background it has no console at all, so this file
    is the only record of what it is doing — and it is what the console window
    reads back when you reopen it. A failure to write here must never take the
    app down, so it is tried once and then left alone.
    """
    global _activity_file, _activity_failed
    if _activity_failed:
        return
    try:
        if _activity_file is None:
            os.makedirs(_log_dir(), exist_ok=True)
            _activity_file = open(activity_log_path(), "a", encoding="utf-8")
        _activity_file.write(text + "\n")
        _activity_file.flush()
    except Exception:
        _activity_failed = True


def _emit(text):
    print(text)
    _mirror(text)


def _line(marker, component, message):
    ts = time.strftime("%H:%M:%S")
    name = _friendly(component)
    _emit(f"{ts}  {marker}  {name:<{_NAME_WIDTH}}  {message}")


def info(component, message):
    _line(_MARKERS["info"], component, message)


def ok(component, message):
    _line(_MARKERS["ok"], component, message)


def warn(component, message):
    _line(_MARKERS["warn"], component, message)


def setup(component, message):
    _line(_MARKERS["setup"], component, message)


def detail(component, message):
    """An indented follow-up line under whatever was just printed."""
    _line("  ", component, f"    {message}")


def section(title):
    """A labelled divider, so long runs of output stay scannable."""
    _emit("")
    _emit(f"  {'-' * 66}")
    _emit(f"  {title}")
    _emit(f"  {'-' * 66}")


def summary(component, title, rows):
    """Print an indented multi-fact block under a header line.

    rows: list of (label, value) pairs. Labels are left-aligned.
    """
    ok(component, title)
    if not rows:
        return
    label_width = max(len(label) for label, _ in rows)
    for label, value in rows:
        detail(component, f"{label:<{label_width}} : {value}")


def error(component, message, exc=None, next_step=None):
    """Short, plain-language line on screen; full technical detail to error.log.

    next_step tells the reader what the app is doing about it — without that,
    an error line reads as "everything stopped" even when it didn't.
    """
    _line(_MARKERS["error"], component, message)
    if next_step:
        detail(component, f"-> {next_step}")

    written = message
    if exc is not None:
        written += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _get_file_logger().error("[%s] %s", component, written)


def blank():
    _emit("")
