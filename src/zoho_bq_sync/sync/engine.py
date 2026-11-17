"""Sync orchestration for one run across modules.

Per module: discover fields -> build schema -> evolve tables (additive DDL) -> decide mode
-> extract -> normalize -> fetch deletions -> apply in one transaction (data + checkpoint)
-> reconcile counts -> record run. Modules are isolated: one failing module leaves its
rows and checkpoint untouched (additive DDL that ran before the transaction may stay),
the others still sync, and the run exits non-zero.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol, Sequence

from ..config import SyncSettings
from ..errors import ConfigError, ReconciliationError, SyncError, ZohoApiError
from ..logging_setup import bind_context, log_event
from ..schema.builder import build_module_plan
from ..schema.evolution import SchemaChange, plan_schema_change
from ..schema.model import ModulePlan, TableSpec
from ..transform.normalize import RunContext, to_child_rows, to_parent_row
from ..warehouse.base import ApplyBatch, ChildBatch, SyncState, Warehouse
from ..zoho.metadata import ZohoField, ZohoModule, select_modules
from ..zoho.records import parse_zoho_datetime
from .checkpoint import compute_safe_checkpoint
from .extract import extract_module
from .reconcile import Check, child_count_check, log_checks

logger = logging.getLogger(__name__)


class MetadataSource(Protocol):
    def list_modules(self) -> list[ZohoModule]: ...

    def get_fields(self, module_api_name: str) -> list[ZohoField]: ...


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id(ts: datetime) -> str:
    # Starts with a letter and has only [A-Za-z0-9_]: reused verbatim in staging table names.
    return f"r{ts.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}_{secrets.token_hex(3)}"


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


@dataclass
class ModuleResult:
    module: str
    status: str = "pending"  # success | failed | dry_run
    mode: str | None = None
    mode_reason: str | None = None
    table: str | None = None
    child_tables: list[str] = field(default_factory=list)
    records_extracted: int = 0
    parent_rows: int = 0
    child_rows: int = 0
    deleted_records: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    new_columns: list[str] = field(default_factory=list)
    widened_columns: list[str] = field(default_factory=list)
    drift_detected: bool = False
    checkpoint_before: datetime | None = None
    checkpoint_after: datetime | None = None
    api_calls: int = 0
    reconciliation: list[dict[str, Any]] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    def to_run_row(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "module": self.module,
            "mode": self.mode,
            "mode_reason": self.mode_reason,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "records_extracted": self.records_extracted,
            "parent_rows": self.parent_rows,
            "child_rows": self.child_rows,
            "deleted_records": self.deleted_records,
            "rows_inserted": self.rows_inserted,
            "rows_updated": self.rows_updated,
            "new_columns": list(self.new_columns),
            "widened_columns": list(self.widened_columns),
            "drift_detected": self.drift_detected,
            "checkpoint_before": _iso(self.checkpoint_before),
            "checkpoint_after": _iso(self.checkpoint_after),
            "api_calls": self.api_calls,
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("checkpoint_before", "checkpoint_after", "started_at", "finished_at"):
            data[key] = _iso(getattr(self, key))
        return data


@dataclass
class RunReport:
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    modules: list[ModuleResult] = field(default_factory=list)

    @property
    def failed(self) -> list[ModuleResult]:
        return [m for m in self.modules if m.status == "failed"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "failed_modules": [m.module for m in self.failed],
            "modules": [m.to_dict() for m in self.modules],
        }


class SyncRunFailed(SyncError):
    def __init__(self, report: RunReport) -> None:
        self.report = report
        details = "; ".join(f"{m.module}: {m.error}" for m in report.failed)
        super().__init__(f"{len(report.failed)} module(s) failed in run {report.run_id}: {details}")


def decide_mode(
    *,
    full_requested: bool,
    window_requested: bool,
    state: SyncState | None,
    new_columns: Sequence[str],
    backfill_on_new_fields: bool,
    has_version_field: bool,
) -> tuple[str, str]:
    if full_requested:
        return "full", "requested (--full)"
    if window_requested:
        return "window", "explicit --since/--until"
    if not has_version_field:
        return "full", "module has no Modified_Time field"
    if state is None or state.last_modified_time is None:
        return "full", "no checkpoint (initial load)"
    if new_columns and backfill_on_new_fields:
        return "full", "backfill of new columns (BACKFILL_ON_NEW_FIELDS)"
    return "incremental", "checkpoint"


class SyncEngine:
    def __init__(
        self,
        *,
        metadata: MetadataSource,
        records: Any,
        warehouse: Warehouse,
        settings: SyncSettings,
        clock: Callable[[], datetime] = utcnow,
        run_id: str | None = None,
        api_calls: Callable[[], int] | None = None,
    ) -> None:
        self._metadata = metadata
        self._records = records
        self._wh = warehouse
        self._settings = settings
        self._clock = clock
        self._api_calls = api_calls or (lambda: int(getattr(records, "api_calls", 0)))
        self.run_ts = clock()
        self.run_id = run_id or new_run_id(self.run_ts)
        self._ctx = RunContext(self.run_id, self.run_ts)

    # -- run ----------------------------------------------------------------------------
    def run(
        self,
        *,
        modules: Sequence[str] | None = None,
        full: bool = False,
        since: datetime | None = None,
        until: datetime | None = None,
        dry_run: bool = False,
    ) -> RunReport:
        if full and (since or until):
            raise ConfigError("--full cannot be combined with --since/--until")
        if since and until and since >= until:
            raise ConfigError("--since must be earlier than --until")

        bind_context(run_id=self.run_id)
        report = RunReport(run_id=self.run_id, started_at=self.run_ts)
        log_event(logger, "run_started", full=full, since=_iso(since), until=_iso(until), dry_run=dry_run)

        if not dry_run:
            self._wh.ensure_datasets()
            self._wh.ensure_ops_tables()

        include = list(modules) if modules else list(self._settings.modules)
        selected = select_modules(self._metadata.list_modules(), include, self._settings.exclude_modules)
        log_event(logger, "modules_selected", modules=[m.api_name for m in selected])

        for module in selected:
            bind_context(module=module.api_name)
            result = ModuleResult(module=module.api_name, started_at=self._clock())
            calls_before = self._api_calls()
            try:
                self._sync_module(module, result, full=full, since=since, until=until, dry_run=dry_run)
            except Exception as exc:  # isolate modules; the run still fails at the end
                result.status = "failed"
                result.error = f"{type(exc).__name__}: {exc}"
                log_event(logger, "module_sync_failed", logging.ERROR, exc_info=True, error=result.error)
            result.api_calls = self._api_calls() - calls_before
            result.finished_at = self._clock()
            if not dry_run:
                self._record_run(result)
            report.modules.append(result)
        bind_context(module=None)

        report.finished_at = self._clock()
        log_event(
            logger,
            "run_completed",
            logging.ERROR if report.failed else logging.INFO,
            modules=len(report.modules),
            failed=[m.module for m in report.failed],
            api_calls=sum(m.api_calls for m in report.modules),
        )
        if report.failed:
            raise SyncRunFailed(report)
        return report

    # -- one module -----------------------------------------------------------------------
    def _sync_module(
        self,
        module: ZohoModule,
        result: ModuleResult,
        *,
        full: bool,
        since: datetime | None,
        until: datetime | None,
        dry_run: bool,
    ) -> None:
        s = self._settings
        plan = self._discover(module.api_name)
        result.table = plan.parent.name
        result.child_tables = [c.name for c in plan.children]

        parent_change = self._evolve(plan.parent, dry_run)
        child_changes = [self._evolve(c, dry_run) for c in plan.children]
        for change in [parent_change, *child_changes]:
            if not change.create:
                result.new_columns.extend(f"{change.table}.{c.name}" for c in change.added)
            result.widened_columns.extend(f"{change.table}.{c}:{old}->{new}" for c, old, new in change.widened)
        parent_spec = parent_change.effective
        child_specs = [c.effective for c in child_changes]

        state = self._wh.load_state(module.api_name)
        mode, reason = decide_mode(
            full_requested=full,
            window_requested=since is not None or until is not None,
            state=state,
            new_columns=result.new_columns,
            backfill_on_new_fields=s.backfill_on_new_fields,
            has_version_field=plan.version_field is not None,
        )
        result.mode, result.mode_reason = mode, reason
        result.checkpoint_before = state.last_modified_time if state else None

        overlap = timedelta(seconds=s.overlap_seconds)
        if mode == "incremental":
            assert state is not None and state.last_modified_time is not None
            since = state.last_modified_time - overlap
        elif mode == "full":
            since = until = None
        log_event(
            logger,
            "extraction_window",
            mode=mode,
            reason=reason,
            since=_iso(since),
            until=_iso(until),
            checkpoint=_iso(result.checkpoint_before),
            overlap_seconds=s.overlap_seconds,
            strategy="record_fetch" if plan.requires_record_fetch else "list_api",
        )

        extraction = extract_module(
            self._records,
            module.api_name,
            plan.list_fields,
            requires_record_fetch=plan.requires_record_fetch,
            since=since,
            until=until,
            version_field=plan.version_field,
            clock=self._clock,
        )
        result.records_extracted = len(extraction.records)

        decision = compute_safe_checkpoint(
            extraction.pages,
            previous=None if mode == "full" else result.checkpoint_before,
            until=until,
        )
        result.drift_detected = decision.drift_detected
        # A manual replay window must never move the checkpoint (it could skip a gap).
        new_checkpoint = result.checkpoint_before if mode == "window" else decision.checkpoint
        if decision.drift_detected:
            log_event(
                logger,
                "pagination_drift_detected",
                logging.WARNING,
                concurrent_modifications=decision.concurrent_modifications,
                safe_checkpoint=_iso(decision.checkpoint),
            )
        log_event(
            logger,
            "extraction_completed",
            listed=extraction.listed,
            records=len(extraction.records),
            pages=len(extraction.pages),
            detail_calls=extraction.detail_calls,
            checkpoint_candidate=_iso(new_checkpoint),
        )

        # -- transform
        subform_names = [c.subform_field for c in child_specs if c.subform_field]
        parent_rows = [to_parent_row(r, parent_spec, self._ctx, subform_names) for r in extraction.records]
        child_batches: list[ChildBatch] = []
        child_expected: dict[str, dict[str, int]] = {}
        for spec in child_specs:
            rows: list[dict[str, Any]] = []
            per_parent: dict[str, int] = {}
            for record, parent_row in zip(extraction.records, parent_rows):
                version = parent_row.get(parent_spec.version_column) if parent_spec.version_column else None
                child_rows = to_child_rows(record, spec, self._ctx, version)
                per_parent[parent_row["id"]] = len(child_rows)
                rows.extend(child_rows)
            child_batches.append(ChildBatch(spec, rows))
            child_expected[spec.name] = per_parent
        result.parent_rows = len(parent_rows)
        result.child_rows = sum(len(b.rows) for b in child_batches)

        # -- deletions
        deleted: list[dict[str, Any]] = []
        deleted_checkpoint = state.last_deleted_check if state else None
        if mode == "incremental":
            assert state is not None
            base = state.last_deleted_check or state.last_modified_time
            poll_since = base - overlap if base else None
            items, server_time = self._records.list_deleted(module.api_name, poll_since)
            for item in items:
                if item.get("id") in (None, ""):
                    continue
                deleted_at = parse_zoho_datetime(item.get("deleted_time"))
                deleted.append({"id": str(item["id"]), "deleted_time": _iso(deleted_at), "deleted_type": item.get("type")})
            deleted_checkpoint = server_time or self._clock()
        elif mode == "full":
            deleted_checkpoint = extraction.pages[0].fetched_at if extraction.pages else self._clock()
        result.deleted_records = len(deleted)

        soft_delete_missing = mode == "full" and not decision.drift_detected
        if mode == "full" and decision.drift_detected:
            log_event(logger, "missing_row_reconciliation_skipped", logging.WARNING, reason="pagination drift during full extraction")

        new_state = SyncState(
            module=module.api_name,
            table_name=parent_spec.name,
            last_modified_time=new_checkpoint,
            last_deleted_check=deleted_checkpoint,
            last_full_sync_at=self.run_ts if mode == "full" else (state.last_full_sync_at if state else None),
            last_run_id=self.run_id,
        )
        result.checkpoint_after = new_checkpoint

        if dry_run:
            result.status = "dry_run"
            log_event(
                logger,
                "dry_run_summary",
                parent_rows=result.parent_rows,
                child_rows=result.child_rows,
                deleted=result.deleted_records,
                new_columns=result.new_columns,
                would_soft_delete_missing=soft_delete_missing,
            )
            return

        batch = ApplyBatch(
            module=module.api_name,
            run_id=self.run_id,
            run_ts=self.run_ts,
            mode=mode,
            parent_spec=parent_spec,
            parent_rows=parent_rows,
            children=child_batches,
            deleted=deleted,
            soft_delete_missing=soft_delete_missing,
            max_delete_fraction=s.max_delete_fraction,
            new_state=new_state,
        )
        stats = self._wh.apply_batch(batch)
        totals = stats.summary()
        result.rows_inserted, result.rows_updated = totals["inserted"], totals["updated"]
        log_event(logger, "checkpoint_committed", checkpoint_before=_iso(result.checkpoint_before), checkpoint_after=_iso(new_checkpoint), deleted_checkpoint=_iso(deleted_checkpoint))

        checks = self._reconcile(module.api_name, parent_spec, child_specs, mode, child_expected, child_batches)
        result.reconciliation = [c.to_row(self.run_id, module.api_name, self._clock()) for c in checks]
        if result.reconciliation:
            self._wh.record_reconciliation(result.reconciliation)
        result.status = "success"
        log_event(
            logger,
            "module_sync_completed",
            mode=mode,
            records=result.records_extracted,
            parent_rows=result.parent_rows,
            child_rows=result.child_rows,
            deleted=result.deleted_records,
            inserted=result.rows_inserted,
            updated=result.rows_updated,
            new_columns=result.new_columns,
        )
        mismatched = [c for c in checks if c.status != "OK"]
        if mismatched and s.fail_on_reconciliation_mismatch:
            raise ReconciliationError(
                "data committed, but reconciliation failed: " + ", ".join(f"{c.table_name}.{c.check_name}" for c in mismatched)
            )

    # -- helpers --------------------------------------------------------------------------
    def _discover(self, module: str) -> ModulePlan:
        fields = self._metadata.get_fields(module)
        subforms: dict[str, list[ZohoField]] = {}
        for f in fields:
            if f.is_subform and f"{module}.{f.api_name}" not in self._settings.exclude_fields:
                subforms[f.api_name] = self._metadata.get_fields(f.associated_module or f.api_name)
        plan = build_module_plan(
            module,
            fields,
            subforms,
            table_name_overrides=self._settings.table_name_overrides,
            exclude_fields=self._settings.exclude_fields,
            store_raw_json=self._settings.store_raw_json,
        )
        for warning in plan.warnings:
            log_event(logger, "type_mapping_fallback", logging.WARNING, detail=warning)
        log_event(
            logger,
            "schema_discovered",
            fields=len(fields),
            table=plan.parent.name,
            columns=len(plan.parent.columns),
            subforms={c.subform_field: c.name for c in plan.children},
            skipped=[f"{n} ({r})" for n, r in plan.skipped_fields],
        )
        return plan

    def _evolve(self, spec: TableSpec, dry_run: bool) -> SchemaChange:
        change = plan_schema_change(spec, self._wh.get_table_columns(spec.name))
        if change.create:
            log_event(logger, "table_create_planned" if dry_run else "table_created", table=spec.name, columns=len(spec.columns))
            if not dry_run:
                self._wh.create_table(change.effective)
            return change
        if change.added:
            log_event(
                logger,
                "schema_change_planned" if dry_run else "schema_evolved",
                table=spec.name,
                added=[f"{c.name}:{c.type}{' REPEATED' if c.mode == 'REPEATED' else ''} <- {c.source}" for c in change.added],
            )
            if not dry_run:
                self._wh.add_columns(spec.name, change.added)
        if change.widened:
            log_event(logger, "column_widening", table=spec.name, changes=change.widened)
            if not dry_run:
                self._wh.widen_columns(spec.name, change.widened)
        for column, bq_type, zoho_type in change.kept_existing_type:
            log_event(logger, "zoho_type_changed", logging.WARNING, table=spec.name, column=column, bigquery_type=bq_type, zoho_mapped_type=zoho_type)
        if change.orphaned:
            log_event(logger, "columns_not_in_zoho", table=spec.name, columns=change.orphaned, action="kept for history")
        return change

    def _reconcile(
        self,
        module: str,
        parent: TableSpec,
        children: Sequence[TableSpec],
        mode: str,
        child_expected: dict[str, dict[str, int]],
        child_batches: Sequence[ChildBatch],
    ) -> list[Check]:
        checks: list[Check] = []
        try:
            zoho_count = self._records.count(module)
        except ZohoApiError as exc:
            log_event(logger, "zoho_count_unavailable", logging.WARNING, error=str(exc))
            zoho_count = None
        if zoho_count is not None:
            checks.append(Check(parent.name, "zoho_count_vs_bq_active", zoho_count, self._wh.count_active(parent.name)))
        totals = {b.spec.name: len(b.rows) for b in child_batches}
        for spec in children:
            if mode == "full":
                checks.append(Check(spec.name, "child_rows_total", totals.get(spec.name, 0), self._wh.count_active(spec.name)))
            elif child_expected.get(spec.name):
                expected = child_expected[spec.name]
                actual = self._wh.count_active_children(spec.name, list(expected))
                checks.append(child_count_check(spec.name, expected, actual))
        log_checks(checks)
        return checks

    def _record_run(self, result: ModuleResult) -> None:
        try:
            self._wh.record_run(result.to_run_row(self.run_id))
        except Exception as exc:  # the data commit already happened; never mask the real outcome
            log_event(logger, "run_log_write_failed", logging.ERROR, error=f"{type(exc).__name__}: {exc}")
