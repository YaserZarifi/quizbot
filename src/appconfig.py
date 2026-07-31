"""Single place config.yaml is loaded and its relative paths are resolved.

Every module used to carry its own load_config(), which meant the answer to
"where is kankor.db?" depended on which script you happened to launch and from
which directory. Now there is one answer: next to the exe.

Asset paths resolve through paths.resource() (bundled, read-only, overridable);
everything the app writes resolves through paths.writable().
"""

import os

import yaml

import paths

ASSET_KEYS = (
    "fonts_dir",
    "themes_file",
    "strings_file",
    "promo_phrases_file",
    "lexicon_file",
    "subject_positions_file",
    "subject_keywords_file",
)

WRITABLE_KEYS = ("db_file", "output_dir", "log_dir", "question_images_dir")

DEFAULTS = {"question_images_dir": "question_images"}


class ConfigError(Exception):
    """Config is missing or unusable — reported to the user, not a traceback."""


def load(path="config.yaml"):
    config_path = paths.writable(path)
    if not os.path.exists(config_path):
        raise ConfigError(f"config.yaml not found at {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ConfigError(f"{config_path} is empty or not valid YAML")

    cfg.setdefault("paths", {})
    for key, default in DEFAULTS.items():
        cfg["paths"].setdefault(key, default)

    for key in ASSET_KEYS:
        if cfg["paths"].get(key):
            cfg["paths"][key] = paths.resource(cfg["paths"][key])
    for key in WRITABLE_KEYS:
        if cfg["paths"].get(key):
            cfg["paths"][key] = paths.writable(cfg["paths"][key])

    # Telethon writes <session_name>.session — keep it beside the exe too.
    tg = cfg.setdefault("telegram", {})
    if tg.get("session_name"):
        tg["session_name"] = paths.writable(tg["session_name"])

    os.environ.setdefault("KANKOR_LOG_DIR", cfg["paths"]["log_dir"])
    return cfg


def missing_assets(cfg):
    """Asset files the app cannot start without, so one clear error beats a
    FileNotFoundError surfacing hours later mid-render."""
    required = [cfg["paths"][k] for k in ASSET_KEYS if cfg["paths"].get(k)]
    required += [
        os.path.join(cfg["paths"]["fonts_dir"], name)
        for name in ("Vazirmatn-Regular.ttf", "Vazirmatn-Medium.ttf", "NotoNaskhArabic-Regular.ttf")
    ]
    return [p for p in required if not os.path.exists(p)]
