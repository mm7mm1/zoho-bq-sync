"""Exception hierarchy.

Every pipeline failure is a RuntimeError subclass so that the Cloud Run task exits
non-zero and the BigQuery rows and checkpoint stay untouched (data writes happen in one
transaction; additive DDL that ran before it may stay).
"""

from __future__ import annotations

from typing import Any


class SyncError(RuntimeError):
    """Base class for all pipeline errors."""


class ConfigError(SyncError):
    """Missing or invalid configuration."""


class ZohoAuthError(SyncError):
    """OAuth token refresh failed. Messages never include secrets."""


class ZohoApiError(SyncError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        details: Any = None,
        path: str | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.details = details
        self.path = path
        parts = [message]
        if status is not None:
            parts.append(f"status={status}")
        if code:
            parts.append(f"code={code}")
        if path:
            parts.append(f"path={path}")
        super().__init__(" ".join(parts))


class ZohoRateLimitError(ZohoApiError):
    """HTTP 429: concurrency limit or the rolling 24h API-credit allowance is exhausted."""


class ApiBudgetExceeded(SyncError):
    """The per-run API call budget was reached; protects the org's shared daily credits."""


class SchemaConflictError(SyncError):
    """A Zoho field type change cannot be applied to the existing BigQuery column safely."""


class CoercionError(SyncError):
    """A Zoho value cannot be converted to its BigQuery column type."""


class SafetyCheckError(SyncError):
    """A destructive change (mass soft-delete) exceeded the configured threshold."""


class ReconciliationError(SyncError):
    """Row counts differ and FAIL_ON_RECONCILIATION_MISMATCH is enabled."""
