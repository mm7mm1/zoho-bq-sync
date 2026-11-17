"""Module and field discovery via the Zoho metadata APIs.

GET /crm/v8/settings/modules             -> {"modules": [...]}
GET /crm/v8/settings/fields?module=<api> -> {"fields": [...]}
A subform field (data_type "subform") names its subform module in associated_module.module;
calling the fields API with that name returns the subform's own fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..errors import ConfigError, ZohoApiError
from .client import ZohoClient

# Modules that hold business records. "subform" modules are synced through their parent,
# "linking" modules back multi-select lookups, "web" modules are embedded web tabs.
SYNCABLE_GENERATED_TYPES = frozenset({"default", "custom"})


@dataclass(frozen=True)
class ZohoModule:
    api_name: str
    plural_label: str = ""
    generated_type: str = "default"
    api_supported: bool = True
    module_id: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "ZohoModule":
        return cls(
            api_name=str(data["api_name"]),
            plural_label=str(data.get("plural_label") or data.get("module_name") or data["api_name"]),
            generated_type=str(data.get("generated_type") or "default"),
            api_supported=bool(data.get("api_supported", True)),
            module_id=str(data["id"]) if data.get("id") is not None else None,
        )

    @property
    def syncable(self) -> bool:
        return self.api_supported and self.generated_type in SYNCABLE_GENERATED_TYPES


@dataclass(frozen=True)
class ZohoField:
    api_name: str
    data_type: str
    json_type: str | None = None
    field_id: str | None = None
    label: str | None = None
    custom_field: bool = False
    return_type: str | None = None  # formula / rollup_summary result type
    associated_module: str | None = None  # subform module api name for subform fields
    decimal_place: int | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "ZohoField":
        data_type = str(data.get("data_type") or "")
        formula = data.get("formula") if isinstance(data.get("formula"), dict) else {}
        rollup = data.get("rollup_summary") if isinstance(data.get("rollup_summary"), dict) else {}
        assoc = data.get("associated_module") if isinstance(data.get("associated_module"), dict) else {}
        associated = assoc.get("module") or assoc.get("api_name")
        if not associated and data_type == "subform" and isinstance(data.get("subform"), dict):
            associated = data["subform"].get("module")  # shape used by older API versions
        decimal_place = data.get("decimal_place")
        return cls(
            api_name=str(data["api_name"]),
            data_type=data_type,
            json_type=data.get("json_type"),
            field_id=str(data["id"]) if data.get("id") is not None else None,
            label=data.get("field_label") or data.get("display_label"),
            custom_field=bool(data.get("custom_field", False)),
            return_type=formula.get("return_type") or rollup.get("return_type"),
            associated_module=str(associated) if associated else None,
            decimal_place=int(decimal_place) if isinstance(decimal_place, int) else None,
        )

    @property
    def is_subform(self) -> bool:
        return self.data_type.lower() == "subform"


class MetadataClient:
    def __init__(self, client: ZohoClient) -> None:
        self._client = client

    def list_modules(self) -> list[ZohoModule]:
        response = self._client.get("settings/modules")
        modules = (response.body or {}).get("modules")
        if not isinstance(modules, list):
            raise ZohoApiError("Unexpected modules metadata response: 'modules' missing", path="settings/modules")
        return [ZohoModule.from_api(m) for m in modules if m.get("api_name")]

    def get_fields(self, module_api_name: str) -> list[ZohoField]:
        response = self._client.get("settings/fields", params={"module": module_api_name})
        fields = (response.body or {}).get("fields")
        if not isinstance(fields, list):
            raise ZohoApiError(
                f"Unexpected fields metadata response for {module_api_name}: 'fields' missing",
                path="settings/fields",
            )
        return [ZohoField.from_api(f) for f in fields if f.get("api_name")]


def select_modules(
    available: Iterable[ZohoModule],
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
) -> list[ZohoModule]:
    """Explicit include list wins; otherwise every syncable standard and custom module."""
    available = list(available)
    by_name = {m.api_name: m for m in available}
    include = [name for name in include if name and name != "*"]
    excluded = set(exclude)

    if include:
        unknown = [n for n in include if n not in by_name]
        if unknown:
            raise ConfigError(f"Unknown Zoho module(s): {', '.join(unknown)}")
        not_syncable = [n for n in include if not by_name[n].syncable]
        if not_syncable:
            raise ConfigError(
                f"Module(s) not syncable through the Records API (subform/linking/web or api_supported=false): "
                f"{', '.join(not_syncable)}"
            )
        return [by_name[n] for n in include if n not in excluded]

    return sorted((m for m in available if m.syncable and m.api_name not in excluded), key=lambda m: m.api_name)
