from datetime import datetime, timezone

import pytest

from tests.fakes import FakeResponse, FakeSession, StaticAuth
from zoho_bq_sync.errors import ZohoApiError
from zoho_bq_sync.sync.extract import extract_module, split_field_chunks
from zoho_bq_sync.zoho.client import ZohoClient
from zoho_bq_sync.zoho.records import RecordsClient, format_zoho_datetime

UTC = timezone.utc
DATE = {"Date": "Tue, 17 Nov 2026 10:00:00 GMT"}


def page(ids, more, token=None, start_minute=0):
    info = {"more_records": more, "per_page": 200}
    if token:
        info["next_page_token"] = token
    data = [{"id": str(i), "Modified_Time": f"2026-11-17T09:{(start_minute + n) % 60:02d}:00+00:00"} for n, i in enumerate(ids)]
    return FakeResponse(200, {"data": data, "info": info}, DATE)


def records_client(responses, per_page=200):
    session = FakeSession(responses)
    client = ZohoClient(StaticAuth(), "https://www.zohoapis.com", session=session, sleep=lambda s: None)
    return RecordsClient(client, per_page=per_page), session


def test_pages_until_more_records_false_with_incremental_header():
    rc, session = records_client([page([1, 2], True), page([3], False)])
    since = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
    pages = list(rc.iter_pages("Wedding_Bookings", ["Modified_Time", "Name"], modified_since=since))

    assert [len(p.records) for p in pages] == [2, 1]
    assert [c["params"]["page"] for c in session.calls] == [1, 2]
    first = session.calls[0]
    assert first["params"]["fields"] == "Modified_Time,Name"
    assert first["params"]["sort_by"] == "Modified_Time" and first["params"]["sort_order"] == "asc"
    assert first["headers"]["If-Modified-Since"] == "2026-11-16T08:00:00+00:00"


def test_switches_to_page_token_beyond_2000_records():
    responses = [page([p], True, token=f"tok{p}") for p in range(1, 11)] + [page([11], True, token="tok11"), page([12], False)]
    rc, session = records_client(responses)
    pages = list(rc.iter_pages("Leads", ["Modified_Time"]))

    assert len(pages) == 12
    pages_param = [c["params"].get("page") for c in session.calls]
    assert pages_param[:10] == list(range(1, 11))
    # 10 x 200 = 2000 records reached with `page`; the rest must use page_token.
    assert "page" not in session.calls[10]["params"] and session.calls[10]["params"]["page_token"] == "tok10"
    assert session.calls[11]["params"]["page_token"] == "tok11"


def test_missing_page_token_beyond_limit_fails_loudly():
    responses = [page([p], True) for p in range(1, 11)]
    rc, _ = records_client(responses)
    with pytest.raises(ZohoApiError, match="next_page_token"):
        list(rc.iter_pages("Leads", ["Modified_Time"]))


def test_not_modified_yields_no_pages():
    rc, _ = records_client([FakeResponse(304)])
    assert list(rc.iter_pages("Leads", ["Modified_Time"], modified_since=datetime(2026, 1, 1, tzinfo=UTC))) == []


def test_more_than_50_fields_rejected_by_client():
    rc, _ = records_client([])
    with pytest.raises(ValueError, match="50"):
        list(rc.iter_pages("Leads", [f"F{i}" for i in range(51)]))


def test_field_chunks_always_include_version_field():
    fields = ["Modified_Time"] + [f"F{i}" for i in range(120)]
    chunks = split_field_chunks(fields, "Modified_Time", 50)
    assert all(c[0] == "Modified_Time" and len(c) <= 50 for c in chunks)
    assert sorted(f for c in chunks for f in c[1:]) == sorted(fields[1:])
    assert len(chunks) == 3


class ChunkSource:
    MAX_FIELDS_PER_REQUEST = 50

    def __init__(self):
        self.by_ids_calls = []

    def iter_pages(self, module, fields, **kw):
        from zoho_bq_sync.zoho.records import RecordPage

        yield RecordPage([{"id": "1", "Modified_Time": "2026-11-17T09:00:00+00:00", "F0": "a"}], datetime(2026, 11, 17, 10, tzinfo=UTC), 1)

    def fetch_by_ids(self, module, ids, fields):
        self.by_ids_calls.append(list(fields))
        return [{"id": "1", "Modified_Time": "2026-11-17T09:30:00+00:00", **{f: f"v-{f}" for f in fields if f != "Modified_Time"}}]

    def get_record(self, module, record_id):
        raise AssertionError("list strategy must not fetch single records")


def test_extra_field_chunks_merged_by_id_keeping_first_version():
    fields = ["Modified_Time"] + [f"F{i}" for i in range(60)]
    source = ChunkSource()
    extraction = extract_module(source, "Leads", fields, requires_record_fetch=False, since=None, until=None)
    record = extraction.records[0]
    assert record["F59"] == "v-F59" and record["F0"] == "a"
    # The first chunk's version stamp wins so a mid-extraction edit is re-fetched next run.
    assert record["Modified_Time"] == "2026-11-17T09:00:00+00:00"
    assert len(source.by_ids_calls) == 1


def test_by_ids_requests_are_chunked_by_100():
    responses = [FakeResponse(200, {"data": []}, DATE), FakeResponse(200, {"data": []}, DATE), FakeResponse(200, {"data": []}, DATE)]
    rc, session = records_client(responses)
    rc.fetch_by_ids("Leads", [str(i) for i in range(250)], ["Modified_Time", "Email"])
    assert [len(c["params"]["ids"].split(",")) for c in session.calls] == [100, 100, 50]


def test_until_stops_paging_early():
    responses = [page([1, 2, 3], True, start_minute=0), page([4], False, start_minute=3)]
    rc, session = records_client(responses)
    extraction = extract_module(
        rc,
        "Leads",
        ["Modified_Time"],
        requires_record_fetch=False,
        since=None,
        until=datetime(2026, 11, 17, 9, 1, tzinfo=UTC),
    )
    assert [r["id"] for r in extraction.records] == ["1", "2"]
    assert len(session.calls) == 1  # second page never requested


def test_deleted_records_paginate_and_filter():
    since = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
    responses = [
        FakeResponse(200, {"data": [{"id": "9", "deleted_time": "2026-11-17T08:30:00+00:00", "type": "recycle"}], "info": {"more_records": True}}, DATE),
        FakeResponse(200, {"data": [{"id": "7", "deleted_time": "2026-11-16T08:30:00+00:00", "type": "permanent"}], "info": {"more_records": False}}, DATE),
    ]
    rc, session = records_client(responses)
    items, server_time = rc.list_deleted("Leads", since)
    assert [i["id"] for i in items] == ["9"]
    assert session.calls[0]["url"].endswith("/Leads/deleted") and session.calls[0]["params"]["type"] == "all"
    assert session.calls[0]["headers"]["If-Modified-Since"] == format_zoho_datetime(since)
    assert server_time == datetime(2026, 11, 17, 10, tzinfo=UTC)


def test_count_endpoint():
    rc, session = records_client([FakeResponse(200, {"count": 42})])
    assert rc.count("Leads") == 42
    assert session.calls[0]["url"].endswith("/Leads/actions/count")


def test_path_injection_guard():
    rc, _ = records_client([])
    with pytest.raises(ValueError):
        rc.get_record("Leads", "1/../../settings")
