"""Test doubles. Everything here is MOCK data for unit tests, never used by the live demo."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Sequence

from zoho_bq_sync.errors import SafetyCheckError
from zoho_bq_sync.schema.evolution import ExistingColumn
from zoho_bq_sync.schema.model import Column, TableSpec
from zoho_bq_sync.warehouse.base import ApplyBatch, ApplyStats, SyncState
from zoho_bq_sync.zoho.metadata import ZohoField, ZohoModule
from zoho_bq_sync.zoho.records import RecordPage, parse_zoho_datetime

UTC = timezone.utc


# -- HTTP level ------------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code: int = 200, json_body: Any = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._json = json_body
        self.headers = headers or {}
        self.content = b"" if json_body is None else b"{}"

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Returns queued responses (or calls a handler) and records every request."""

    def __init__(self, responses: Sequence[Any] | Callable[..., Any]) -> None:
        self._responses = list(responses) if not callable(responses) else None
        self._handler = responses if callable(responses) else None
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._handler:
            result = self._handler(method, url, **kwargs)
        else:
            assert self._responses, f"unexpected request {method} {url}"
            result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def post(self, url: str, data: Any = None, timeout: Any = None) -> Any:
        return self.request("POST", url, data=data, timeout=timeout)


class StaticAuth:
    def __init__(self, token: str = "tok-1") -> None:
        self.token = token
        self.invalidations = 0

    def get_access_token(self) -> str:
        return self.token

    def invalidate(self) -> None:
        self.invalidations += 1
        self.token = f"tok-{self.invalidations + 1}"


# -- Zoho level ------------------------------------------------------------------------------
class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def zf(api_name: str, data_type: str, field_id: str, **kw: Any) -> ZohoField:
    return ZohoField(api_name=api_name, data_type=data_type, field_id=field_id, **kw)


class FakeMetadata:
    def __init__(self, modules: list[ZohoModule], fields: dict[str, list[ZohoField]]) -> None:
        self.modules = modules
        self.fields = fields

    def list_modules(self) -> list[ZohoModule]:
        return list(self.modules)

    def get_fields(self, module_api_name: str) -> list[ZohoField]:
        return list(self.fields[module_api_name])


class FakeRecords:
    """In-memory Zoho module store with list/detail/deleted/count semantics."""

    MAX_FIELDS_PER_REQUEST = 50

    def __init__(self, clock: FakeClock, subform_fields: Sequence[str] = ()) -> None:
        self.clock = clock
        self.store: dict[str, dict[str, dict[str, Any]]] = {}
        self.deleted: dict[str, list[dict[str, Any]]] = {}
        self.subform_fields = set(subform_fields)
        self.list_calls: list[dict[str, Any]] = []
        self.deleted_calls: list[datetime | None] = []
        self.detail_calls = 0
        self.api_calls = 0

    def upsert(self, module: str, record: dict[str, Any]) -> None:
        record = copy.deepcopy(record)
        record["Modified_Time"] = self.clock().isoformat()
        self.store.setdefault(module, {})[record["id"]] = record

    def delete(self, module: str, record_id: str) -> None:
        self.store[module].pop(record_id)
        self.deleted.setdefault(module, []).append({"id": record_id, "deleted_time": self.clock().isoformat(), "type": "recycle"})

    def iter_pages(self, module: str, fields: Sequence[str], *, modified_since: datetime | None = None, sort_by: str = "Modified_Time", sort_order: str = "asc") -> Iterator[RecordPage]:
        self.list_calls.append({"module": module, "fields": list(fields), "since": modified_since})
        self.api_calls += 1
        rows = sorted(self.store.get(module, {}).values(), key=lambda r: (r["Modified_Time"], r["id"]))
        if modified_since is not None:
            rows = [r for r in rows if parse_zoho_datetime(r["Modified_Time"]) >= modified_since]
        projected = []
        for r in rows:
            item = {"id": r["id"]}
            for f in fields:
                if f in self.subform_fields:
                    continue  # the real list API returns only a count for subforms
                item[f] = copy.deepcopy(r.get(f))
            projected.append(item)
        for i in range(0, len(projected), 200):
            yield RecordPage(projected[i : i + 200], self.clock(), i // 200 + 1)

    def fetch_by_ids(self, module: str, ids: Sequence[str], fields: Sequence[str]) -> list[dict[str, Any]]:
        self.api_calls += 1
        store = self.store.get(module, {})
        return [{"id": i, **{f: copy.deepcopy(store[i].get(f)) for f in fields}} for i in ids if i in store]

    def get_record(self, module: str, record_id: str) -> dict[str, Any] | None:
        self.detail_calls += 1
        self.api_calls += 1
        record = self.store.get(module, {}).get(record_id)
        return copy.deepcopy(record) if record else None

    def list_deleted(self, module: str, since: datetime | None) -> tuple[list[dict[str, Any]], datetime | None]:
        self.deleted_calls.append(since)
        self.api_calls += 1
        items = [d for d in self.deleted.get(module, []) if since is None or parse_zoho_datetime(d["deleted_time"]) >= since]
        return copy.deepcopy(items), self.clock()

    def count(self, module: str) -> int:
        self.api_calls += 1
        return len(self.store.get(module, {}))


# -- Warehouse level -------------------------------------------------------------------------
def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


class InMemoryWarehouse:
    """Python model of the SQL semantics in warehouse/sql.py (tested separately as SQL).

    apply_batch works on a copy and swaps it in only at the end: the same all-or-nothing
    behaviour as the BigQuery transaction.
    """

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, Any]] = {}
        self.states: dict[str, SyncState] = {}
        self.runs: list[dict[str, Any]] = []
        self.reconciliation: list[dict[str, Any]] = []
        self.ddl: list[tuple[str, str, Any]] = []
        self.fail_next_apply: Exception | None = None
        self.writes = 0

    # schema
    def ensure_datasets(self) -> None:
        self.writes += 1

    def ensure_ops_tables(self) -> None:
        pass

    def get_table_columns(self, table: str) -> list[ExistingColumn] | None:
        t = self.tables.get(table)
        if t is None:
            return None
        return [ExistingColumn(c.name, c.type, c.mode) for c in t["columns"]]

    def create_table(self, spec: TableSpec) -> None:
        self.writes += 1
        self.ddl.append(("create", spec.name, [c.name for c in spec.columns]))
        self.tables[spec.name] = {"columns": list(spec.columns), "rows": {}}

    def add_columns(self, table: str, columns: Sequence[Column]) -> None:
        self.writes += 1
        self.ddl.append(("add", table, [c.name for c in columns]))
        self.tables[table]["columns"].extend(columns)

    def widen_columns(self, table: str, changes: Sequence[tuple[str, str, str]]) -> None:
        self.writes += 1
        self.ddl.append(("widen", table, list(changes)))
        cols = self.tables[table]["columns"]
        for name, _old, new in changes:
            cols[:] = [replace(c, type=new) if c.name == name else c for c in cols]

    # state
    def load_state(self, module: str) -> SyncState | None:
        return self.states.get(module)

    def read_states(self) -> list[SyncState]:
        return list(self.states.values())

    # data
    def rows(self, table: str) -> dict[str, dict[str, Any]]:
        return self.tables[table]["rows"]

    def active(self, table: str) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.rows(table).items() if not v.get("_is_deleted")}

    @staticmethod
    def _upsert(target: dict[str, dict[str, Any]], rows: Sequence[dict[str, Any]], version: str | None) -> None:
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            current = latest.get(row["id"])
            if current is None or (version and (_ts(row.get(version)) or datetime.min.replace(tzinfo=UTC)) >= (_ts(current.get(version)) or datetime.min.replace(tzinfo=UTC))):
                latest[row["id"]] = row
        for key, row in latest.items():
            existing = target.get(key)
            if existing is None:
                target[key] = dict(row)
                continue
            if version:
                t_v, s_v = _ts(existing.get(version)), _ts(row.get(version))
                if t_v is not None and s_v is not None:
                    if s_v < t_v:
                        continue
                    deleted_at, synced_at = _ts(existing.get("_deleted_at")), _ts(row.get("_synced_at"))
                    if s_v == t_v and existing.get("_is_deleted") and deleted_at and synced_at and synced_at <= deleted_at:
                        continue  # stale same-version replay must not revive a soft-deleted row
            existing.update(row)

    def apply_batch(self, batch: ApplyBatch) -> ApplyStats:
        if self.fail_next_apply is not None:
            exc, self.fail_next_apply = self.fail_next_apply, None
            raise exc
        snapshot = copy.deepcopy(self.tables)
        parent = snapshot[batch.parent_spec.name]["rows"]
        pv = batch.parent_spec.version_column
        staged_ids = {r["id"] for r in batch.parent_rows}

        if batch.soft_delete_missing:
            active = [k for k, v in parent.items() if not v.get("_is_deleted")]
            would = [k for k in active if k not in staged_ids]
            if active and len(would) / len(active) > batch.max_delete_fraction:
                raise SafetyCheckError(f"would soft-delete {len(would)} of {len(active)}")

        before = {k: dict(v) for k, v in parent.items()}
        staged_parent = {r["id"]: r for r in batch.parent_rows}
        if batch.parent_rows or batch.soft_delete_missing:
            self._upsert(parent, batch.parent_rows, pv)
            if batch.soft_delete_missing:
                for k, v in parent.items():
                    if not v.get("_is_deleted") and k not in staged_ids:
                        v.update(_is_deleted=True, _deleted_at=batch.run_ts.isoformat(), _run_id=batch.run_id)
            for child in batch.children:
                rows = snapshot[child.spec.name]["rows"]
                self._upsert(rows, child.rows, child.spec.version_column)
                child_ids = {r["id"] for r in child.rows}
                for k, v in rows.items():
                    p = staged_parent.get(v["parent_id"])
                    if v.get("_is_deleted") or p is None or k in child_ids:
                        continue
                    if pv and _ts(v.get("_parent_modified_time")) and _ts(p.get(pv)) and _ts(p.get(pv)) < _ts(v.get("_parent_modified_time")):
                        continue
                    v.update(_is_deleted=True, _deleted_at=batch.run_ts.isoformat(), _run_id=batch.run_id)
                    if pv and p.get(pv):
                        v["_parent_modified_time"] = p[pv]
                if batch.soft_delete_missing:
                    for k, v in rows.items():
                        if not v.get("_is_deleted") and k not in child_ids:
                            v.update(_is_deleted=True, _deleted_at=batch.run_ts.isoformat(), _run_id=batch.run_id)

        deleted_at: dict[str, datetime | None] = {}
        for d in batch.deleted:
            t = _ts(d.get("deleted_time"))
            prev = deleted_at.get(d["id"])
            deleted_at[d["id"]] = t if prev is None or (t and t > prev) else prev
        for rid, when in deleted_at.items():
            row = parent.get(rid)
            if row and not row.get("_is_deleted"):
                mt = _ts(row.get(pv)) if pv else None
                if mt is None or when is None or mt <= when:
                    row.update(_is_deleted=True, _deleted_at=(when or batch.run_ts).isoformat(), _run_id=batch.run_id)
            for child in batch.children:
                for v in snapshot[child.spec.name]["rows"].values():
                    if v["parent_id"] == rid and not v.get("_is_deleted"):
                        pmt = _ts(v.get("_parent_modified_time"))
                        if pmt is None or when is None or pmt <= when:
                            v.update(_is_deleted=True, _deleted_at=(when or batch.run_ts).isoformat(), _run_id=batch.run_id)

        inserted = len([k for k in parent if k not in before])
        updated = len([k for k in parent if k in before and parent[k] != before[k]])
        self.tables = snapshot
        self.states[batch.module] = replace(batch.new_state, updated_at=batch.run_ts)
        self.writes += 1
        return ApplyStats([{"statement_type": "MERGE", "inserted": inserted, "updated": updated, "deleted": 0}])

    def count_active(self, table: str) -> int:
        return len(self.active(table))

    def count_active_children(self, table: str, parent_ids: Sequence[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        wanted = set(parent_ids)
        for row in self.active(table).values():
            if row["parent_id"] in wanted:
                counts[row["parent_id"]] = counts.get(row["parent_id"], 0) + 1
        return counts

    def record_run(self, row: dict[str, Any]) -> None:
        self.runs.append(row)

    def record_reconciliation(self, rows: Sequence[dict[str, Any]]) -> None:
        self.reconciliation.extend(rows)
