"""Zoho OAuth 2.0 refresh-token flow.

POST {accounts_url}/oauth/v2/token?grant_type=refresh_token returns a 1-hour access
token. Zoho allows only 10 access tokens per refresh token in 10 minutes, so the token
is cached and refreshed proactively before expiry instead of on every request.
Refresh tokens do not rotate and do not expire until revoked, so nothing needs to be
written back to Secret Manager.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable

import requests

from ..errors import ZohoAuthError
from ..logging_setup import log_event
from .backoff import backoff_delay

logger = logging.getLogger(__name__)


class ZohoOAuth:
    TOKEN_PATH = "/oauth/v2/token"

    def __init__(
        self,
        accounts_url: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        session: Any = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        refresh_margin_seconds: float = 300.0,
        timeout_seconds: float = 30.0,
        max_refreshes: int = 5,
        max_retries: int = 3,
    ) -> None:
        self._url = accounts_url.rstrip("/") + self.TOKEN_PATH
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._session = session or requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._margin = refresh_margin_seconds
        self._timeout = timeout_seconds
        self._max_refreshes = max_refreshes
        self._max_retries = max_retries
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self.api_domain: str | None = None
        self.refresh_count = 0

    def get_access_token(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._expires_at - self._margin:
                self._refresh()
            assert self._token is not None
            return self._token

    def invalidate(self) -> None:
        """Drop the cached token after Zoho rejected it (revoked or expired early)."""
        with self._lock:
            self._token = None

    def _refresh(self) -> None:
        if self.refresh_count >= self._max_refreshes:
            # A loop of refreshes would burn Zoho's 10-tokens-per-10-minutes allowance and
            # lock out every other integration using this refresh token.
            raise ZohoAuthError(
                f"Refusing to refresh the access token more than {self._max_refreshes} times in one run"
            )
        data = {
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
        }
        response = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.post(self._url, data=data, timeout=self._timeout)
            except requests.RequestException as exc:
                if attempt >= self._max_retries:
                    raise ZohoAuthError(f"Zoho accounts server unreachable: {type(exc).__name__}") from None
                self._sleep(backoff_delay(attempt, 1.0, 30.0, random.random))
                continue
            if response.status_code >= 500 and attempt < self._max_retries:
                self._sleep(backoff_delay(attempt, 1.0, 30.0, random.random))
                continue
            break

        assert response is not None
        try:
            body = response.json()
        except ValueError:
            body = {}
        # Zoho reports invalid_client / invalid_code with HTTP 200 and an "error" key.
        if response.status_code != 200 or not isinstance(body, dict) or "error" in body or "access_token" not in body:
            error = body.get("error", "unknown") if isinstance(body, dict) else "unknown"
            raise ZohoAuthError(f"Zoho token refresh failed: HTTP {response.status_code}, error={error}")

        self._token = str(body["access_token"])
        self._expires_at = self._clock() + float(body.get("expires_in", 3600))
        self.api_domain = body.get("api_domain")
        self.refresh_count += 1
        log_event(
            logger,
            "oauth_token_refreshed",
            expires_in=body.get("expires_in", 3600),
            api_domain=self.api_domain,
            refresh_count=self.refresh_count,
        )
