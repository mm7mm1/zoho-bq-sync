"""HTTP client for Zoho CRM REST API v8.

Responsibilities: auth header, retries with backoff, re-auth on INVALID_TOKEN, Zoho
error parsing, API-credit awareness and a hard per-run call budget.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol

import requests

from ..errors import ApiBudgetExceeded, ZohoApiError, ZohoRateLimitError
from ..logging_setup import log_event
from .backoff import backoff_delay, parse_http_date, parse_retry_after

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Codes for which a fresh access token can fix the request.
REAUTH_CODES = frozenset({"INVALID_TOKEN", "AUTHENTICATION_FAILURE"})
CREDITS_HEADER = "X-API-CREDITS-REMAINING"


class TokenProvider(Protocol):
    def get_access_token(self) -> str: ...

    def invalidate(self) -> None: ...


@dataclass(frozen=True)
class ZohoResponse:
    status: int
    body: dict[str, Any] | None
    headers: Mapping[str, str]
    server_time: datetime | None  # parsed from the Date header; used for drift-safe checkpoints


class ZohoClient:
    def __init__(
        self,
        auth: TokenProvider,
        api_domain: str,
        *,
        api_version: str = "v8",
        session: Any = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 5,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        max_calls: int = 2000,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._auth = auth
        self.base_url = f"{api_domain.rstrip('/')}/crm/{api_version}"
        self._session = session or requests.Session()
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._max_calls = max_calls
        self._sleep = sleep
        self._rng = rng
        self.calls_made = 0
        self.credits_remaining: int | None = None

    # -- public verbs -----------------------------------------------------------------
    def get(self, path: str, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None) -> ZohoResponse:
        return self.request("GET", path, params=params, headers=headers)

    def post(self, path: str, json: Any) -> ZohoResponse:
        return self.request("POST", path, json=json)

    def put(self, path: str, json: Any) -> ZohoResponse:
        return self.request("PUT", path, json=json)

    def delete(self, path: str, params: Mapping[str, Any] | None = None) -> ZohoResponse:
        return self.request("DELETE", path, params=params)

    # -- core -------------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> ZohoResponse:
        url = f"{self.base_url}/{path.lstrip('/')}"
        idempotent = method.upper() == "GET"
        attempt = 0
        reauthenticated = False

        while True:
            if self.calls_made >= self._max_calls:
                raise ApiBudgetExceeded(
                    f"Per-run Zoho API budget of {self._max_calls} calls reached; "
                    "raise ZOHO_MAX_API_CALLS_PER_RUN only after checking the org's daily credits"
                )
            request_headers = {"Authorization": f"Zoho-oauthtoken {self._auth.get_access_token()}"}
            if headers:
                request_headers.update(headers)

            started = time.monotonic()
            try:
                self.calls_made += 1
                response = self._session.request(
                    method, url, params=params, json=json, headers=request_headers, timeout=self._timeout
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                # Retrying a write after a timeout could duplicate it, so only GETs retry.
                if not idempotent or attempt >= self._max_retries:
                    raise ZohoApiError(f"Network error calling Zoho: {type(exc).__name__}", path=path) from None
                self._backoff(attempt, path, reason=type(exc).__name__)
                attempt += 1
                continue

            self._track_credits(response.headers)
            log_event(
                logger,
                "zoho_request",
                logging.DEBUG,
                method=method,
                path=path,
                status=response.status_code,
                attempt=attempt,
                duration_ms=round((time.monotonic() - started) * 1000),
            )

            status = response.status_code
            if status in (204, 304):
                # 204: no records match; 304: nothing modified since If-Modified-Since.
                return ZohoResponse(status, None, response.headers, parse_http_date(response.headers.get("Date")))
            if 200 <= status < 300:
                try:
                    body = response.json()
                except ValueError:
                    raise ZohoApiError("Zoho returned a non-JSON success response", status=status, path=path) from None
                return ZohoResponse(status, body, response.headers, parse_http_date(response.headers.get("Date")))

            body = _safe_json(response)
            code = body.get("code") if isinstance(body, dict) else None
            message = body.get("message") if isinstance(body, dict) else None

            if status == 401 and code in REAUTH_CODES and not reauthenticated:
                log_event(logger, "zoho_reauthenticating", logging.WARNING, path=path, code=code)
                self._auth.invalidate()
                reauthenticated = True
                continue

            # 429 means the request was rejected before processing, so even writes are safe to retry.
            retryable = status == 429 or (idempotent and status in RETRYABLE_STATUS)
            if retryable and attempt < self._max_retries:
                delay = parse_retry_after(response.headers.get("Retry-After"))
                self._backoff(attempt, path, reason=f"HTTP {status} {code or ''}".strip(), delay=delay)
                attempt += 1
                continue

            error_cls = ZohoRateLimitError if status == 429 else ZohoApiError
            raise error_cls(
                message or "Zoho API request failed",
                status=status,
                code=code,
                details=body.get("details") if isinstance(body, dict) else None,
                path=path,
            )

    def _backoff(self, attempt: int, path: str, *, reason: str, delay: float | None = None) -> None:
        wait = delay if delay is not None else backoff_delay(attempt, self._backoff_base, self._backoff_max, self._rng)
        wait = min(wait, self._backoff_max)
        log_event(logger, "zoho_retry", logging.WARNING, path=path, attempt=attempt + 1, wait_seconds=round(wait, 2), reason=reason)
        self._sleep(wait)

    def _track_credits(self, headers: Mapping[str, str]) -> None:
        # Zoho only sends this header once 50% of the rolling 24h credit allowance is used.
        raw = headers.get(CREDITS_HEADER) if headers else None
        if raw is None:
            return
        try:
            remaining = int(raw)
        except ValueError:
            return
        if self.credits_remaining is None or remaining < self.credits_remaining:
            if self.credits_remaining is None:
                log_event(logger, "zoho_credits_low", logging.WARNING, credits_remaining=remaining)
            self.credits_remaining = remaining


def _safe_json(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
