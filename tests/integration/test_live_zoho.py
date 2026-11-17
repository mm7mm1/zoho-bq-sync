"""LIVE Zoho CRM test (read-only) against your developer org.

Opt-in only:  RUN_LIVE_TESTS=1 python -m pytest -m integration tests/integration/test_live_zoho.py
Uses the same ZOHO_* environment variables as the pipeline. Costs a handful of API credits.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="live tests disabled"),
]


@pytest.fixture(scope="module")
def zoho():
    from zoho_bq_sync.cli import build_zoho
    from zoho_bq_sync.config import load_settings

    return build_zoho(load_settings(require_bigquery=False))


def test_token_refresh_and_module_discovery(zoho):
    _client, metadata, _records = zoho
    names = {m.api_name for m in metadata.list_modules()}
    assert "Wedding_Bookings" in names, "create the demo module first (docs/zoho_setup.md)"


def test_subform_metadata_and_detail_fetch(zoho):
    _client, metadata, records = zoho
    fields = {f.api_name: f for f in metadata.get_fields("Wedding_Bookings")}
    subform = fields["Booked_Services"]
    assert subform.is_subform and subform.associated_module
    assert metadata.get_fields(subform.associated_module)

    first_page = next(iter(records.iter_pages("Wedding_Bookings", ["Modified_Time"])), None)
    if first_page and first_page.records:
        record = records.get_record("Wedding_Bookings", first_page.records[0]["id"])
        assert isinstance(record.get("Booked_Services"), list)
    assert records.count("Wedding_Bookings") >= 0
