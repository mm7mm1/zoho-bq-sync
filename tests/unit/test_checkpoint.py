from datetime import datetime, timedelta, timezone

from zoho_bq_sync.sync.checkpoint import PageMeta, compute_safe_checkpoint
from zoho_bq_sync.sync.engine import decide_mode
from zoho_bq_sync.warehouse.base import SyncState

UTC = timezone.utc
T0 = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)


def t(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_no_drift_advances_to_max_modified_time():
    pages = [PageMeta(t(0), (t(-50), t(-40))), PageMeta(t(0.1), (t(-30), t(-20)))]
    decision = compute_safe_checkpoint(pages, previous=t(-60))
    assert decision.checkpoint == t(-20) and not decision.drift_detected


def test_drift_limits_checkpoint_to_last_clean_page():
    # Page 3 contains a record edited at t(0.15), i.e. after extraction started at t(0).
    # Pages served before that edit are clean; page 3 (served at t(0.2)) may have shifted.
    pages = [
        PageMeta(t(0.0), (t(-50), t(-45))),
        PageMeta(t(0.1), (t(-40), t(-35))),
        PageMeta(t(0.2), (t(-30), t(0.15))),
    ]
    decision = compute_safe_checkpoint(pages, previous=t(-60))
    assert decision.drift_detected and decision.concurrent_modifications == 1
    assert decision.checkpoint == t(-35)


def test_drift_on_first_page_keeps_previous_checkpoint():
    pages = [PageMeta(t(1), (t(-10), t(1)))]
    decision = compute_safe_checkpoint(pages, previous=t(-60))
    assert decision.drift_detected and decision.checkpoint == t(-60)


def test_until_caps_checkpoint_and_never_moves_backwards():
    pages = [PageMeta(t(0), (t(-50), t(-10)))]
    assert compute_safe_checkpoint(pages, previous=None, until=t(-30)).checkpoint == t(-30)
    assert compute_safe_checkpoint(pages, previous=t(-5)).checkpoint == t(-5)


def test_empty_extraction_keeps_previous():
    assert compute_safe_checkpoint([], previous=t(-60)).checkpoint == t(-60)
    assert compute_safe_checkpoint([PageMeta(t(0), ())], previous=None).checkpoint is None


def test_mode_decision():
    state = SyncState("M", "m", last_modified_time=t(-60))
    kw = dict(new_columns=[], backfill_on_new_fields=False, has_version_field=True)
    assert decide_mode(full_requested=False, window_requested=False, state=None, **kw)[0] == "full"
    assert decide_mode(full_requested=False, window_requested=False, state=state, **kw)[0] == "incremental"
    assert decide_mode(full_requested=True, window_requested=False, state=state, **kw)[0] == "full"
    assert decide_mode(full_requested=False, window_requested=True, state=state, **kw)[0] == "window"
    assert decide_mode(full_requested=False, window_requested=False, state=state, new_columns=["t.c"], backfill_on_new_fields=True, has_version_field=True)[0] == "full"
    assert decide_mode(full_requested=False, window_requested=False, state=state, new_columns=[], backfill_on_new_fields=False, has_version_field=False)[0] == "full"
