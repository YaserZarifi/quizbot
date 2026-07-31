"""Lets the bot keep running after you close its window.

Closing a console window on Windows kills the program inside it, and no amount
of handling inside that program reliably prevents it — Windows terminates the
process a few seconds later regardless. So the bot never lives in the console
window you open. Instead:

    kankor-bot.exe                 the window you double-click. It starts the
                                   worker if it isn't already running, then
                                   just shows you what the worker is doing.
                                   Closing it closes a viewer, nothing more.

    kankor-bot.exe --worker        the real bot. No console window, a tray icon
                                   by the clock, and it keeps running until you
                                   choose Quit from that icon.

Double-clicking the exe again while the worker is running simply reattaches the
log view — the single-instance lock below makes sure you never get two bots
posting the same questions twice.
"""

import ctypes
import os
import subprocess
import sys
import time

import paths

MUTEX_NAME = "Global\\KankorQuizBot_SingleInstance"
_ERROR_ALREADY_EXISTS = 183

# Windows process creation flags (from winbase.h).
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200

_mutex_handle = None


def claim_single_instance():
    """True if this process is now the one and only worker.

    The handle is deliberately kept in a module global: the lock lasts exactly
    as long as the process holds it, and Windows releases it automatically if
    the process dies, so a crashed bot never leaves a stale lock behind.
    """
    global _mutex_handle
    kernel32 = ctypes.windll.kernel32
    _mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return kernel32.GetLastError() != _ERROR_ALREADY_EXISTS


def worker_is_running():
    """Check for a running worker without claiming the lock ourselves."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    already = kernel32.GetLastError() == _ERROR_ALREADY_EXISTS
    if handle:
        kernel32.CloseHandle(handle)
    return already


def _worker_command():
    if paths.is_frozen():
        return [sys.executable, "--worker"]
    return [sys.executable, os.path.abspath(sys.argv[0]), "--worker"]


def start_worker():
    """Launch the bot as a detached, windowless background process."""
    subprocess.Popen(
        _worker_command(),
        cwd=paths.base_dir(),
        creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _print_header(started_worker):
    print()
    print("  " + "-" * 66)
    print("  Kankor Quiz Bot")
    print("  " + "-" * 66)
    if started_worker:
        print("  Started. The bot is now running in the background.")
    else:
        print("  The bot is already running in the background.")
    print()
    print("  This window only SHOWS what the bot is doing.")
    print("  Closing it (X) does NOT stop the bot.")
    print()
    print("  To stop the bot: right-click the tray icon by the clock")
    print("  (under the ^ arrow next to the date) and choose Quit.")
    print("  " + "-" * 66)
    print()


def follow_log(log_path, from_start_lines=40):
    """Show the worker's activity live, like `tail -f`."""
    deadline = time.time() + 30
    while not os.path.exists(log_path) and time.time() < deadline:
        time.sleep(0.3)

    if not os.path.exists(log_path):
        print("  (waiting for the bot to start writing its log...)")
        print(f"  If nothing appears, check {os.path.join(os.path.dirname(log_path), 'error.log')}")
        return

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        tail = f.readlines()[-from_start_lines:]
        for line in tail:
            print(line.rstrip("\n"))
        f.seek(0, os.SEEK_END)

        while True:
            line = f.readline()
            if line:
                print(line.rstrip("\n"))
            else:
                time.sleep(0.4)


def run_viewer():
    """The double-clicked window: make sure the bot runs, then watch it."""
    started = False
    if not worker_is_running():
        start_worker()
        started = True

    _print_header(started)

    import logger

    try:
        follow_log(logger.activity_log_path())
    except KeyboardInterrupt:
        print()
        print("  Closing this window. The bot keeps running in the background.")
    return 0
