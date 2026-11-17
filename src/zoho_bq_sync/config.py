"""Configuration from environment variables (12-factor; identical locally and on Cloud Run)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping

from .errors import ConfigError
from .secrets import resolve_json_secret, resolve_secret

# (accounts server, API domain) per Zoho data center.
# https://www.zoho.com/crm/developer/docs/api/v8/multi-dc.html
DATA_CENTERS: dict[str, tuple[str, str]] = {
    "us": ("https://accounts.zoho.com", "https://www.zohoapis.com"),
    "eu": ("https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    "in": ("https://accounts.zoho.in", "https://www.zohoapis.in"),
    "au": ("https://accounts.zoho.com.au", "https://www.zohoapis.com.au"),
    "jp": ("https://accounts.zoho.jp", "https://www.zohoapis.jp"),
    "cn": ("https://accounts.zoho.com.cn", "https://www.zohoapis.com.cn"),
    "ca": ("https://accounts.zohocloud.ca", "https://www.zohoapis.ca"),
    "sa": ("https://accounts.zoho.sa", "https://www.zohoapis.sa"),
    # Developer-edition and sandbox orgs authenticate on the US accounts server but are
    # served from dedicated API hosts.
    "us-developer": ("https://accounts.zoho.com", "https://developer.zohoapis.com"),
    "us-sandbox": ("https://accounts.zoho.com", "https://sandbox.zohoapis.com"),
}

_DATASET_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be a boolean, got {raw!r}")


def _int(env: Mapping[str, str], key: str, default: int, minimum: int = 0) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    return value


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from None


def _csv(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in env.get(key, "").split(",") if part.strip())


def _mapping(env: Mapping[str, str], key: str) -> dict[str, str]:
    """Parse "A=b,C=d" into {"A": "b", "C": "d"}."""
    result: dict[str, str] = {}
    for item in _csv(env, key):
        if "=" not in item:
            raise ConfigError(f"{key}: expected KEY=VALUE pairs, got {item!r}")
        k, v = item.split("=", 1)
        result[k.strip()] = v.strip()
    return result


@dataclass(frozen=True)
class ZohoSettings:
    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    accounts_url: str
    api_domain: str
    api_version: str = "v8"
    timeout_seconds: float = 60.0
    max_retries: int = 5
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    max_api_calls_per_run: int = 2000
    page_size: int = 200


@dataclass(frozen=True)
class BigQuerySettings:
    project: str
    dataset: str
    staging_dataset: str
    location: str
    staging_expiration_hours: int = 24


@dataclass(frozen=True)
class SyncSettings:
    modules: tuple[str, ...] = ()
    exclude_modules: tuple[str, ...] = ()
    overlap_seconds: int = 300
    table_name_overrides: dict[str, str] = field(default_factory=dict)
    exclude_fields: frozenset[str] = frozenset()
    backfill_on_new_fields: bool = False
    max_delete_fraction: float = 0.5
    store_raw_json: bool = True
    fail_on_reconciliation_mismatch: bool = False


@dataclass(frozen=True)
class Settings:
    zoho: ZohoSettings | None
    bigquery: BigQuerySettings | None
    sync: SyncSettings
    log_level: str = "INFO"
    log_format: str = "json"


def load_zoho_settings(env: Mapping[str, str]) -> ZohoSettings:
    dc = env.get("ZOHO_DC", "us").lower()
    if dc not in DATA_CENTERS:
        raise ConfigError(f"ZOHO_DC must be one of {sorted(DATA_CENTERS)}, got {dc!r}")
    default_accounts, default_api = DATA_CENTERS[dc]

    # One JSON secret {"client_id", "client_secret", "refresh_token"} keeps Cloud Run to a
    # single secret mount; individual variables are supported for local development.
    oauth = resolve_json_secret(env, "ZOHO_OAUTH_JSON")
    client_id = env.get("ZOHO_CLIENT_ID") or oauth.get("client_id")
    client_secret = resolve_secret(env, "ZOHO_CLIENT_SECRET", required=False) or oauth.get("client_secret")
    refresh_token = resolve_secret(env, "ZOHO_REFRESH_TOKEN", required=False) or oauth.get("refresh_token")
    missing = [n for n, v in (("client_id", client_id), ("client_secret", client_secret), ("refresh_token", refresh_token)) if not v]
    if missing:
        raise ConfigError(
            f"Zoho OAuth credentials missing: {', '.join(missing)} "
            "(set ZOHO_OAUTH_JSON_FILE / ZOHO_OAUTH_JSON_SECRET or ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN)"
        )

    accounts_url = (env.get("ZOHO_ACCOUNTS_URL") or oauth.get("accounts_url") or default_accounts).rstrip("/")
    api_domain = (env.get("ZOHO_API_DOMAIN") or oauth.get("api_domain") or default_api).rstrip("/")
    for name, url in (("ZOHO_ACCOUNTS_URL", accounts_url), ("ZOHO_API_DOMAIN", api_domain)):
        if not url.startswith("https://"):
            raise ConfigError(f"{name} must be an https:// URL")

    page_size = _int(env, "ZOHO_PAGE_SIZE", 200, minimum=1)
    if page_size > 200:
        raise ConfigError("ZOHO_PAGE_SIZE cannot exceed 200 (Zoho per_page maximum)")

    return ZohoSettings(
        client_id=str(client_id),
        client_secret=str(client_secret),
        refresh_token=str(refresh_token),
        accounts_url=accounts_url,
        api_domain=api_domain,
        api_version=env.get("ZOHO_API_VERSION", "v8"),
        timeout_seconds=_float(env, "ZOHO_TIMEOUT_SECONDS", 60.0),
        max_retries=_int(env, "ZOHO_MAX_RETRIES", 5),
        backoff_base_seconds=_float(env, "ZOHO_BACKOFF_BASE_SECONDS", 1.0),
        backoff_max_seconds=_float(env, "ZOHO_BACKOFF_MAX_SECONDS", 60.0),
        max_api_calls_per_run=_int(env, "ZOHO_MAX_API_CALLS_PER_RUN", 2000, minimum=1),
        page_size=page_size,
    )


def load_bigquery_settings(env: Mapping[str, str]) -> BigQuerySettings:
    # Only the explicit variable counts: GOOGLE_CLOUD_PROJECT is often left in the shell by other
    # work, and falling back to it would silently write to someone else's project.
    project = env.get("GCP_PROJECT")
    if not project:
        raise ConfigError("GCP_PROJECT is not set (GOOGLE_CLOUD_PROJECT is deliberately ignored)")
    dataset = env.get("BQ_DATASET", "zoho_crm")
    staging = env.get("BQ_STAGING_DATASET", f"{dataset}_staging")
    for key, value in (("BQ_DATASET", dataset), ("BQ_STAGING_DATASET", staging)):
        if not _DATASET_RE.match(value):
            raise ConfigError(f"{key} may contain only letters, digits and underscores")
    return BigQuerySettings(
        project=project,
        dataset=dataset,
        staging_dataset=staging,
        location=env.get("BQ_LOCATION", "EU"),
        staging_expiration_hours=_int(env, "BQ_STAGING_EXPIRATION_HOURS", 24, minimum=1),
    )


def load_sync_settings(env: Mapping[str, str]) -> SyncSettings:
    fraction = _float(env, "MAX_DELETE_FRACTION", 0.5)
    if not 0 <= fraction <= 1:
        raise ConfigError("MAX_DELETE_FRACTION must be between 0 and 1")
    return SyncSettings(
        modules=_csv(env, "ZOHO_MODULES"),
        exclude_modules=_csv(env, "ZOHO_EXCLUDE_MODULES"),
        overlap_seconds=_int(env, "SYNC_OVERLAP_SECONDS", 300),
        table_name_overrides=_mapping(env, "TABLE_NAME_OVERRIDES"),
        exclude_fields=frozenset(_csv(env, "ZOHO_EXCLUDE_FIELDS")),
        backfill_on_new_fields=_bool(env, "BACKFILL_ON_NEW_FIELDS", False),
        max_delete_fraction=fraction,
        store_raw_json=_bool(env, "STORE_RAW_JSON", True),
        fail_on_reconciliation_mismatch=_bool(env, "FAIL_ON_RECONCILIATION_MISMATCH", False),
    )


def load_settings(
    env: Mapping[str, str] | None = None,
    *,
    require_zoho: bool = True,
    require_bigquery: bool = True,
) -> Settings:
    env = os.environ if env is None else env
    log_format = env.get("LOG_FORMAT", "json").lower()
    if log_format not in ("json", "text"):
        raise ConfigError("LOG_FORMAT must be json or text")
    return Settings(
        zoho=load_zoho_settings(env) if require_zoho else None,
        bigquery=load_bigquery_settings(env) if require_bigquery else None,
        sync=load_sync_settings(env),
        log_level=env.get("LOG_LEVEL", "INFO"),
        log_format=log_format,
    )
