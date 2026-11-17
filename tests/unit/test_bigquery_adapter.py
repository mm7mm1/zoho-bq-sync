"""BigQuery adapter wiring with a mocked google-cloud-bigquery client (no GCP calls)."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.api_core import exceptions as gexc
from google.cloud import bigquery

from zoho_bq_sync.config import BigQuerySettings
from zoho_bq_sync.errors import SafetyCheckError
from zoho_bq_sync.schema.model import Column, TableSpec
from zoho_bq_sync.warehouse.base import ApplyBatch, ChildBatch, SyncState
from zoho_bq_sync.warehouse.bigquery import BigQueryWarehouse

SETTINGS = BigQuerySettings(project="demo-proj", dataset="zoho_crm", staging_dataset="zoho_crm_staging", location="EU")
PARENT = TableSpec("wedding_bookings", "Wedding_Bookings", (Column("id", "STRING", "REQUIRED"), Column("modified_time", "TIMESTAMP"), Column("_is_deleted", "BOOL")), version_column="modified_time")
CHILD = TableSpec("wedding_bookings_services", "Booked_Services", (Column("id", "STRING", "REQUIRED"), Column("parent_id", "STRING", "REQUIRED"), Column("_parent_modified_time", "TIMESTAMP")), version_column="_parent_modified_time", parent_key="parent_id", subform_field="Booked_Services")
RUN_TS = datetime(2026, 11, 17, 10, tzinfo=timezone.utc)


def batch(**kw):
    defaults = dict(
        module="Wedding_Bookings",
        run_id="r20261117T100000Z_abc123",
        run_ts=RUN_TS,
        mode="incremental",
        parent_spec=PARENT,
        parent_rows=[{"id": "1", "modified_time": "2026-11-17T09:00:00+00:00", "_is_deleted": False}],
        children=[ChildBatch(CHILD, [{"id": "s1", "parent_id": "1", "_parent_modified_time": "2026-11-17T09:00:00+00:00"}])],
        deleted=[],
        soft_delete_missing=False,
        max_delete_fraction=0.5,
        new_state=SyncState("Wedding_Bookings", "wedding_bookings", last_modified_time=RUN_TS),
    )
    defaults.update(kw)
    return ApplyBatch(**defaults)


def make_client(guard_rows=None):
    client = MagicMock()
    script_job = MagicMock(job_id="job-1")

    def query(sql, job_config=None):
        job = MagicMock(job_id="q")
        if sql.startswith("SELECT COUNT(*) AS active_rows"):
            job.result.return_value = guard_rows or [{"active_rows": 0, "would_delete": 0}]
            return job
        client.scripts.append((sql, job_config))
        return script_job

    client.scripts = []
    client.query.side_effect = query
    client.list_jobs.return_value = [
        SimpleNamespace(statement_type="MERGE", dml_stats=SimpleNamespace(inserted_row_count=1, updated_row_count=2, deleted_row_count=0), query="MERGE x"),
    ]
    return client


def test_apply_batch_stages_with_load_jobs_and_runs_one_transaction():
    client = make_client()
    wh = BigQueryWarehouse(SETTINGS, client=client, sleep=lambda s: None)
    stats = wh.apply_batch(batch())

    created = [c.args[0].table_id for c in client.create_table.call_args_list]
    assert created == ["wedding_bookings__r20261117T100000Z_abc123", "wedding_bookings_services__r20261117T100000Z_abc123"]
    load_targets = [c.args[1] for c in client.load_table_from_json.call_args_list]
    assert load_targets == [
        "demo-proj.zoho_crm_staging.wedding_bookings__r20261117T100000Z_abc123",
        "demo-proj.zoho_crm_staging.wedding_bookings_services__r20261117T100000Z_abc123",
    ]
    (script, config), = client.scripts
    assert script.startswith("BEGIN\nBEGIN TRANSACTION;") and "MERGE `demo-proj.zoho_crm.wedding_bookings` AS T" in script
    params = {p.name: p for p in config.query_parameters}
    assert params["run_id"].value == "r20261117T100000Z_abc123" and params["checkpoint"].type_ == "TIMESTAMP"
    assert stats.summary() == {"inserted": 1, "updated": 2, "deleted": 0}
    assert client.delete_table.call_count == 2  # staging dropped after commit


def test_full_resync_guard_blocks_mass_delete_before_transaction():
    client = make_client(guard_rows=[{"active_rows": 10, "would_delete": 9}])
    wh = BigQueryWarehouse(SETTINGS, client=client)
    with pytest.raises(SafetyCheckError):
        wh.apply_batch(batch(mode="full", soft_delete_missing=True))
    assert client.scripts == [] and client.delete_table.call_count == 0  # staging kept for debugging


def test_concurrent_update_is_retried():
    client = make_client()
    calls = {"n": 0}
    original = client.query.side_effect

    def flaky(sql, job_config=None):
        if sql.startswith("BEGIN") and calls["n"] == 0:
            calls["n"] += 1
            raise gexc.BadRequest("Transaction is aborted due to concurrent update against table x")
        return original(sql, job_config)

    client.query.side_effect = flaky
    sleeps = []
    BigQueryWarehouse(SETTINGS, client=client, sleep=sleeps.append).apply_batch(batch())
    assert sleeps == [5] and len(client.scripts) == 1


def test_get_table_columns_normalizes_legacy_types():
    client = MagicMock()
    client.get_table.return_value = SimpleNamespace(schema=[bigquery.SchemaField("id", "STRING", mode="REQUIRED"), bigquery.SchemaField("guests", "INTEGER"), bigquery.SchemaField("ok", "BOOLEAN")])
    cols = BigQueryWarehouse(SETTINGS, client=client).get_table_columns("wedding_bookings")
    assert [(c.name, c.type, c.mode) for c in cols] == [("id", "STRING", "REQUIRED"), ("guests", "INT64", "NULLABLE"), ("ok", "BOOL", "NULLABLE")]


def test_missing_table_returns_none():
    client = MagicMock()
    client.get_table.side_effect = gexc.NotFound("nope")
    assert BigQueryWarehouse(SETTINGS, client=client).get_table_columns("x") is None


def test_add_columns_appends_nullable_fields():
    client = MagicMock()
    table = SimpleNamespace(schema=[bigquery.SchemaField("id", "STRING", mode="REQUIRED")])
    client.get_table.return_value = table
    BigQueryWarehouse(SETTINGS, client=client).add_columns("wedding_bookings", [Column("apostille_deadline", "DATE", "REQUIRED")])
    assert [(f.name, f.field_type, f.mode) for f in table.schema] == [("id", "STRING", "REQUIRED"), ("apostille_deadline", "DATE", "NULLABLE")]
    client.update_table.assert_called_once_with(table, ["schema"])
