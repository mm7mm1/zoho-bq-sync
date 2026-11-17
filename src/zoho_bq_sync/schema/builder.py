"""Build table specs for a module (parent table + one child table per subform)."""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from .model import BOOL, INT64, REQUIRED, STRING, TIMESTAMP, Column, Kind, ModulePlan, TableSpec
from .naming import NameAllocator, assert_safe_identifier, normalize_identifier
from .type_mapping import DETAIL_ONLY_TYPES, map_field
from ..zoho.metadata import ZohoField
from ..zoho.records import MODIFIED_TIME

PARENT_FK = "parent_id"
CHILD_VERSION_COLUMN = "_parent_modified_time"

# Subform rows carry a Parent_Id lookup; the pipeline owns the foreign key column instead.
SUBFORM_MANAGED_FIELDS = frozenset({"id", "Parent_Id"})
PARENT_MANAGED_FIELDS = frozenset({"id"})


def meta_tail(store_raw_json: bool) -> list[Column]:
    columns = [
        Column("_synced_at", TIMESTAMP, kind=Kind.META, description="When this row version was written"),
        Column("_run_id", STRING, kind=Kind.META, description="Sync run that last wrote this row"),
        Column("_is_deleted", BOOL, kind=Kind.META, description="Soft-delete flag set by reconciliation"),
        Column("_deleted_at", TIMESTAMP, kind=Kind.META, description="Deletion time reported by Zoho or detected"),
    ]
    if store_raw_json:
        columns.append(Column("_raw_json", STRING, kind=Kind.META, description="Source record as JSON text"))
    return columns


def parent_head() -> list[Column]:
    return [Column("id", STRING, REQUIRED, kind=Kind.META, description="Zoho record id")]


def child_head() -> list[Column]:
    return [
        Column("id", STRING, REQUIRED, kind=Kind.META, description="Zoho subform row id"),
        Column(PARENT_FK, STRING, REQUIRED, kind=Kind.META, description="Zoho id of the parent record"),
        Column("_row_index", INT64, kind=Kind.META, description="Position of the row inside the subform"),
        Column(CHILD_VERSION_COLUMN, TIMESTAMP, kind=Kind.META, description="Parent Modified_Time of this row version"),
    ]


def _field_order(field: ZohoField) -> tuple:
    fid = field.field_id or ""
    return (0 if fid.isdigit() else 1, int(fid) if fid.isdigit() else 0, fid, field.api_name)


def _disambiguator(field: ZohoField) -> str:
    fid = "".join(ch for ch in (field.field_id or "") if ch.isalnum())
    return fid[-6:] if fid else normalize_identifier(field.api_name)[:12]


def _build_columns(
    module: str,
    fields: Sequence[ZohoField],
    head: list[Column],
    tail: list[Column],
    managed: frozenset[str],
    exclude: frozenset[str],
) -> tuple[list[Column], list[tuple[str, str]], list[str]]:
    allocator = NameAllocator(c.name for c in head + tail)
    columns: list[Column] = []
    skipped: list[tuple[str, str]] = []
    warnings: list[str] = []
    for field in sorted(fields, key=_field_order):
        if field.api_name in managed:
            continue
        if field.is_subform:
            continue  # becomes its own child table
        if f"{module}.{field.api_name}" in exclude:
            skipped.append((field.api_name, "excluded by ZOHO_EXCLUDE_FIELDS"))
            continue
        mapping = map_field(field)
        if mapping.warning:
            warnings.append(f"{module}.{mapping.warning}")
        base = normalize_identifier(field.api_name)
        for mapped in mapping.columns:
            desired = base if mapped.suffix is None else f"{base}_{mapped.suffix}"
            name = allocator.allocate(desired, _disambiguator(field))
            columns.append(
                Column(
                    name=name,
                    type=mapped.type,
                    mode=mapped.mode,
                    source=field.api_name,
                    kind=mapped.kind,
                    description=f"Zoho {field.label or field.api_name} ({field.data_type})"[:1024],
                )
            )
    return head + columns + tail, skipped, warnings


def table_name_for_module(module: str, overrides: Mapping[str, str]) -> str:
    return assert_safe_identifier(overrides.get(module) or normalize_identifier(module))


def table_name_for_subform(parent_table: str, subform_field: str, overrides: Mapping[str, str]) -> str:
    return assert_safe_identifier(overrides.get(subform_field) or f"{parent_table}_{normalize_identifier(subform_field)}")


def build_module_plan(
    module: str,
    fields: Sequence[ZohoField],
    subform_fields: Mapping[str, Sequence[ZohoField]],
    *,
    table_name_overrides: Mapping[str, str] | None = None,
    exclude_fields: Iterable[str] = (),
    store_raw_json: bool = True,
) -> ModulePlan:
    overrides = dict(table_name_overrides or {})
    exclude = frozenset(exclude_fields)
    parent_table = table_name_for_module(module, overrides)

    parent_columns, skipped, warnings = _build_columns(
        module, fields, parent_head(), meta_tail(store_raw_json), PARENT_MANAGED_FIELDS, exclude
    )
    version_column = next(
        (c.name for c in parent_columns if c.source == MODIFIED_TIME and c.type == TIMESTAMP and c.kind == Kind.SCALAR),
        None,
    )
    parent = TableSpec(
        name=parent_table,
        zoho_module=module,
        columns=tuple(parent_columns),
        version_column=version_column,
        clustering=("id",),
    )

    children: list[TableSpec] = []
    for field in sorted((f for f in fields if f.is_subform), key=_field_order):
        if f"{module}.{field.api_name}" in exclude:
            skipped.append((field.api_name, "excluded by ZOHO_EXCLUDE_FIELDS"))
            continue
        sub_fields = subform_fields.get(field.api_name)
        if sub_fields is None:
            raise ValueError(f"Subform metadata for {module}.{field.api_name} was not loaded")
        child_columns, child_skipped, child_warnings = _build_columns(
            field.api_name, sub_fields, child_head(), meta_tail(store_raw_json), SUBFORM_MANAGED_FIELDS, exclude
        )
        skipped.extend((f"{field.api_name}.{name}", reason) for name, reason in child_skipped)
        warnings.extend(child_warnings)
        children.append(
            TableSpec(
                name=table_name_for_subform(parent_table, field.api_name, overrides),
                zoho_module=field.associated_module or field.api_name,
                columns=tuple(child_columns),
                version_column=CHILD_VERSION_COLUMN,
                parent_key=PARENT_FK,
                subform_field=field.api_name,
                clustering=(PARENT_FK, "id"),
            )
        )

    list_fields = tuple(dict.fromkeys(c.source for c in parent_columns if c.source))
    excluded_names = {name for name, _ in skipped}
    requires_record_fetch = bool(children) or any(
        f.data_type.lower() in DETAIL_ONLY_TYPES and f.api_name not in excluded_names for f in fields
    )
    return ModulePlan(
        module=module,
        parent=parent,
        children=tuple(children),
        list_fields=list_fields,
        version_field=MODIFIED_TIME if version_column else None,
        requires_record_fetch=requires_record_fetch,
        skipped_fields=tuple(skipped),
        warnings=tuple(warnings),
    )
