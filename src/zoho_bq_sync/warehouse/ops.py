"""Operational metadata tables (live next to the replicated tables)."""

from __future__ import annotations

from ..schema.model import BOOL, INT64, REPEATED, REQUIRED, STRING, TIMESTAMP, Column

STATE_TABLE = "_sync_state"
RUNS_TABLE = "_sync_runs"
RECONCILIATION_TABLE = "_reconciliation"

STATE_COLUMNS: tuple[Column, ...] = (
    Column("module", STRING, REQUIRED, description="Zoho module api name"),
    Column("table_name", STRING),
    Column("last_modified_time", TIMESTAMP, description="Incremental checkpoint (Zoho Modified_Time)"),
    Column("last_deleted_check", TIMESTAMP, description="Zoho server time of the last deleted-records poll"),
    Column("last_full_sync_at", TIMESTAMP),
    Column("last_run_id", STRING),
    Column("updated_at", TIMESTAMP, description="Last successful commit"),
)

RUNS_COLUMNS: tuple[Column, ...] = (
    Column("run_id", STRING, REQUIRED),
    Column("module", STRING, REQUIRED),
    Column("mode", STRING),
    Column("mode_reason", STRING),
    Column("status", STRING),
    Column("started_at", TIMESTAMP),
    Column("finished_at", TIMESTAMP),
    Column("records_extracted", INT64),
    Column("parent_rows", INT64),
    Column("child_rows", INT64),
    Column("deleted_records", INT64),
    Column("rows_inserted", INT64),
    Column("rows_updated", INT64),
    Column("new_columns", STRING, REPEATED),
    Column("widened_columns", STRING, REPEATED),
    Column("drift_detected", BOOL),
    Column("checkpoint_before", TIMESTAMP),
    Column("checkpoint_after", TIMESTAMP),
    Column("api_calls", INT64),
    Column("error", STRING),
)

RECONCILIATION_COLUMNS: tuple[Column, ...] = (
    Column("run_id", STRING, REQUIRED),
    Column("module", STRING, REQUIRED),
    Column("table_name", STRING),
    Column("check_name", STRING),
    Column("expected", INT64),
    Column("actual", INT64),
    Column("difference", INT64),
    Column("status", STRING),
    Column("details", STRING),
    Column("checked_at", TIMESTAMP),
)

OPS_TABLES = {
    STATE_TABLE: STATE_COLUMNS,
    RUNS_TABLE: RUNS_COLUMNS,
    RECONCILIATION_TABLE: RECONCILIATION_COLUMNS,
}
