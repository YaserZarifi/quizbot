"""Shared structured console logger. Every script prints through here so
formatting stays identical everywhere (see kankor_quiz_bot_spec.md, §2.1).

Console gets short, friendly lines only. Full tracebacks go to logs/error.log.
"""

import logging
import logging.handlers
import os
import sys
import time
import traceback

_LEVEL_WIDTH = 5  # "ERROR" is the longest level label

# Windows consoles default to a legacy codepage, so printing Persian/Pashto
# question text would raise UnicodeEncodeError and kill the process.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


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


def _line(level, component, message):
    ts = time.strftime("%H:%M:%S")
    print(f"{ts} [{level:<{_LEVEL_WIDTH}}] [{component}] {message}")


def info(component, message):
    _line("INFO", component, message)


def ok(component, message):
    _line("OK", component, message)


def warn(component, message):
    _line("WARN", component, message)


def setup(component, message):
    _line("SETUP", component, message)


def summary(component, title, rows):
    """Print an indented multi-fact block under a header line.

    rows: list of (label, value) pairs. Labels are left-aligned.
    """
    ok(component, title)
    if not rows:
        return
    label_width = max(len(label) for label, _ in rows)
    for label, value in rows:
        ts = time.strftime("%H:%M:%S")
        print(f"{ts} [{'OK':<{_LEVEL_WIDTH}}] [{component}]   {label:<{label_width}} : {value}")


def error(component, message, exc=None):
    """Print a short friendly line to console; log full detail to error.log."""
    _line("ERROR", component, message)
    detail = message
    if exc is not None:
        detail += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _get_file_logger().error("[%s] %s", component, detail)


def blank():
    print()
