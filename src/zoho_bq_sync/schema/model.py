"""Warehouse-agnostic schema model (no google-cloud imports, so it is trivially testable)."""

from __future__ import annotations

from dataclasses import dataclass

STRING = "STRING"
INT64 = "INT64"
FLOAT64 = "FLOAT64"
NUMERIC = "NUMERIC"
BIGNUMERIC = "BIGNUMERIC"
BOOL = "BOOL"
DATE = "DATE"
TIMESTAMP = "TIMESTAMP"

NULLABLE = "NULLABLE"
REQUIRED = "REQUIRED"
REPEATED = "REPEATED"


class Kind:
    """How a Zoho value is projected into a column."""

    SCALAR = "scalar"
    LOOKUP_ID = "lookup_id"  # {"id": .., "name": ..} -> id
    LOOKUP_NAME = "lookup_name"  # {"id": .., "name": ..} -> name
    JSON = "json"  # complex value kept as JSON text
    META = "meta"  # pipeline-managed column


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    mode: str = NULLABLE
    source: str | None = None  # Zoho field api_name
    kind: str = Kind.SCALAR
    description: str | None = None


@dataclass(frozen=True)
class TableSpec:
    name: str
    zoho_module: str
    columns: tuple[Column, ...]
    key: str = "id"
    version_column: str | None = None
    parent_key: str | None = None  # set for subform (child) tables
    subform_field: str | None = None  # parent record key that holds the subform rows
    clustering: tuple[str, ...] = ("id",)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def data_columns(self) -> list[Column]:
        return [c for c in self.columns if c.kind != Kind.META]

    def column(self, name: str) -> Column | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None


@dataclass(frozen=True)
class ModulePlan:
    module: str
    parent: TableSpec
    children: tuple[TableSpec, ...]
    list_fields: tuple[str, ...]  # Zoho api names requested from the list API
    version_field: str | None  # Zoho api name used for incremental extraction
    requires_record_fetch: bool  # subforms / multi-lookups are only returned by GET /{module}/{id}
    skipped_fields: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()
