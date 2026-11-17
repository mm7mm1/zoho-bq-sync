"""Extraction strategy for one module.

- Modules with subforms or multi-select/multi-user lookups: list ids + Modified_Time,
  then GET /{module}/{id} per changed record (the only endpoint that returns those values).
- Other modules: list API with up to 50 fields; more fields are fetched in extra chunks
  by id (?ids=, 100 per call) and merged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, Sequence

from ..logging_setup import log_event
from ..zoho.records import MODIFIED_TIME, RecordPage, parse_zoho_datetime
from .checkpoint import PageMeta

logger = logging.getLogger(__name__)


class RecordsSource(Protocol):
    MAX_FIELDS_PER_REQUEST: int

    def iter_pages(self, module: str, fields: Sequence[str], *, modified_since: datetime | None = ..., sort_by: str = ..., sort_order: str = ...) -> Any: ...

    def fetch_by_ids(self, module: str, ids: Sequence[str], fields: Sequence[str]) -> list[dict[str, Any]]: ...

    def get_record(self, module: str, record_id: str) -> dict[str, Any] | None: ...


@dataclass
class Extraction:
    records: list[dict[str, Any]]
    pages: list[PageMeta]
    listed: int = 0
    detail_calls: int = 0
    vanished: list[str] = field(default_factory=list)  # listed but gone before the detail GET


def split_field_chunks(field_names: Sequence[str], version_field: str | None, max_per_request: int = 50) -> list[list[str]]:
    """Every chunk carries the version field so a record that changed between chunk calls is detectable."""
    if version_field is None:
        return [list(field_names[i : i + max_per_request]) for i in range(0, len(field_names), max_per_request)] or [[]]
    others = [f for f in field_names if f != version_field]
    size = max_per_request - 1
    chunks = [[version_field, *others[i : i + size]] for i in range(0, len(others), size)]
    return chunks or [[version_field]]


def extract_module(
    source: RecordsSource,
    module: str,
    list_fields: Sequence[str],
    *,
    requires_record_fetch: bool,
    since: datetime | None,
    until: datetime | None,
    version_field: str | None = MODIFIED_TIME,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Extraction:
    if requires_record_fetch:
        chunks = [[version_field or list_fields[0]]]
    else:
        chunks = split_field_chunks(list_fields, version_field, source.MAX_FIELDS_PER_REQUEST)

    sort_by = MODIFIED_TIME if version_field else "id"
    by_id: dict[str, dict[str, Any]] = {}
    pages: list[PageMeta] = []
    stop = False

    page: RecordPage
    for page in source.iter_pages(module, chunks[0], modified_since=since, sort_by=sort_by, sort_order="asc"):
        times: list[datetime] = []
        for record in page.records:
            modified = parse_zoho_datetime(record.get(version_field)) if version_field else None
            if modified is not None:
                times.append(modified)
            if until is not None and modified is not None and modified > until:
                stop = True  # sorted ascending: everything after this is outside the window
                continue
            by_id[str(record["id"])] = record
        pages.append(PageMeta(fetched_at=page.server_time or clock(), modified_times=tuple(times)))
        log_event(logger, "extraction_page", logging.DEBUG, module=module, page=page.page_number, records=len(page.records))
        if stop:
            break

    extraction = Extraction(records=[], pages=pages, listed=len(by_id))
    ids = list(by_id)

    if requires_record_fetch:
        for record_id in ids:
            full = source.get_record(module, record_id)
            extraction.detail_calls += 1
            if full is None:
                # Deleted between list and detail call; the deleted-records poll picks it up.
                extraction.vanished.append(record_id)
                continue
            extraction.records.append(full)
        if extraction.vanished:
            log_event(logger, "records_vanished_during_extraction", logging.WARNING, module=module, ids=extraction.vanished[:20])
        return extraction

    for chunk in chunks[1:]:
        for extra in source.fetch_by_ids(module, ids, chunk):
            target = by_id.get(str(extra.get("id")))
            if target is None:
                continue
            for key, value in extra.items():
                # Keep the first chunk's Modified_Time: if the record changed between chunk
                # calls, the older stamp guarantees the next run re-fetches it consistently.
                if key not in ("id", version_field):
                    target[key] = value
    extraction.records = list(by_id.values())
    return extraction
