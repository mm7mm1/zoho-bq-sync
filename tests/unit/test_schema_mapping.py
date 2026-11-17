import pytest

from tests.fakes import zf
from zoho_bq_sync.schema.builder import build_module_plan
from zoho_bq_sync.schema.model import Kind
from zoho_bq_sync.schema.naming import NameAllocator, assert_safe_identifier, normalize_identifier
from zoho_bq_sync.schema.type_mapping import map_field


@pytest.mark.parametrize(
    "data_type,expected",
    [
        ("text", "STRING"),
        ("picklist", "STRING"),
        ("email", "STRING"),
        ("integer", "INT64"),
        ("bigint", "INT64"),
        ("double", "FLOAT64"),
        ("percent", "FLOAT64"),
        ("currency", "NUMERIC"),
        ("decimal", "NUMERIC"),
        ("boolean", "BOOL"),
        ("date", "DATE"),
        ("datetime", "TIMESTAMP"),
        ("autonumber", "STRING"),
    ],
)
def test_scalar_type_mapping(data_type, expected):
    mapping = map_field(zf("X", data_type, "1"))
    assert [(c.type, c.mode, c.kind) for c in mapping.columns] == [(expected, "NULLABLE", Kind.SCALAR)]
    assert mapping.warning is None


def test_lookup_expands_to_id_and_name():
    mapping = map_field(zf("Owner", "ownerlookup", "1"))
    assert [(c.suffix, c.type, c.kind) for c in mapping.columns] == [("id", "STRING", Kind.LOOKUP_ID), ("name", "STRING", Kind.LOOKUP_NAME)]


def test_multiselect_is_repeated_and_multilookup_is_json():
    assert map_field(zf("Tags", "multiselectpicklist", "1")).columns[0].mode == "REPEATED"
    assert map_field(zf("Langs", "multiselectlookup", "1")).columns[0].kind == Kind.JSON


def test_formula_uses_return_type():
    assert map_field(zf("Margin", "formula", "1", return_type="currency")).columns[0].type == "NUMERIC"
    assert map_field(zf("Days", "rollup_summary", "1", return_type="integer")).columns[0].type == "INT64"


def test_unknown_type_falls_back_with_warning():
    via_json = map_field(zf("Future", "hologram", "1", json_type="double"))
    assert via_json.columns[0].type == "FLOAT64" and "json_type" in via_json.warning
    opaque = map_field(zf("Future", "hologram", "1"))
    assert opaque.columns[0].kind == Kind.JSON and "JSON text" in opaque.warning


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Wedding_Date", "wedding_date"),
        ("Modified_Time", "modified_time"),
        ("$approval_state", "approval_state"),
        ("2nd_Partner", "f_2nd_partner"),
        ("Ceremony City!", "ceremony_city_"),
        ("___", "field"),
    ],
)
def test_normalize_identifier(raw, expected):
    assert normalize_identifier(raw) == expected


def test_name_allocator_is_deterministic_and_collision_free():
    alloc = NameAllocator(["id"])
    assert alloc.allocate("email", "000101") == "email"
    assert alloc.allocate("email", "000102") == "email_000102"
    assert alloc.allocate("id", "000103") == "id_000103"


def test_unsafe_identifier_rejected():
    with pytest.raises(ValueError):
        assert_safe_identifier("name`; DROP TABLE x; --")


def _wedding_fields():
    return [
        zf("Name", "text", "10"),
        zf("Modified_Time", "datetime", "11"),
        zf("Owner", "ownerlookup", "12"),
        zf("Package_Price", "currency", "13"),
        zf("Booked_Services", "subform", "14", associated_module="Booked_Services"),
        zf("EMAIL", "email", "16"),
        zf("Email", "email", "15"),
    ]


def _service_fields():
    return [
        zf("Service_Name", "text", "20"),
        zf("Unit_Price", "currency", "21"),
        zf("Parent_Id", "lookup", "22"),
        zf("id", "bigint", "23"),
    ]


def test_module_plan_parent_and_child_tables():
    plan = build_module_plan(
        "Wedding_Bookings",
        _wedding_fields(),
        {"Booked_Services": _service_fields()},
        table_name_overrides={"Booked_Services": "wedding_bookings_services"},
    )
    parent = plan.parent
    assert parent.name == "wedding_bookings"
    assert parent.column_names[0] == "id" and parent.version_column == "modified_time"
    assert {"owner_id", "owner_name", "package_price", "_synced_at", "_is_deleted", "_raw_json"} <= set(parent.column_names)
    # Collision: lower field id keeps the plain name, the other gets a stable id-based suffix.
    assert parent.column("email").source == "Email" and parent.column("email_16").source == "EMAIL"
    assert plan.requires_record_fetch is True
    assert "Booked_Services" not in plan.list_fields

    (child,) = plan.children
    assert child.name == "wedding_bookings_services" and child.parent_key == "parent_id"
    assert child.column_names[:4] == ["id", "parent_id", "_row_index", "_parent_modified_time"]
    assert "parent_id_id" not in child.column_names  # Parent_Id lookup is owned by the pipeline
    assert child.column("unit_price").type == "NUMERIC"


def test_default_child_table_name_and_exclusions():
    plan = build_module_plan(
        "Wedding_Bookings",
        _wedding_fields(),
        {"Booked_Services": _service_fields()},
        exclude_fields=["Wedding_Bookings.Package_Price"],
        store_raw_json=False,
    )
    assert plan.children[0].name == "wedding_bookings_booked_services"
    assert "package_price" not in plan.parent.column_names and "_raw_json" not in plan.parent.column_names
    assert ("Package_Price", "excluded by ZOHO_EXCLUDE_FIELDS") in plan.skipped_fields


def test_module_without_detail_only_fields_uses_list_api():
    plan = build_module_plan("Leads", [zf("Last_Name", "text", "1"), zf("Modified_Time", "datetime", "2")], {})
    assert plan.requires_record_fetch is False and plan.list_fields == ("Last_Name", "Modified_Time")
