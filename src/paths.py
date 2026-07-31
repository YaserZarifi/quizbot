"""Where the app's files live, in both a frozen .exe and a source checkout.

Two different roots matter and they are not the same directory:

  base_dir()   — writable app folder. The folder holding kankor-bot.exe when
                 frozen, the project root when running from source. Database,
                 logs, rendered images and the Telegram session file go here.

  resource()   — read-only bundled assets (fonts, themes, word lists). Under
                 PyInstaller these are unpacked to a temp folder (sys._MEIPASS)
                 that is deleted on exit, so they can never be written to.
                 A same-named file next to the exe wins, which is what lets you
                 tweak themes.json without rebuilding.
"""

import os
import sys


def is_frozen():
    return getattr(sys, "frozen", False)


def base_dir():
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundle_dir():
    """Where --add-data files were unpacked. Same as base_dir() from source."""
    return getattr(sys, "_MEIPASS", None) or base_dir()


def resource(rel_path):
    """Resolve a bundled asset, preferring an override next to the exe."""
    if os.path.isabs(rel_path):
        return rel_path
    override = os.path.join(base_dir(), rel_path)
    if os.path.exists(override):
        return override
    return os.path.join(bundle_dir(), rel_path)


def writable(rel_path):
    """Resolve a path the app writes to — always next to the exe."""
    if os.path.isabs(rel_path):
        return rel_path
    return os.path.join(base_dir(), rel_path)
