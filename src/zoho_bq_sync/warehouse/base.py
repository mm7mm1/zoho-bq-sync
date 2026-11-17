"""Warehouse contract used by the sync engine.

The engine only talks to this interface, which keeps orchestration testable with an
in-memory implementation while the BigQuery implementation owns SQL and jobs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence

from ..schema.evolution import ExistingColumn
from ..schema.model import REQUIRED, STRING, TIMESTAMP, Column, TableSpec


@dataclass(frozen=True)
class SyncState:
    module: str
    table_name: str
    last_modified_time: datetime | None = None  # incremental high-water mark (Zoho Modified_Time)
    last_deleted_check: datetime | None = None  # Zoho server time of the last deleted-records poll
    last_full_sync_at: datetime | None = None
    last_run_id: str | None = None
    updated_at: datetime | None = None  # last successful commit; drives freshness monitoring


@dataclass
class ChildBatch:
    spec: TableSpec
    rows: list[dict[str, Any]]


@dataclass
class ApplyBatch:
    module: str
    run_id: str
    run_ts: datetime
    mode: str  # full | incremental | window
    parent_spec: TableSpec
    parent_rows: list[dict[str, Any]]
    children: list[ChildBatch]
    deleted: list[dict[str, Any]]  # {"id", "deleted_time", "deleted_type"}
    soft_delete_missing: bool  # full resync: rows absent from Zoho are soft-deleted
    max_delete_fraction: float
    new_state: SyncState


@dataclass
class ApplyStats:
    statements: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        totals = {"inserted": 0, "updated": 0, "deleted": 0}
        for stmt in self.statements:
            for key in totals:
                totals[key] += int(stmt.get(key) or 0)
        return totals


DELETED_COLUMNS: tuple[Column, ...] = (
    Column("id", STRING, REQUIRED),
    Column("deleted_time", TIMESTAMP),
    Column("deleted_type", STRING),
)


class Warehouse(Protocol):
    def ensure_datasets(self) -> None: ...

    def ensure_ops_tables(self) -> None: ...

    def get_table_columns(self, table: str) -> list[ExistingColumn] | None: ...

    def create_table(self, spec: TableSpec) -> None: ...

    def add_columns(self, table: str, columns: Sequence[Column]) -> None: ...

    def widen_columns(self, table: str, changes: Sequence[tuple[str, str, str]]) -> None: ...

    def load_state(self, module: str) -> SyncState | None: ...

    def read_states(self) -> list[SyncState]: ...

    def apply_batch(self, batch: ApplyBatch) -> ApplyStats: ...

    def count_active(self, table: str) -> int: ...

    def count_active_children(self, table: str, parent_ids: Sequence[str]) -> dict[str, int]: ...

    def record_run(self, row: dict[str, Any]) -> None: ...

    def record_reconciliation(self, rows: Sequence[dict[str, Any]]) -> None: ...
