import json

import pytest

from zoho_bq_sync.config import load_settings
from zoho_bq_sync.demo.synthetic import NAME_PREFIX, PARENT_FIELDS, SUBFORM, SUBFORM_FIELDS, generate_bookings
from zoho_bq_sync.errors import ConfigError

BASE = {"GCP_PROJECT": "demo-proj", "ZOHO_CLIENT_ID": "id", "ZOHO_CLIENT_SECRET": "secret", "ZOHO_REFRESH_TOKEN": "refresh"}


def test_defaults_and_data_center_mapping():
    s = load_settings({**BASE, "ZOHO_DC": "eu"})
    assert s.zoho.accounts_url == "https://accounts.zoho.eu" and s.zoho.api_domain == "https://www.zohoapis.eu"
    assert s.bigquery.dataset == "zoho_crm" and s.bigquery.staging_dataset == "zoho_crm_staging"
    assert s.sync.overlap_seconds == 300 and s.sync.store_raw_json is True
    assert "secret" not in repr(s.zoho) and "refresh" not in repr(s.zoho)


def test_oauth_json_secret_file(tmp_path):
    secret = tmp_path / "oauth.json"
    secret.write_text(json.dumps({"client_id": "cid", "client_secret": "cs", "refresh_token": "rt", "api_domain": "https://developer.zohoapis.com"}))
    s = load_settings({"GCP_PROJECT": "p", "ZOHO_OAUTH_JSON_FILE": str(secret)})
    assert s.zoho.client_id == "cid" and s.zoho.refresh_token == "rt" and s.zoho.api_domain == "https://developer.zohoapis.com"


def test_overrides_and_validation():
    s = load_settings({**BASE, "TABLE_NAME_OVERRIDES": "Booked_Services=wedding_bookings_services", "ZOHO_MODULES": "Wedding_Bookings, Contacts"})
    assert s.sync.table_name_overrides == {"Booked_Services": "wedding_bookings_services"}
    assert s.sync.modules == ("Wedding_Bookings", "Contacts")
    with pytest.raises(ConfigError):
        load_settings({**BASE, "ZOHO_DC": "mars"})
    with pytest.raises(ConfigError):
        load_settings({**BASE, "ZOHO_PAGE_SIZE": "500"})
    with pytest.raises(ConfigError, match="refresh_token"):
        load_settings({"GCP_PROJECT": "p", "ZOHO_CLIENT_ID": "id", "ZOHO_CLIENT_SECRET": "s"})


def test_project_must_be_explicit():
    no_project = {k: v for k, v in BASE.items() if k != "GCP_PROJECT"}
    with pytest.raises(ConfigError, match="GCP_PROJECT"):
        load_settings({**no_project, "GOOGLE_CLOUD_PROJECT": "leftover-project"})


def test_synthetic_generator_is_deterministic_and_marked():
    first, second = generate_bookings(5, seed=42), generate_bookings(5, seed=42)
    assert first == second
    for booking in first:
        assert booking["Name"].startswith(NAME_PREFIX)
        assert booking["Contact_Email"].endswith("@example.com")
        assert set(PARENT_FIELDS) <= set(booking)
        assert booking[SUBFORM] and all(set(SUBFORM_FIELDS) == set(s) for s in booking[SUBFORM])
        total = round(sum(s["Unit_Price"] * s["Quantity"] for s in booking[SUBFORM]), 2)
        assert booking["Package_Price"] == pytest.approx(total)
