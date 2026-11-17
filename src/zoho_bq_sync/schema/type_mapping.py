"""Zoho data_type -> BigQuery column type.

Decisions:
- currency/decimal -> NUMERIC (exact decimal arithmetic for money).
- lookups -> two STRING columns (<field>_id, <field>_name): ids are joinable, names readable.
- multiselectpicklist -> REPEATED STRING (queryable with UNNEST).
- multi-lookups, file/image uploads and unknown objects -> STRING holding JSON text.
  STRING (not the JSON type) because it loads identically through load jobs and MERGE,
  and JSON_VALUE()/JSON_QUERY() accept JSON-formatted strings.
- formula / rollup_summary -> their declared return_type.
- anything unknown -> derived from json_type, else JSON text, with a warning. A new Zoho
  field type therefore never breaks the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import BOOL, DATE, FLOAT64, INT64, NULLABLE, NUMERIC, REPEATED, STRING, TIMESTAMP, Kind
from ..zoho.metadata import ZohoField

SCALAR_TYPES: dict[str, str] = {
    "text": STRING,
    "textarea": STRING,
    "email": STRING,
    "phone": STRING,
    "website": STRING,
    "picklist": STRING,
    "autonumber": STRING,
    "profileimage": STRING,
    "event_reminder": STRING,
    "integer": INT64,
    "bigint": INT64,
    "long": INT64,
    "double": FLOAT64,
    "percent": FLOAT64,
    "currency": NUMERIC,
    "decimal": NUMERIC,
    "boolean": BOOL,
    "date": DATE,
    "datetime": TIMESTAMP,
}

LOOKUP_TYPES = frozenset({"lookup", "ownerlookup", "userlookup"})
REPEATED_STRING_TYPES = frozenset({"multiselectpicklist"})
JSON_TYPES = frozenset(
    {
        "multiselectlookup",
        "multiuserlookup",
        "multi_module_lookup",
        "fileupload",
        "imageupload",
        "consent_lookup",
        "rrule",
        "alarm",
    }
)
# Values of these types are only returned by GET /{module}/{id}, not by the list API.
DETAIL_ONLY_TYPES = frozenset({"subform", "multiselectlookup", "multiuserlookup"})
COMPUTED_TYPES = frozenset({"formula", "rollup_summary"})

RETURN_TYPES: dict[str, str] = {
    "text": STRING,
    "string": STRING,
    "integer": INT64,
    "double": FLOAT64,
    "percent": FLOAT64,
    "currency": NUMERIC,
    "decimal": NUMERIC,
    "boolean": BOOL,
    "date": DATE,
    "datetime": TIMESTAMP,
}

JSON_TYPE_FALLBACK: dict[str, tuple[str, str]] = {
    "string": (STRING, Kind.SCALAR),
    "integer": (INT64, Kind.SCALAR),
    "double": (FLOAT64, Kind.SCALAR),
    "boolean": (BOOL, Kind.SCALAR),
    "jsonobject": (STRING, Kind.JSON),
    "jsonarray": (STRING, Kind.JSON),
}


@dataclass(frozen=True)
class MappedColumn:
    suffix: str | None  # None -> base column name; "id" / "name" for lookups
    type: str
    mode: str = NULLABLE
    kind: str = Kind.SCALAR


@dataclass(frozen=True)
class FieldMapping:
    columns: tuple[MappedColumn, ...]
    warning: str | None = None


def map_field(field: ZohoField) -> FieldMapping:
    data_type = field.data_type.strip().lower()
    if data_type == "subform":
        raise ValueError("subform fields map to a child table, not to a column")

    if data_type in COMPUTED_TYPES:
        return_type = (field.return_type or "").lower()
        if return_type in RETURN_TYPES:
            return FieldMapping((MappedColumn(None, RETURN_TYPES[return_type]),))
        return _fallback(field, f"{field.api_name}: {data_type} with unknown return_type {field.return_type!r}")

    if data_type in SCALAR_TYPES:
        return FieldMapping((MappedColumn(None, SCALAR_TYPES[data_type]),))
    if data_type in LOOKUP_TYPES:
        return FieldMapping(
            (
                MappedColumn("id", STRING, kind=Kind.LOOKUP_ID),
                MappedColumn("name", STRING, kind=Kind.LOOKUP_NAME),
            )
        )
    if data_type in REPEATED_STRING_TYPES:
        return FieldMapping((MappedColumn(None, STRING, mode=REPEATED),))
    if data_type in JSON_TYPES:
        return FieldMapping((MappedColumn(None, STRING, kind=Kind.JSON),))
    return _fallback(field, f"{field.api_name}: unknown Zoho data_type {field.data_type!r}")


def _fallback(field: ZohoField, reason: str) -> FieldMapping:
    json_type = (field.json_type or "").lower()
    if json_type in JSON_TYPE_FALLBACK:
        bq_type, kind = JSON_TYPE_FALLBACK[json_type]
        return FieldMapping((MappedColumn(None, bq_type, kind=kind),), warning=f"{reason}; mapped via json_type={json_type}")
    return FieldMapping((MappedColumn(None, STRING, kind=Kind.JSON),), warning=f"{reason}; stored as JSON text")
