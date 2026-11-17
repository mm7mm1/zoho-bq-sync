"""Retry timing helpers shared by the OAuth and API clients."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable


def backoff_delay(attempt: int, base: float, cap: float, rng: Callable[[], float] = random.random) -> float:
    """Exponential backoff with "equal jitter".

    Half of the delay is fixed so a retry never fires immediately; the other half is
    random so parallel jobs hitting the same 429 do not retry in lockstep.
    """
    ceiling = min(cap, base * (2**attempt))
    return ceiling / 2 + rng() * ceiling / 2


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After may be delta-seconds or an HTTP date (RFC 9110)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())


def parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
