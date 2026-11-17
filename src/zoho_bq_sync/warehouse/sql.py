"""BigQuery SQL generation (pure functions, unit-tested as strings, executed in live tests).

Identifiers cannot be query parameters, so every interpolated name goes through
assert_safe_identifier; values (run id, timestamps, checkpoints) are always parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..schema.model import TableSpec
from ..schema.naming import assert_safe_identifier

_TABLE_PART_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def q(identifier: str) -> str:
    return f"`{assert_safe_identifier(identifier)}`"


def table_ref(project: str, dataset: str, table: str) -> str:
    for part in (project, dataset, table):
        if not part or not set(part) <= _TABLE_PART_CHARS:
            raise ValueError(f"Unsafe table reference part: {part!r}")
    return f"`{project}.{dataset}.{table}`"


def dedup_source(staging: str, key: str, version_column: str | None) -> str:
    """Latest version per key; protects MERGE from 'multiple source rows matched' errors."""
    order = f"{q(version_column)} DESC" if version_column else f"{q('_synced_at')} DESC"
    return (
        f"(SELECT * EXCEPT(_dedup_rn) FROM ("
        f"SELECT *, ROW_NUMBER() OVER (PARTITION BY {q(key)} ORDER BY {order}) AS _dedup_rn FROM {staging}"
        f") WHERE _dedup_rn = 1)"
    )


def merge_upsert_sql(target: str, source: str, columns: Sequence[str], key: str, version_column: str | None) -> str:
    if key not in columns:
        raise ValueError("key column must be part of the column list")
    updates = ",\n    ".join(f"{q(c)} = S.{q(c)}" for c in columns if c != key)
    insert_columns = ", ".join(q(c) for c in columns)
    insert_values = ", ".join(f"S.{q(c)}" for c in columns)
    guard = ""
    if version_column:
        v = q(version_column)
        # An older version never overwrites a newer one, so replays and overlapping
        # windows are idempotent. Equal versions may re-write the row (fills new columns),
        # but must not revive a row soft-deleted after that source row was read
        # (a stale concurrent run committing late).
        same_version = "TRUE"
        if {"_is_deleted", "_deleted_at", "_synced_at"} <= set(columns):
            same_version = "(T._is_deleted IS NOT TRUE OR T._deleted_at IS NULL OR S._synced_at > T._deleted_at)"
        guard = f" AND (T.{v} IS NULL OR S.{v} IS NULL OR S.{v} > T.{v} OR (S.{v} = T.{v} AND {same_version}))"
    return (
        f"MERGE {target} AS T\n"
        f"USING {source} AS S\n"
        f"ON T.{q(key)} = S.{q(key)}\n"
        f"WHEN MATCHED{guard} THEN UPDATE SET\n    {updates}\n"
        f"WHEN NOT MATCHED BY TARGET THEN\n  INSERT ({insert_columns})\n  VALUES ({insert_values})"
    )


def soft_delete_missing_sql(target: str, staging: str, key: str = "id") -> str:
    """Full resync: rows that Zoho no longer returns are soft-deleted."""
    k = q(key)
    return (
        f"UPDATE {target} AS T\n"
        f"SET _is_deleted = TRUE, _deleted_at = @run_ts, _run_id = @run_id\n"
        f"WHERE T._is_deleted IS NOT TRUE\n"
        f"  AND NOT EXISTS (SELECT 1 FROM {staging} AS S WHERE S.{k} = T.{k})"
    )


def soft_delete_removed_children_sql(
    child_target: str,
    child_staging: str,
    parent_staging: str,
    parent_version_column: str | None,
    parent_key: str = "parent_id",
) -> str:
    """Subform rows removed in Zoho disappear from the parent's array; the parent was
    re-fetched in full, so any active child of that parent missing from staging is gone.
    The parent's version is stamped on the deleted row so an older replay cannot revive it."""
    if parent_version_column:
        v = q(parent_version_column)
        parents = f"(SELECT id, MAX({v}) AS version FROM {parent_staging} GROUP BY id)"
        set_version = ", _parent_modified_time = COALESCE(P.version, T._parent_modified_time)"
        guard = "\n  AND (T._parent_modified_time IS NULL OR P.version IS NULL OR P.version >= T._parent_modified_time)"
    else:
        parents = f"(SELECT DISTINCT id FROM {parent_staging})"
        set_version = ""
        guard = ""
    return (
        f"UPDATE {child_target} AS T\n"
        f"SET _is_deleted = TRUE, _deleted_at = @run_ts, _run_id = @run_id{set_version}\n"
        f"FROM {parents} AS P\n"
        f"WHERE T.{q(parent_key)} = P.id AND T._is_deleted IS NOT TRUE{guard}\n"
        f"  AND NOT EXISTS (SELECT 1 FROM {child_staging} AS C WHERE C.id = T.id)"
    )


def _deleted_source(deleted_staging: str) -> str:
    return f"(SELECT id, MAX(deleted_time) AS deleted_time FROM {deleted_staging} GROUP BY id)"


def apply_deletions_sql(target: str, deleted_staging: str, version_column: str | None) -> str:
    guard = ""
    if version_column:
        v = q(version_column)
        # A record restored and edited after its deletion must stay alive.
        guard = f"\n  AND (T.{v} IS NULL OR D.deleted_time IS NULL OR T.{v} <= D.deleted_time)"
    return (
        f"UPDATE {target} AS T\n"
        f"SET _is_deleted = TRUE, _deleted_at = COALESCE(D.deleted_time, @run_ts), _run_id = @run_id\n"
        f"FROM {_deleted_source(deleted_staging)} AS D\n"
        f"WHERE T.id = D.id AND T._is_deleted IS NOT TRUE{guard}"
    )


def cascade_child_deletions_sql(child_target: str, deleted_staging: str, parent_key: str = "parent_id") -> str:
    return (
        f"UPDATE {child_target} AS T\n"
        f"SET _is_deleted = TRUE, _deleted_at = COALESCE(D.deleted_time, @run_ts), _run_id = @run_id\n"
        f"FROM {_deleted_source(deleted_staging)} AS D\n"
        f"WHERE T.{q(parent_key)} = D.id AND T._is_deleted IS NOT TRUE\n"
        f"  AND (T._parent_modified_time IS NULL OR D.deleted_time IS NULL OR T._parent_modified_time <= D.deleted_time)"
    )


def upsert_state_sql(state_table: str) -> str:
    return (
        f"MERGE {state_table} AS T\n"
        f"USING (SELECT @module AS module, @table_name AS table_name, @checkpoint AS last_modified_time,\n"
        f"              @deleted_checkpoint AS last_deleted_check, @last_full_sync_at AS last_full_sync_at,\n"
        f"              @run_id AS last_run_id, @run_ts AS updated_at) AS S\n"
        f"ON T.module = S.module\n"
        f"WHEN MATCHED THEN UPDATE SET\n"
        f"    table_name = S.table_name,\n"
        f"    last_modified_time = S.last_modified_time,\n"
        f"    last_deleted_check = S.last_deleted_check,\n"
        f"    last_full_sync_at = COALESCE(S.last_full_sync_at, T.last_full_sync_at),\n"
        f"    last_run_id = S.last_run_id,\n"
        f"    updated_at = S.updated_at\n"
        f"WHEN NOT MATCHED THEN\n"
        f"  INSERT (module, table_name, last_modified_time, last_deleted_check, last_full_sync_at, last_run_id, updated_at)\n"
        f"  VALUES (S.module, S.table_name, S.last_modified_time, S.last_deleted_check, S.last_full_sync_at, S.last_run_id, S.updated_at)"
    )


def delete_guard_sql(target: str, staging: str, key: str = "id") -> str:
    k = q(key)
    return (
        f"SELECT COUNT(*) AS active_rows, COUNTIF(S.{k} IS NULL) AS would_delete\n"
        f"FROM {target} AS T\n"
        f"LEFT JOIN (SELECT DISTINCT {k} FROM {staging}) AS S ON T.{k} = S.{k}\n"
        f"WHERE T._is_deleted IS NOT TRUE"
    )


def transaction_script(statements: Sequence[str]) -> str:
    """All data changes plus the checkpoint commit atomically: either the module is
    fully synced and the checkpoint advances, or nothing changes."""
    body = ";\n\n".join(s.strip().rstrip(";") for s in statements) + ";"
    return (
        "BEGIN\n"
        "BEGIN TRANSACTION;\n\n"
        f"{body}\n\n"
        "COMMIT TRANSACTION;\n"
        "EXCEPTION WHEN ERROR THEN\n"
        "  ROLLBACK TRANSACTION;\n"
        "  RAISE USING MESSAGE = @@error.message;\n"
        "END;"
    )


@dataclass(frozen=True)
class StagedChild:
    spec: TableSpec
    target: str
    staging: str | None


def build_apply_statements(
    *,
    parent_spec: TableSpec,
    parent_target: str,
    parent_staging: str | None,
    children: Sequence[StagedChild],
    deleted_staging: str | None,
    soft_delete_missing: bool,
    state_table: str,
) -> list[str]:
    statements: list[str] = []
    if parent_staging:
        statements.append(
            merge_upsert_sql(
                parent_target,
                dedup_source(parent_staging, parent_spec.key, parent_spec.version_column),
                parent_spec.column_names,
                parent_spec.key,
                parent_spec.version_column,
            )
        )
        if soft_delete_missing:
            statements.append(soft_delete_missing_sql(parent_target, parent_staging, parent_spec.key))
        for child in children:
            if child.staging is None:
                continue
            statements.append(
                merge_upsert_sql(
                    child.target,
                    dedup_source(child.staging, child.spec.key, child.spec.version_column),
                    child.spec.column_names,
                    child.spec.key,
                    child.spec.version_column,
                )
            )
            statements.append(
                soft_delete_removed_children_sql(child.target, child.staging, parent_staging, parent_spec.version_column)
            )
            if soft_delete_missing:
                statements.append(soft_delete_missing_sql(child.target, child.staging, child.spec.key))
    if deleted_staging:
        statements.append(apply_deletions_sql(parent_target, deleted_staging, parent_spec.version_column))
        for child in children:
            statements.append(cascade_child_deletions_sql(child.target, deleted_staging))
    statements.append(upsert_state_sql(state_table))
    return statements
