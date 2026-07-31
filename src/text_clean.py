"""Watermark/link/ad stripping and dedup fingerprinting for scraped question text."""

import hashlib
import json
import re

_URL_RE = re.compile(r"(https?://\S+|t\.me/\S+|www\.\S+)", re.IGNORECASE)
_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]{3,}")
_FLAG_EMOJI_RE = re.compile(
    r"[\U0001F1E6-\U0001F1FF]{2}"  # regional indicator pairs, e.g. 🇦🇫
)
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def load_promo_phrases(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def clean_question_text(text, promo_phrases):
    """Strip @handles, URLs, flag-emoji watermarks, and known promo phrases."""
    if not text:
        return ""
    cleaned = _URL_RE.sub("", text)
    cleaned = _HANDLE_RE.sub("", cleaned)
    cleaned = _FLAG_EMOJI_RE.sub("", cleaned)
    for phrase in promo_phrases:
        cleaned = cleaned.replace(phrase, "")
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    cleaned = _BLANK_LINES_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def normalize_for_fingerprint(text):
    """Loose normalization so near-duplicate wording across channels still matches."""
    if not text:
        return ""
    t = text.strip().lower()
    t = re.sub(r"[‌‏‎]", "", t)  # ZWNJ/RTL/LTR marks
    t = re.sub(r"[^\w\s]", "", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def compute_fingerprint(question_text, option_a):
    normalized = normalize_for_fingerprint(question_text) + "|" + normalize_for_fingerprint(option_a)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_ASCII_DIGITS = "0123456789"

_TO_ASCII_TABLE = {ord(ch): d for ch, d in zip(_PERSIAN_DIGITS + _ARABIC_DIGITS, _ASCII_DIGITS * 2)}
_TO_FARSI_TABLE = {ord(ch): d for ch, d in zip(_ASCII_DIGITS, _PERSIAN_DIGITS)}


def normalize_digits(text):
    """Persian/Arabic-Indic digits -> ASCII digits, for parsing counters like [57/60]."""
    if not text:
        return text
    return text.translate(_TO_ASCII_TABLE)


def to_farsi_digits(text):
    """ASCII digits -> Persian digits, for on-card display (never applied to public_id)."""
    if not text:
        return text
    return text.translate(_TO_FARSI_TABLE)


def load_lexicon_flag_words(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("flag_words", [])


def check_lexicon_flag(text, flag_words):
    if not text:
        return False
    return any(word in text for word in flag_words)
