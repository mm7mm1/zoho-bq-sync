"""Drift-safe incremental checkpoints.

The list API paginates by offset over records sorted by Modified_Time. If a record is
edited while we page, it jumps to the end of the result set and every later record
shifts one position left - one record can slide from the next page onto a page we have
already read and is silently skipped. Advancing the checkpoint to max(Modified_Time)
would then lose that record forever.

Rule used here: a page is "clean" if it was served (Zoho Date header) before the first
modification that happened during extraction. Records on clean pages were read
consistently, so the checkpoint is the max Modified_Time among them. Data from later
pages is still merged (idempotently); the next run simply re-reads from the safe point.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence


@dataclass(frozen=True)
class PageMeta:
    fetched_at: datetime  # Zoho server time (Date header), local UTC as a fallback
    modified_times: tuple[datetime, ...]


@dataclass(frozen=True)
class CheckpointDecision:
    checkpoint: datetime | None
    drift_detected: bool
    concurrent_modifications: int


def compute_safe_checkpoint(
    pages: Sequence[PageMeta],
    previous: datetime | None,
    until: datetime | None = None,
) -> CheckpointDecision:
    all_times = [t for page in pages for t in page.modified_times]
    if not all_times:
        return CheckpointDecision(previous, False, 0)

    extraction_start = pages[0].fetched_at
    # ">=" because Modified_Time and the Date header have one-second resolution.
    concurrent = [t for t in all_times if t >= extraction_start]
    if concurrent:
        first_change = min(concurrent)
        clean_times = [
            t
            for page in pages
            if page.fetched_at < first_change
            for t in page.modified_times
            if t < extraction_start
        ]
        candidate = max(clean_times) if clean_times else None
    else:
        candidate = max(all_times)

    if until is not None and candidate is not None and candidate > until:
        candidate = until
    if candidate is None or (previous is not None and candidate < previous):
        candidate = previous  # a checkpoint never moves backwards
    return CheckpointDecision(candidate, bool(concurrent), len(concurrent))
