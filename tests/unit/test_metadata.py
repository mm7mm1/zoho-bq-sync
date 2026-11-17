import pytest

from tests.fakes import FakeResponse, FakeSession, StaticAuth
from zoho_bq_sync.errors import ConfigError
from zoho_bq_sync.zoho.client import ZohoClient
from zoho_bq_sync.zoho.metadata import MetadataClient, ZohoField, ZohoModule, select_modules

# Shapes follow GET /crm/v8/settings/modules and /settings/fields (trimmed MOCK payloads).
MODULES = {
    "modules": [
        {"api_name": "Leads", "generated_type": "default", "api_supported": True, "plural_label": "Leads", "id": "1"},
        {"api_name": "Wedding_Bookings", "generated_type": "custom", "api_supported": True, "plural_label": "Wedding Bookings", "id": "2"},
        {"api_name": "Booked_Services", "generated_type": "subform", "api_supported": True, "id": "3"},
        {"api_name": "Contacts_X_Deals", "generated_type": "linking", "api_supported": True, "id": "4"},
        {"api_name": "Visits", "generated_type": "default", "api_supported": False, "id": "5"},
    ]
}
FIELDS = {
    "fields": [
        {"api_name": "Name", "data_type": "text", "json_type": "string", "id": "100", "field_label": "Booking Name"},
        {"api_name": "Booked_Services", "data_type": "subform", "json_type": "jsonarray", "id": "101", "associated_module": {"module": "Booked_Services", "id": "3"}},
        {"api_name": "Margin", "data_type": "formula", "json_type": "double", "id": "102", "formula": {"return_type": "currency"}},
        {"api_name": "Apostille_Deadline", "data_type": "date", "json_type": "string", "id": "103", "custom_field": True},
    ]
}


def metadata_client(responses):
    session = FakeSession(responses)
    return MetadataClient(ZohoClient(StaticAuth(), "https://www.zohoapis.com", session=session, sleep=lambda s: None)), session


def test_list_modules_and_default_selection_excludes_non_record_modules():
    client, session = metadata_client([FakeResponse(200, MODULES)])
    modules = client.list_modules()
    assert session.calls[0]["url"].endswith("/crm/v8/settings/modules")
    assert [m.api_name for m in select_modules(modules)] == ["Leads", "Wedding_Bookings"]


def test_include_and_exclude_lists():
    modules = [ZohoModule.from_api(m) for m in MODULES["modules"]]
    assert [m.api_name for m in select_modules(modules, include=["Wedding_Bookings"])] == ["Wedding_Bookings"]
    assert [m.api_name for m in select_modules(modules, exclude=["Leads"])] == ["Wedding_Bookings"]
    assert [m.api_name for m in select_modules(modules, include=["*"])] == ["Leads", "Wedding_Bookings"]


def test_unknown_or_unsyncable_include_fails():
    modules = [ZohoModule.from_api(m) for m in MODULES["modules"]]
    with pytest.raises(ConfigError, match="Unknown"):
        select_modules(modules, include=["Nope"])
    with pytest.raises(ConfigError, match="not syncable"):
        select_modules(modules, include=["Booked_Services"])


def test_field_discovery_parses_subform_formula_and_custom_flags():
    client, session = metadata_client([FakeResponse(200, FIELDS)])
    fields = {f.api_name: f for f in client.get_fields("Wedding_Bookings")}
    assert session.calls[0]["params"] == {"module": "Wedding_Bookings"}
    assert fields["Booked_Services"].is_subform and fields["Booked_Services"].associated_module == "Booked_Services"
    assert fields["Margin"].return_type == "currency"
    assert fields["Apostille_Deadline"].custom_field is True
    assert fields["Name"].label == "Booking Name"


def test_legacy_subform_shape_is_supported():
    f = ZohoField.from_api({"api_name": "Items", "data_type": "subform", "subform": {"module": "Items_Sub"}})
    assert f.associated_module == "Items_Sub"
