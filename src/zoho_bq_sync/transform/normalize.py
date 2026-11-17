"""Normalize Zoho records into parent rows and subform child rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from ..errors import CoercionError
from ..schema.builder import CHILD_VERSION_COLUMN, PARENT_FK
from ..schema.model import Kind, TableSpec
from .coerce import coerce_value, to_json_text


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_ts: datetime

    @property
    def run_ts_iso(self) -> str:
        return self.run_ts.astimezone(timezone.utc).isoformat()


def _project(value: Any, kind: str) -> Any:
    if kind == Kind.LOOKUP_ID:
        return value.get("id") if isinstance(value, dict) else value
    if kind == Kind.LOOKUP_NAME:
        return value.get("name") if isinstance(value, dict) else None
    return value


def _fill_columns(row: dict[str, Any], source: dict[str, Any], spec: TableSpec, where: str) -> None:
    for column in spec.data_columns:
        raw = _project(source.get(column.source) if column.source else None, column.kind)
        try:
            row[column.name] = coerce_value(raw, column)
        except CoercionError as exc:
            raise CoercionError(f"{where} field {column.source} -> {spec.name}.{column.name} ({column.type}): {exc}") from None


def _meta(row: dict[str, Any], spec: TableSpec, ctx: RunContext, raw: dict[str, Any] | None) -> None:
    row["_synced_at"] = ctx.run_ts_iso
    row["_run_id"] = ctx.run_id
    row["_is_deleted"] = False
    row["_deleted_at"] = None
    if spec.column("_raw_json") is not None:
        row["_raw_json"] = to_json_text(raw) if raw is not None else None


def to_parent_row(
    record: dict[str, Any],
    spec: TableSpec,
    ctx: RunContext,
    subform_fields: Iterable[str] = (),
) -> dict[str, Any]:
    if record.get("id") in (None, ""):
        raise CoercionError(f"{spec.zoho_module}: record without id")
    row: dict[str, Any] = {"id": str(record["id"])}
    _fill_columns(row, record, spec, where=f"{spec.zoho_module} record {record['id']}")
    excluded = set(subform_fields)
    # Subform rows live in child tables; keeping them out of _raw_json avoids duplicating them.
    _meta(row, spec, ctx, {k: v for k, v in record.items() if k not in excluded})
    return row


def to_child_rows(
    record: dict[str, Any],
    spec: TableSpec,
    ctx: RunContext,
    parent_version: str | None,
) -> list[dict[str, Any]]:
    assert spec.subform_field is not None
    items = record.get(spec.subform_field)
    if items is None:
        return []
    if not isinstance(items, list):
        raise CoercionError(f"{spec.zoho_module}: subform {spec.subform_field} of record {record.get('id')} is not a list")
    parent_id = str(record["id"])
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("id") in (None, ""):
            raise CoercionError(f"{spec.subform_field} row {index} of record {parent_id} has no id")
        row: dict[str, Any] = {
            "id": str(item["id"]),
            PARENT_FK: parent_id,
            "_row_index": index,
            CHILD_VERSION_COLUMN: parent_version,
        }
        _fill_columns(row, item, spec, where=f"{spec.subform_field} row {item['id']} of record {parent_id}")
        _meta(row, spec, ctx, item)
        rows.append(row)
    return rows
