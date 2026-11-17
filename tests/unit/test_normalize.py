import json
from datetime import datetime, timezone

import pytest

from tests.fakes import zf
from zoho_bq_sync.errors import CoercionError
from zoho_bq_sync.schema.builder import build_module_plan
from zoho_bq_sync.schema.model import Column
from zoho_bq_sync.transform.coerce import coerce_value
from zoho_bq_sync.transform.normalize import RunContext, to_child_rows, to_parent_row

CTX = RunContext("r20261117T100000Z_abc123", datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc))

PLAN = build_module_plan(
    "Wedding_Bookings",
    [
        zf("Name", "text", "10"),
        zf("Modified_Time", "datetime", "11"),
        zf("Owner", "ownerlookup", "12"),
        zf("Package_Price", "currency", "13"),
        zf("Guest_Count", "integer", "14"),
        zf("Wedding_Date", "date", "15"),
        zf("Documents_Verified", "boolean", "16"),
        zf("Languages", "multiselectpicklist", "17"),
        zf("Booked_Services", "subform", "18", associated_module="Booked_Services"),
    ],
    {"Booked_Services": [zf("Service_Name", "text", "20"), zf("Unit_Price", "currency", "21"), zf("Quantity", "integer", "22"), zf("Parent_Id", "lookup", "23")]},
)

# MOCK record in the shape returned by GET /crm/v8/Wedding_Bookings/{id}
RECORD = {
    "id": "5843104000000123001",
    "Name": "WB-SYN-0001 Olena & Marco - Tbilisi",
    "Modified_Time": "2026-11-17T12:30:00+02:00",
    "Owner": {"name": "Demo Owner", "id": "5843104000000000001", "email": "owner@example.com"},
    "Package_Price": 2450.5,
    "Guest_Count": 12,
    "Wedding_Date": "2027-06-14",
    "Documents_Verified": True,
    "Languages": ["English", "Georgian"],
    "Booked_Services": [
        {"id": "900001", "Service_Name": "Civil registration", "Unit_Price": 650, "Quantity": 1, "Parent_Id": {"id": "5843104000000123001", "name": "x"}},
        {"id": "900002", "Service_Name": "Certified translation", "Unit_Price": 32.5, "Quantity": 6},
    ],
}


def test_parent_row_types_and_metadata():
    row = to_parent_row(RECORD, PLAN.parent, CTX, ["Booked_Services"])
    assert row["id"] == "5843104000000123001"
    assert row["modified_time"] == "2026-11-17T10:30:00+00:00"  # normalized to UTC
    assert row["owner_id"] == "5843104000000000001" and row["owner_name"] == "Demo Owner"
    assert row["package_price"] == "2450.5"  # NUMERIC as exact decimal string
    assert row["guest_count"] == 12 and row["wedding_date"] == "2027-06-14" and row["documents_verified"] is True
    assert row["languages"] == ["English", "Georgian"]
    assert row["_run_id"] == CTX.run_id and row["_is_deleted"] is False and row["_deleted_at"] is None
    raw = json.loads(row["_raw_json"])
    assert "Booked_Services" not in raw and raw["Name"] == RECORD["Name"]


def test_missing_values_are_null_and_repeated_empty():
    row = to_parent_row({"id": "1"}, PLAN.parent, CTX)
    assert row["package_price"] is None and row["owner_id"] is None and row["languages"] == []


def test_subform_rows_carry_parent_fk_index_and_version():
    parent = to_parent_row(RECORD, PLAN.parent, CTX, ["Booked_Services"])
    rows = to_child_rows(RECORD, PLAN.children[0], CTX, parent["modified_time"])
    assert [(r["id"], r["parent_id"], r["_row_index"]) for r in rows] == [
        ("900001", "5843104000000123001", 0),
        ("900002", "5843104000000123001", 1),
    ]
    assert all(r["_parent_modified_time"] == "2026-11-17T10:30:00+00:00" for r in rows)
    assert rows[1]["unit_price"] == "32.5" and rows[1]["quantity"] == 6
    assert "parent_id_id" not in rows[0]


def test_empty_or_missing_subform_yields_no_rows():
    assert to_child_rows({"id": "1", "Booked_Services": []}, PLAN.children[0], CTX, None) == []
    assert to_child_rows({"id": "1"}, PLAN.children[0], CTX, None) == []


def test_subform_row_without_id_fails_loudly():
    with pytest.raises(CoercionError, match="has no id"):
        to_child_rows({"id": "1", "Booked_Services": [{"Service_Name": "x"}]}, PLAN.children[0], CTX, None)


def test_bad_value_error_names_record_and_field():
    with pytest.raises(CoercionError) as exc:
        to_parent_row({"id": "77", "Guest_Count": "twelve"}, PLAN.parent, CTX)
    assert "77" in str(exc.value) and "Guest_Count" in str(exc.value)


@pytest.mark.parametrize(
    "value,bq_type,expected",
    [
        ("12", "INT64", 12),
        (12.0, "INT64", 12),
        ("", "INT64", None),
        ("1.23456789012", "NUMERIC", "1.234567890"),
        (0.1, "NUMERIC", "0.1"),
        ("true", "BOOL", True),
        ({"a": 1}, "STRING", '{"a":1}'),
        (False, "STRING", "false"),
        ("2026-11-17T10:00:00Z", "TIMESTAMP", "2026-11-17T10:00:00+00:00"),
        ("2027-06-14", "DATE", "2027-06-14"),
    ],
)
def test_coercion(value, bq_type, expected):
    assert coerce_value(value, Column("c", bq_type)) == expected


@pytest.mark.parametrize("value,bq_type", [(True, "INT64"), ("1.5", "INT64"), ("x", "NUMERIC"), (float("nan"), "FLOAT64"), ("14/06/2027", "DATE")])
def test_coercion_rejects(value, bq_type):
    with pytest.raises(CoercionError):
        coerce_value(value, Column("c", bq_type))
