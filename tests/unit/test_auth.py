import pytest
import requests

from tests.fakes import FakeResponse, FakeSession
from zoho_bq_sync.errors import ZohoAuthError
from zoho_bq_sync.zoho.auth import ZohoOAuth

TOKEN_OK = {"access_token": "1000.aaa", "api_domain": "https://www.zohoapis.eu", "token_type": "Bearer", "expires_in": 3600}


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def make(session, clock=None, **kw):
    return ZohoOAuth(
        "https://accounts.zoho.eu",
        "client-id",
        "client-secret-value",
        "refresh-token-value",
        session=session,
        clock=clock or Clock(),
        sleep=lambda s: None,
        **kw,
    )


def test_refresh_posts_refresh_grant_to_accounts_server():
    session = FakeSession([FakeResponse(200, TOKEN_OK)])
    auth = make(session)

    assert auth.get_access_token() == "1000.aaa"
    call = session.calls[0]
    assert call["url"] == "https://accounts.zoho.eu/oauth/v2/token"
    assert call["data"] == {
        "refresh_token": "refresh-token-value",
        "client_id": "client-id",
        "client_secret": "client-secret-value",
        "grant_type": "refresh_token",
    }
    assert auth.api_domain == "https://www.zohoapis.eu"


def test_token_is_cached_until_refresh_margin_then_refreshed():
    clock = Clock()
    session = FakeSession([FakeResponse(200, TOKEN_OK), FakeResponse(200, {**TOKEN_OK, "access_token": "1000.bbb"})])
    auth = make(session, clock, refresh_margin_seconds=300)

    assert auth.get_access_token() == "1000.aaa"
    clock.t += 3600 - 301  # still outside the margin
    assert auth.get_access_token() == "1000.aaa"
    assert len(session.calls) == 1
    clock.t += 2  # inside the 5-minute margin -> proactive refresh
    assert auth.get_access_token() == "1000.bbb"
    assert len(session.calls) == 2


def test_invalidate_forces_refresh():
    session = FakeSession([FakeResponse(200, TOKEN_OK), FakeResponse(200, {**TOKEN_OK, "access_token": "1000.ccc"})])
    auth = make(session)
    auth.get_access_token()
    auth.invalidate()
    assert auth.get_access_token() == "1000.ccc"


def test_error_body_with_http_200_raises_without_leaking_secrets():
    session = FakeSession([FakeResponse(200, {"error": "invalid_client"})])
    auth = make(session)
    with pytest.raises(ZohoAuthError) as exc:
        auth.get_access_token()
    assert "invalid_client" in str(exc.value)
    assert "client-secret-value" not in str(exc.value)
    assert "refresh-token-value" not in str(exc.value)


def test_server_error_is_retried_then_succeeds():
    session = FakeSession([FakeResponse(503, {}), requests.ConnectionError("boom"), FakeResponse(200, TOKEN_OK)])
    auth = make(session, max_retries=3)
    assert auth.get_access_token() == "1000.aaa"
    assert len(session.calls) == 3


def test_refresh_guard_prevents_token_storm():
    session = FakeSession(lambda *a, **k: FakeResponse(200, TOKEN_OK))
    auth = make(session, max_refreshes=2)
    auth.get_access_token()
    auth.invalidate()
    auth.get_access_token()
    auth.invalidate()
    with pytest.raises(ZohoAuthError, match="more than 2 times"):
        auth.get_access_token()
