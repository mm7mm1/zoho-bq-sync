"""Secret resolution.

Order of precedence for a secret NAME:
  1. NAME            - plain env var (local development only)
  2. NAME_FILE       - path to a file; on Cloud Run this is a Secret Manager secret
                       mounted with --set-secrets=/path=secret:version
  3. NAME_SECRET     - Secret Manager resource name read via the API (local runs that
                       should not keep secrets in .env)

Secrets are read when settings are loaded inside the command handler, never at import
time, so tests and `--help` never touch Secret Manager.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError


def _read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"Cannot read secret file {path}: {exc.strerror}") from None


def _access_secret_manager(resource: str) -> str:
    # Imported lazily: the dependency is only needed when a secret is read via the API.
    from google.cloud import secretmanager

    if "/versions/" not in resource:
        resource = f"{resource}/versions/latest"
    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(name=resource)
    return response.payload.data.decode("utf-8").strip()


def resolve_secret(env: Mapping[str, str], name: str, *, required: bool = True) -> str | None:
    if env.get(name):
        return env[name].strip()
    if env.get(f"{name}_FILE"):
        return _read_file(env[f"{name}_FILE"])
    if env.get(f"{name}_SECRET"):
        return _access_secret_manager(env[f"{name}_SECRET"])
    if required:
        raise ConfigError(f"{name} is not configured: set {name}, {name}_FILE or {name}_SECRET")
    return None


def resolve_json_secret(env: Mapping[str, str], name: str) -> dict[str, Any]:
    """Resolve a secret that holds a JSON object (e.g. the whole Zoho OAuth client)."""
    raw = resolve_secret(env, name, required=False)
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise ConfigError(f"{name} does not contain valid JSON") from None
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must contain a JSON object")
    return value
