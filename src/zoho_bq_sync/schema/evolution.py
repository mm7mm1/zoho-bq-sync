"""Additive, non-destructive schema evolution.

Rules (applied before any data is written):
- table missing                    -> create it
- new Zoho field                   -> ADD COLUMN (always NULLABLE; history is kept, old rows read NULL)
- Zoho field removed               -> keep the column (historical values stay queryable), log it
- INT64 -> NUMERIC/FLOAT64 etc.    -> ALTER COLUMN SET DATA TYPE (BigQuery-supported widening)
- Zoho narrowed a type             -> keep the wider column, coerce into it
- existing column is STRING        -> keep STRING, serialize new values as text (lossless)
- anything else                    -> SchemaConflictError before touching the table
Columns are never dropped or renamed automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Sequence

from ..errors import SchemaConflictError
from .model import NULLABLE, REPEATED, REQUIRED, STRING, Column, TableSpec

TYPE_ALIASES = {
    "INTEGER": "INT64",
    "FLOAT": "FLOAT64",
    "BOOLEAN": "BOOL",
    "DECIMAL": "NUMERIC",
    "BIGDECIMAL": "BIGNUMERIC",
    "RECORD": "STRUCT",
}

# Conversions BigQuery supports with ALTER COLUMN SET DATA TYPE.
SAFE_WIDENING = frozenset(
    {
        ("INT64", "NUMERIC"),
        ("INT64", "BIGNUMERIC"),
        ("INT64", "FLOAT64"),
        ("NUMERIC", "BIGNUMERIC"),
        ("NUMERIC", "FLOAT64"),
    }
)


def canonical_type(bq_type: str) -> str:
    upper = bq_type.upper()
    return TYPE_ALIASES.get(upper, upper)


@dataclass(frozen=True)
class ExistingColumn:
    name: str
    type: str
    mode: str = NULLABLE


@dataclass
class SchemaChange:
    table: str
    create: bool
    effective: TableSpec
    added: list[Column] = field(default_factory=list)
    widened: list[tuple[str, str, str]] = field(default_factory=list)  # (column, from, to)
    kept_existing_type: list[tuple[str, str, str]] = field(default_factory=list)  # (column, bq type, zoho-mapped type)
    orphaned: list[str] = field(default_factory=list)

    @property
    def has_ddl(self) -> bool:
        return self.create or bool(self.added) or bool(self.widened)


def plan_schema_change(desired: TableSpec, existing: Sequence[ExistingColumn] | None) -> SchemaChange:
    if existing is None:
        return SchemaChange(table=desired.name, create=True, effective=desired, added=list(desired.columns))

    by_name = {c.name.lower(): c for c in existing}
    change = SchemaChange(table=desired.name, create=False, effective=desired)
    effective: list[Column] = []

    for col in desired.columns:
        current = by_name.get(col.name.lower())
        if current is None:
            # BigQuery cannot add a REQUIRED column to a table that already has rows.
            added = replace(col, mode=NULLABLE) if col.mode == REQUIRED else col
            change.added.append(added)
            effective.append(added)
            continue

        cur_type = canonical_type(current.type)
        cur_mode = (current.mode or NULLABLE).upper()
        same_shape = (cur_mode == REPEATED) == (col.mode == REPEATED)

        if cur_type == col.type and same_shape:
            effective.append(replace(col, mode=cur_mode))
        elif cur_type == STRING:
            # A STRING column can hold any value as text; keeping it is lossless.
            effective.append(replace(col, type=STRING, mode=cur_mode))
            change.kept_existing_type.append((col.name, cur_type, col.type if same_shape else f"{col.type} {col.mode}"))
        elif not same_shape:
            raise SchemaConflictError(
                f"{desired.name}.{col.name}: BigQuery column is {cur_type} {cur_mode}, Zoho now maps to "
                f"{col.type} {col.mode}; change it manually (e.g. new column + backfill)"
            )
        elif (cur_type, col.type) in SAFE_WIDENING:
            change.widened.append((col.name, cur_type, col.type))
            effective.append(replace(col, mode=cur_mode))
        elif (col.type, cur_type) in SAFE_WIDENING:
            effective.append(replace(col, type=cur_type, mode=cur_mode))
            change.kept_existing_type.append((col.name, cur_type, col.type))
        else:
            raise SchemaConflictError(
                f"{desired.name}.{col.name}: BigQuery column is {cur_type}, Zoho field now maps to {col.type}; "
                "no lossless conversion exists, the table was left unchanged"
            )

    desired_names = {c.name.lower() for c in desired.columns}
    change.orphaned = [c.name for c in existing if c.name.lower() not in desired_names]
    change.effective = replace(desired, columns=tuple(effective))
    return change
