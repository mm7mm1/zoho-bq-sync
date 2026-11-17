"""LIVE BigQuery test: executes the generated MERGE/transaction SQL for real.

Opt-in only:  RUN_LIVE_TESTS=1 BQ_TEST_PROJECT=<project> python -m pytest -m integration
Creates a throwaway dataset zoho_bq_sync_it_<random> (+ _staging) and deletes it afterwards.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (os.getenv("RUN_LIVE_TESTS") == "1" and os.getenv("BQ_TEST_PROJECT")), reason="live tests disabled"),
]

from zoho_bq_sync.config import BigQuerySettings  # noqa: E402
from zoho_bq_sync.schema.builder import build_module_plan  # noqa: E402
from zoho_bq_sync.schema.evolution import plan_schema_change  # noqa: E402
from zoho_bq_sync.transform.normalize import RunContext, to_child_rows, to_parent_row  # noqa: E402
from zoho_bq_sync.warehouse.base import ApplyBatch, ChildBatch, SyncState  # noqa: E402
from zoho_bq_sync.zoho.metadata import ZohoField  # noqa: E402

UTC = timezone.utc


def f(api, dt, fid, **kw):
    return ZohoField(api_name=api, data_type=dt, field_id=fid, **kw)


FIELDS = [f("Name", "text", "1"), f("Modified_Time", "datetime", "2"), f("Booked_Services", "subform", "3", associated_module="Booked_Services")]
SUB = {"Booked_Services": [f("Service_Name", "text", "10"), f("Unit_Price", "currency", "11")]}


@pytest.fixture(scope="module")
def warehouse():
    from google.cloud import bigquery

    from zoho_bq_sync.warehouse.bigquery import BigQueryWarehouse

    suffix = secrets.token_hex(4)
    settings = BigQuerySettings(
        project=os.environ["BQ_TEST_PROJECT"],
        dataset=f"zoho_bq_sync_it_{suffix}",
        staging_dataset=f"zoho_bq_sync_it_{suffix}_staging",
        location=os.getenv("BQ_TEST_LOCATION", "EU"),
    )
    wh = BigQueryWarehouse(settings)
    wh.ensure_datasets()
    wh.ensure_ops_tables()
    yield wh
    client = bigquery.Client(project=settings.project)
    for ds in (settings.dataset, settings.staging_dataset):
        client.delete_dataset(f"{settings.project}.{ds}", delete_contents=True, not_found_ok=True)


def _apply(wh, plan, records, run_no, *, deleted=(), checkpoint=None, mode="incremental"):
    ts = datetime(2026, 11, 17, 10, tzinfo=UTC) + timedelta(minutes=run_no)
    ctx = RunContext(f"r20261117T10000{run_no}Z_it", ts)
    parent_rows = [to_parent_row(r, plan.parent, ctx, ["Booked_Services"]) for r in records]
    child_rows = [row for r, p in zip(records, parent_rows) for row in to_child_rows(r, plan.children[0], ctx, p["modified_time"])]
    return wh.apply_batch(
        ApplyBatch(
            module="Wedding_Bookings",
            run_id=ctx.run_id,
            run_ts=ts,
            mode=mode,
            parent_spec=plan.parent,
            parent_rows=parent_rows,
            children=[ChildBatch(plan.children[0], child_rows)],
            deleted=list(deleted),
            soft_delete_missing=False,
            max_delete_fraction=0.5,
            new_state=SyncState("Wedding_Bookings", plan.parent.name, last_modified_time=checkpoint or ts),
        )
    )


def _rows(wh, table):
    sql = f"SELECT * FROM `{wh.settings.project}.{wh.settings.dataset}.{table}` ORDER BY id"
    return {r["id"]: dict(r.items()) for r in wh.client.query(sql).result()}


def test_merge_transaction_child_reconciliation_and_deletes(warehouse):
    plan = build_module_plan("Wedding_Bookings", FIELDS, SUB)
    for spec in (plan.parent, *plan.children):
        warehouse.create_table(plan_schema_change(spec, warehouse.get_table_columns(spec.name)).effective)

    rec = {
        "id": "1",
        "Name": "WB-SYN-IT",
        "Modified_Time": "2026-11-17T09:00:00+00:00",
        "Booked_Services": [{"id": "s1", "Service_Name": "Venue", "Unit_Price": 900}, {"id": "s2", "Service_Name": "Photo", "Unit_Price": 700.5}],
    }
    _apply(warehouse, plan, [rec], 1)
    assert set(_rows(warehouse, "wedding_bookings_booked_services")) == {"s1", "s2"}

    # Same version replayed -> idempotent; then a newer version without s2.
    _apply(warehouse, plan, [rec], 2)
    newer = {**rec, "Modified_Time": "2026-11-17T09:05:00+00:00", "Booked_Services": [rec["Booked_Services"][0]]}
    _apply(warehouse, plan, [newer], 3)
    children = _rows(warehouse, "wedding_bookings_booked_services")
    assert children["s2"]["_is_deleted"] is True and children["s1"]["_is_deleted"] is False

    # Stale version (a late concurrent run) must neither overwrite the newer row nor revive s2.
    _apply(warehouse, plan, [{**rec, "Name": "STALE"}], 4)
    assert _rows(warehouse, "wedding_bookings")["1"]["name"] == "WB-SYN-IT"
    assert _rows(warehouse, "wedding_bookings_booked_services")["s2"]["_is_deleted"] is True

    _apply(warehouse, plan, [], 5, deleted=[{"id": "1", "deleted_time": "2026-11-17T09:10:00+00:00", "deleted_type": "recycle"}])
    assert _rows(warehouse, "wedding_bookings")["1"]["_is_deleted"] is True
    assert _rows(warehouse, "wedding_bookings_booked_services")["s1"]["_is_deleted"] is True
    assert warehouse.load_state("Wedding_Bookings").last_modified_time is not None
