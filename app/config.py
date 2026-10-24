"""App-wide constants and small input-normalising helpers shared by the routes."""
from __future__ import annotations

import re

SESSION_DAYS = 30
MAX_INTERVAL_HOURS = 24 * 90
MAX_FINALIZE_MIN = 240
REF_RE = re.compile(r"^[A-Za-z0-9_-]{5,}$")  # a YouTube Music playlist id


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def clamp_interval(hours: int) -> int:
    return clamp(hours, 1, MAX_INTERVAL_HOURS)


def clean_url(u: str) -> str:
    return (u or "").strip().rstrip("/")
