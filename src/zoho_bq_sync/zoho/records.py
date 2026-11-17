"""Records, deleted-records and record-count endpoints.

GET /crm/v8/{module}?fields=..&per_page=200&sort_by=Modified_Time&sort_order=asc
    + header If-Modified-Since          -> changed records (list API)
    `fields` is mandatory (max 50); `page` reaches the first 2000 records, beyond that
    `page_token` (from info.next_page_token, bound to the same params, valid 24h,
    up to 100,000 records in total) is required.
GET /crm/v8/{module}?ids=..&fields=..  -> up to 100 specific records (extra field chunks)
GET /crm/v8/{module}/{id}               -> one record; the only call that returns subform
                                           rows and multi-select / multi-user lookup values
GET /crm/v8/{module}/deleted?type=all   -> recycle-bin (60 days) and permanently deleted
                                           (120 days) records, with If-Modified-Since
GET /crm/v8/{module}/actions/count      -> record count for reconciliation
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence
from urllib.parse import quote

from ..errors import ZohoApiError
from ..logging_setup import log_event
from .client import ZohoClient

logger = logging.getLogger(__name__)

MODIFIED_TIME = "Modified_Time"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_SAFE_ID = re.compile(r"^[0-9A-Za-z_-]+$")


def format_zoho_datetime(value: datetime) -> str:
    """ISO 8601 with an explicit offset, as Zoho expects in If-Modified-Since."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_zoho_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _module_path(module: str) -> str:
    if not _SAFE_NAME.match(module):
        raise ValueError(f"Invalid Zoho module api name: {module!r}")
    return module


@dataclass(frozen=True)
class RecordPage:
    records: list[dict[str, Any]]
    server_time: datetime | None
    page_number: int


class RecordsClient:
    MAX_FIELDS_PER_REQUEST = 50
    MAX_IDS_PER_REQUEST = 100
    PAGE_PARAM_RECORD_LIMIT = 2000

    def __init__(self, client: ZohoClient, per_page: int = 200) -> None:
        self._client = client
        self.per_page = per_page

    @property
    def api_calls(self) -> int:
        return self._client.calls_made

    def iter_pages(
        self,
        module: str,
        fields: Sequence[str],
        *,
        modified_since: datetime | None = None,
        sort_by: str = MODIFIED_TIME,
        sort_order: str = "asc",
    ) -> Iterator[RecordPage]:
        if not fields:
            raise ValueError("fields is mandatory for the Zoho list records API")
        if len(fields) > self.MAX_FIELDS_PER_REQUEST:
            raise ValueError(f"Zoho accepts at most {self.MAX_FIELDS_PER_REQUEST} fields per request")
        path = _module_path(module)
        base_params = {
            "fields": ",".join(fields),
            "per_page": self.per_page,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
        headers = {"If-Modified-Since": format_zoho_datetime(modified_since)} if modified_since else None

        page = 1
        page_token: str | None = None
        while True:
            params = dict(base_params)
            if page_token:
                params["page_token"] = page_token
            else:
                params["page"] = page
            response = self._client.get(path, params=params, headers=headers)
            if response.body is None:  # 204 / 304
                return
            data = response.body.get("data") or []
            info = response.body.get("info") or {}
            yield RecordPage(records=list(data), server_time=response.server_time, page_number=page)

            if not info.get("more_records"):
                return
            next_token = info.get("next_page_token")
            if page_token is not None or page * self.per_page >= self.PAGE_PARAM_RECORD_LIMIT:
                if not next_token:
                    raise ZohoApiError(
                        "more_records=true beyond the 2000-record page limit but no next_page_token returned",
                        path=path,
                    )
                page_token = str(next_token)
            page += 1

    def fetch_by_ids(self, module: str, ids: Sequence[str], fields: Sequence[str]) -> list[dict[str, Any]]:
        path = _module_path(module)
        result: list[dict[str, Any]] = []
        for start in range(0, len(ids), self.MAX_IDS_PER_REQUEST):
            chunk = ids[start : start + self.MAX_IDS_PER_REQUEST]
            response = self._client.get(path, params={"ids": ",".join(chunk), "fields": ",".join(fields)})
            if response.body:
                result.extend(response.body.get("data") or [])
        return result

    def get_record(self, module: str, record_id: str) -> dict[str, Any] | None:
        if not _SAFE_ID.match(record_id):
            raise ValueError(f"Invalid Zoho record id: {record_id!r}")
        response = self._client.get(f"{_module_path(module)}/{quote(record_id)}")
        if not response.body:
            return None
        data = response.body.get("data") or []
        return data[0] if data else None

    def list_deleted(self, module: str, since: datetime | None) -> tuple[list[dict[str, Any]], datetime | None]:
        """Return deleted records (recycle + permanent) and the server time of the first page."""
        path = f"{_module_path(module)}/deleted"
        headers = {"If-Modified-Since": format_zoho_datetime(since)} if since else None
        records: list[dict[str, Any]] = []
        first_server_time: datetime | None = None
        page = 1
        while True:
            response = self._client.get(path, params={"type": "all", "page": page, "per_page": self.per_page}, headers=headers)
            if first_server_time is None:
                first_server_time = response.server_time
            if response.body is None:
                break
            data = response.body.get("data") or []
            for item in data:
                deleted_at = parse_zoho_datetime(item.get("deleted_time"))
                # Client-side filter as well: the header semantics for this endpoint are
                # "modified since", and an overlap window is applied by the caller anyway.
                if since is None or deleted_at is None or deleted_at >= since:
                    records.append(item)
            if not (response.body.get("info") or {}).get("more_records"):
                break
            page += 1
        log_event(logger, "zoho_deleted_records_fetched", module=module, count=len(records), pages=page)
        return records, first_server_time

    def count(self, module: str) -> int:
        response = self._client.get(f"{_module_path(module)}/actions/count")
        body = response.body or {}
        value = body.get("count")
        if not isinstance(value, int):
            raise ZohoApiError("Unexpected record count response: 'count' missing", path=f"{module}/actions/count")
        return value
