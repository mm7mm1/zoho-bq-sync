"""BigQuery implementation of the warehouse contract.

Write path per module:
  1. load each batch into a per-run staging table with a LOAD job (not streaming: rows in
     the streaming buffer cannot be touched by DML for up to ~30 minutes, and load jobs
     are free and atomic);
  2. run all MERGE/UPDATE statements plus the checkpoint MERGE in one multi-statement
     transaction;
  3. drop staging tables on success (kept on failure for debugging; they expire anyway).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from google.api_core import exceptions as gexc
from google.cloud import bigquery

from ..config import BigQuerySettings
from ..errors import SafetyCheckError
from ..logging_setup import log_event
from ..schema.evolution import ExistingColumn, canonical_type
from ..schema.model import NULLABLE, REQUIRED, Column, TableSpec
from ..schema.naming import assert_safe_identifier
from . import sql
from .base import DELETED_COLUMNS, ApplyBatch, ApplyStats, SyncState
from .ops import OPS_TABLES, RECONCILIATION_COLUMNS, RECONCILIATION_TABLE, RUNS_COLUMNS, RUNS_TABLE, STATE_TABLE

logger = logging.getLogger(__name__)

LABELS = {"managed_by": "zoho-bq-sync"}
CONCURRENT_UPDATE_MARKERS = ("concurrent update", "could not serialize access")


def to_schema_field(column: Column, *, force_nullable: bool = False) -> bigquery.SchemaField:
    mode = NULLABLE if force_nullable and column.mode == REQUIRED else column.mode
    return bigquery.SchemaField(column.name, column.type, mode=mode, description=column.description)


def to_schema(columns: Sequence[Column]) -> list[bigquery.SchemaField]:
    return [to_schema_field(c) for c in columns]


class BigQueryWarehouse:
    def __init__(
        self,
        settings: BigQuerySettings,
        client: Any = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_transaction_retries: int = 3,
    ) -> None:
        self.settings = settings
        self.client = client or bigquery.Client(project=settings.project, location=settings.location)
        self._sleep = sleep
        self._max_tx_retries = max_transaction_retries

    # -- references ---------------------------------------------------------------------
    def _table_id(self, table: str, dataset: str | None = None) -> str:
        return f"{self.settings.project}.{dataset or self.settings.dataset}.{assert_safe_identifier(table)}"

    def _ref(self, table: str, dataset: str | None = None) -> str:
        return sql.table_ref(self.settings.project, dataset or self.settings.dataset, table)

    # -- datasets and tables ------------------------------------------------------------
    def ensure_datasets(self) -> None:
        targets = [
            (self.settings.dataset, None),
            (self.settings.staging_dataset, self.settings.staging_expiration_hours),
        ]
        for dataset_id, expiration_hours in targets:
            ref = f"{self.settings.project}.{dataset_id}"
            try:
                self.client.get_dataset(ref)
                continue
            except gexc.NotFound:
                pass
            dataset = bigquery.Dataset(ref)
            dataset.location = self.settings.location
            dataset.labels = dict(LABELS)
            if expiration_hours:
                dataset.default_table_expiration_ms = expiration_hours * 3600 * 1000
            self.client.create_dataset(dataset, exists_ok=True)
            log_event(logger, "bq_dataset_created", dataset=ref, location=self.settings.location)

    def ensure_ops_tables(self) -> None:
        for name, columns in OPS_TABLES.items():
            existing = self.get_table_columns(name)
            if existing is None:
                self._create(name, columns, clustering=("module",), description="zoho-bq-sync operational metadata")
                continue
            names = {c.name.lower() for c in existing}
            missing = [c for c in columns if c.name.lower() not in names]
            if missing:
                self.add_columns(name, missing)

    def get_table_columns(self, table: str) -> list[ExistingColumn] | None:
        try:
            bq_table = self.client.get_table(self._table_id(table))
        except gexc.NotFound:
            return None
        return [ExistingColumn(f.name, canonical_type(f.field_type), (f.mode or NULLABLE).upper()) for f in bq_table.schema]

    def _create(self, name: str, columns: Sequence[Column], *, clustering: Sequence[str], description: str) -> None:
        table = bigquery.Table(self._table_id(name), schema=to_schema(columns))
        table.clustering_fields = list(clustering)[:4] or None
        table.description = description
        table.labels = dict(LABELS)
        self.client.create_table(table)
        log_event(logger, "bq_table_created", table=name, columns=len(columns))

    def create_table(self, spec: TableSpec) -> None:
        self._create(
            spec.name,
            spec.columns,
            clustering=spec.clustering,
            description=f"Replica of Zoho CRM {'subform' if spec.parent_key else 'module'} {spec.zoho_module}",
        )

    def add_columns(self, table: str, columns: Sequence[Column]) -> None:
        bq_table = self.client.get_table(self._table_id(table))
        existing = {f.name.lower() for f in bq_table.schema}
        new_fields = [to_schema_field(c, force_nullable=True) for c in columns if c.name.lower() not in existing]
        if not new_fields:
            return
        bq_table.schema = list(bq_table.schema) + new_fields
        # update_table sends the table ETag, so a concurrent schema change fails instead of being overwritten.
        self.client.update_table(bq_table, ["schema"])
        log_event(logger, "bq_columns_added", table=table, columns=[f"{f.name}:{f.field_type}" for f in new_fields])

    def widen_columns(self, table: str, changes: Sequence[tuple[str, str, str]]) -> None:
        for column, old_type, new_type in changes:
            ddl = f"ALTER TABLE {self._ref(table)} ALTER COLUMN {sql.q(column)} SET DATA TYPE {assert_safe_identifier(new_type)}"
            self.client.query(ddl, job_config=bigquery.QueryJobConfig(labels=LABELS)).result()
            log_event(logger, "bq_column_widened", table=table, column=column, from_type=old_type, to_type=new_type)

    # -- state --------------------------------------------------------------------------
    def load_state(self, module: str) -> SyncState | None:
        query = f"SELECT * FROM {self._ref(STATE_TABLE)} WHERE module = @module"
        config = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("module", "STRING", module)])
        try:
            rows = list(self.client.query(query, job_config=config).result())
        except gexc.NotFound:
            return None
        return _state_from_row(dict(rows[0].items())) if rows else None

    def read_states(self) -> list[SyncState]:
        try:
            rows = self.client.query(f"SELECT * FROM {self._ref(STATE_TABLE)} ORDER BY module").result()
        except gexc.NotFound:
            return []
        return [_state_from_row(dict(r.items())) for r in rows]

    # -- write path ---------------------------------------------------------------------
    def _stage(self, name: str, columns: Sequence[Column], rows: Sequence[dict[str, Any]]) -> str:
        table_id = self._table_id(name, self.settings.staging_dataset)
        table = bigquery.Table(table_id, schema=to_schema(columns))
        table.expires = datetime.now(timezone.utc) + timedelta(hours=self.settings.staging_expiration_hours)
        table.labels = dict(LABELS)
        self.client.create_table(table)
        if rows:
            job_config = bigquery.LoadJobConfig(
                schema=to_schema(columns),
                source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                labels=LABELS,
            )
            self.client.load_table_from_json(list(rows), table_id, job_config=job_config).result()
        log_event(logger, "bq_staging_loaded", staging_table=name, rows=len(rows))
        return table_id

    def _check_delete_guard(self, target: str, staging: str, max_fraction: float) -> None:
        result = list(self.client.query(sql.delete_guard_sql(target, staging)).result())
        active = int(result[0]["active_rows"]) if result else 0
        would_delete = int(result[0]["would_delete"]) if result else 0
        log_event(logger, "delete_guard_checked", active_rows=active, would_delete=would_delete)
        if active and would_delete / active > max_fraction:
            raise SafetyCheckError(
                f"Full resync would soft-delete {would_delete} of {active} active rows in {target} "
                f"(> MAX_DELETE_FRACTION={max_fraction}); refusing to apply"
            )

    def apply_batch(self, batch: ApplyBatch) -> ApplyStats:
        staged: list[str] = []
        staging_ds = self.settings.staging_dataset
        suffix = assert_safe_identifier(batch.run_id)
        parent_target = self._ref(batch.parent_spec.name)

        parent_staging: str | None = None
        if batch.parent_rows or batch.soft_delete_missing:
            table_id = self._stage(f"{batch.parent_spec.name}__{suffix}", batch.parent_spec.columns, batch.parent_rows)
            staged.append(table_id)
            parent_staging = self._ref(f"{batch.parent_spec.name}__{suffix}", staging_ds)

        children: list[sql.StagedChild] = []
        for child in batch.children:
            child_staging = None
            if parent_staging:
                # Created even when empty: a parent whose subform became empty must still
                # have its old child rows reconciled away.
                table_id = self._stage(f"{child.spec.name}__{suffix}", child.spec.columns, child.rows)
                staged.append(table_id)
                child_staging = self._ref(f"{child.spec.name}__{suffix}", staging_ds)
            children.append(sql.StagedChild(child.spec, self._ref(child.spec.name), child_staging))

        deleted_staging: str | None = None
        if batch.deleted:
            name = f"{batch.parent_spec.name}__deleted__{suffix}"
            staged.append(self._stage(name, DELETED_COLUMNS, batch.deleted))
            deleted_staging = self._ref(name, staging_ds)

        if batch.soft_delete_missing and parent_staging:
            self._check_delete_guard(parent_target, parent_staging, batch.max_delete_fraction)

        statements = sql.build_apply_statements(
            parent_spec=batch.parent_spec,
            parent_target=parent_target,
            parent_staging=parent_staging,
            children=children,
            deleted_staging=deleted_staging,
            soft_delete_missing=batch.soft_delete_missing,
            state_table=self._ref(STATE_TABLE),
        )
        script = sql.transaction_script(statements)
        state = batch.new_state
        params = [
            bigquery.ScalarQueryParameter("run_id", "STRING", batch.run_id),
            bigquery.ScalarQueryParameter("run_ts", "TIMESTAMP", batch.run_ts),
            bigquery.ScalarQueryParameter("module", "STRING", state.module),
            bigquery.ScalarQueryParameter("table_name", "STRING", state.table_name),
            bigquery.ScalarQueryParameter("checkpoint", "TIMESTAMP", state.last_modified_time),
            bigquery.ScalarQueryParameter("deleted_checkpoint", "TIMESTAMP", state.last_deleted_check),
            bigquery.ScalarQueryParameter("last_full_sync_at", "TIMESTAMP", state.last_full_sync_at),
        ]
        job = self._run_transaction(script, params, module=batch.module)
        stats = self._collect_dml_stats(job)
        log_event(logger, "bq_transaction_committed", statements=len(statements), job_id=job.job_id, **stats.summary())

        for table_id in staged:
            try:
                self.client.delete_table(table_id, not_found_ok=True)
            except gexc.GoogleAPICallError as exc:  # best effort; the dataset expiration cleans up
                log_event(logger, "bq_staging_cleanup_failed", logging.WARNING, table=table_id, error=str(exc))
        return stats

    def _run_transaction(self, script: str, params: list[Any], *, module: str) -> Any:
        labels = {**LABELS, "zoho_module": module.lower()[:63]}
        for attempt in range(self._max_tx_retries + 1):
            config = bigquery.QueryJobConfig(query_parameters=params, labels=labels)
            try:
                job = self.client.query(script, job_config=config)
                job.result()
                return job
            except gexc.GoogleAPICallError as exc:
                message = str(exc).lower()
                # Concurrent DML on the same table aborts one transaction; the whole script is
                # idempotent (versioned MERGE + checkpoint in the same transaction), so retry it.
                if attempt < self._max_tx_retries and any(m in message for m in CONCURRENT_UPDATE_MARKERS):
                    wait = 2 ** attempt * 5
                    log_event(logger, "bq_transaction_retry", logging.WARNING, attempt=attempt + 1, wait_seconds=wait)
                    self._sleep(wait)
                    continue
                raise

    def _collect_dml_stats(self, job: Any) -> ApplyStats:
        stats = ApplyStats()
        try:
            children = list(self.client.list_jobs(parent_job=job.job_id))
        except gexc.GoogleAPICallError:
            return stats
        for child in reversed(children):  # list_jobs returns newest first
            dml = getattr(child, "dml_stats", None)
            statement_type = getattr(child, "statement_type", None)
            if not dml or statement_type not in ("MERGE", "UPDATE", "INSERT", "DELETE"):
                continue
            stats.statements.append(
                {
                    "statement_type": statement_type,
                    "inserted": dml.inserted_row_count or 0,
                    "updated": dml.updated_row_count or 0,
                    "deleted": dml.deleted_row_count or 0,
                    "query": " ".join((getattr(child, "query", "") or "").split())[:120],
                }
            )
        return stats

    # -- reconciliation and run log -------------------------------------------------------
    def count_active(self, table: str) -> int:
        rows = list(self.client.query(f"SELECT COUNT(*) AS n FROM {self._ref(table)} WHERE _is_deleted IS NOT TRUE").result())
        return int(rows[0]["n"]) if rows else 0

    def count_active_children(self, table: str, parent_ids: Sequence[str]) -> dict[str, int]:
        query = (
            f"SELECT parent_id, COUNT(*) AS n FROM {self._ref(table)} "
            f"WHERE _is_deleted IS NOT TRUE AND parent_id IN UNNEST(@parent_ids) GROUP BY parent_id"
        )
        config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("parent_ids", "STRING", list(parent_ids))]
        )
        return {r["parent_id"]: int(r["n"]) for r in self.client.query(query, job_config=config).result()}

    def _append(self, table: str, columns: Sequence[Column], rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        job_config = bigquery.LoadJobConfig(
            schema=to_schema(columns),
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            labels=LABELS,
        )
        self.client.load_table_from_json(list(rows), self._table_id(table), job_config=job_config).result()

    def record_run(self, row: dict[str, Any]) -> None:
        self._append(RUNS_TABLE, RUNS_COLUMNS, [row])

    def record_reconciliation(self, rows: Sequence[dict[str, Any]]) -> None:
        self._append(RECONCILIATION_TABLE, RECONCILIATION_COLUMNS, rows)


def _state_from_row(row: dict[str, Any]) -> SyncState:
    return SyncState(
        module=row["module"],
        table_name=row.get("table_name") or "",
        last_modified_time=row.get("last_modified_time"),
        last_deleted_check=row.get("last_deleted_check"),
        last_full_sync_at=row.get("last_full_sync_at"),
        last_run_id=row.get("last_run_id"),
        updated_at=row.get("updated_at"),
    )
