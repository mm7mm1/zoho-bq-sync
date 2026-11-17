from datetime import datetime, timezone

import pytest
import requests

from tests.fakes import FakeResponse, FakeSession, StaticAuth
from zoho_bq_sync.errors import ApiBudgetExceeded, ZohoApiError, ZohoRateLimitError
from zoho_bq_sync.zoho.client import ZohoClient

OK = FakeResponse(200, {"data": [{"id": "1"}]}, {"Date": "Tue, 17 Nov 2026 10:00:00 GMT"})


def make(responses, **kw):
    sleeps: list[float] = []
    auth = StaticAuth()
    session = FakeSession(responses)
    client = ZohoClient(
        auth,
        "https://www.zohoapis.com",
        session=session,
        sleep=sleeps.append,
        rng=lambda: 1.0,
        backoff_base_seconds=1.0,
        backoff_max_seconds=8.0,
        **kw,
    )
    return client, session, sleeps, auth


def test_auth_header_url_and_server_time():
    client, session, _, _ = make([OK])
    response = client.get("Leads", params={"fields": "Last_Name"})
    call = session.calls[0]
    assert call["url"] == "https://www.zohoapis.com/crm/v8/Leads"
    assert call["headers"]["Authorization"] == "Zoho-oauthtoken tok-1"
    assert response.server_time == datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)


def test_429_honours_retry_after():
    client, _, sleeps, _ = make([FakeResponse(429, {"code": "TOO_MANY_REQUESTS"}, {"Retry-After": "3"}), OK])
    assert client.get("Leads").body == {"data": [{"id": "1"}]}
    assert sleeps == [3.0]


def test_5xx_uses_exponential_backoff_capped():
    responses = [FakeResponse(503, {}) for _ in range(5)] + [OK]
    client, _, sleeps, _ = make(responses, max_retries=5)
    client.get("Leads")
    # equal jitter with rng=1.0 -> full ceiling: 1, 2, 4, 8, 8 (capped)
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_retries_exhausted_raises_rate_limit_error():
    client, _, sleeps, _ = make([FakeResponse(429, {"code": "TOO_MANY_REQUESTS", "message": "limit"})] * 3, max_retries=2)
    with pytest.raises(ZohoRateLimitError) as exc:
        client.get("Leads")
    assert exc.value.status == 429 and len(sleeps) == 2


def test_invalid_token_triggers_one_reauth_and_retry():
    client, session, _, auth = make([FakeResponse(401, {"code": "INVALID_TOKEN"}), OK])
    client.get("Leads")
    assert auth.invalidations == 1
    assert session.calls[1]["headers"]["Authorization"] == "Zoho-oauthtoken tok-2"


def test_second_invalid_token_is_not_retried_forever():
    client, _, _, auth = make([FakeResponse(401, {"code": "INVALID_TOKEN"})] * 2)
    with pytest.raises(ZohoApiError) as exc:
        client.get("Leads")
    assert exc.value.code == "INVALID_TOKEN" and auth.invalidations == 1


def test_client_errors_are_not_retried():
    client, session, sleeps, _ = make([FakeResponse(400, {"code": "INVALID_DATA", "message": "bad field", "details": {"api_name": "X"}})])
    with pytest.raises(ZohoApiError) as exc:
        client.get("Leads")
    assert exc.value.code == "INVALID_DATA" and exc.value.details == {"api_name": "X"}
    assert len(session.calls) == 1 and sleeps == []


def test_204_and_304_return_empty_body():
    client, _, _, _ = make([FakeResponse(204), FakeResponse(304)])
    assert client.get("Leads").body is None
    assert client.get("Leads").body is None


def test_network_errors_retried_for_get_only():
    client, _, sleeps, _ = make([requests.ConnectionError("reset"), OK])
    client.get("Leads")
    assert len(sleeps) == 1

    client, _, _, _ = make([requests.Timeout("slow")])
    with pytest.raises(ZohoApiError, match="Network error"):
        client.post("Leads", json={"data": []})


def test_writes_are_not_retried_on_5xx():
    client, session, _, _ = make([FakeResponse(500, {"code": "INTERNAL_ERROR"})])
    with pytest.raises(ZohoApiError):
        client.post("Leads", json={"data": []})
    assert len(session.calls) == 1


def test_api_budget_stops_before_exceeding():
    client, session, _, _ = make([OK, OK, OK], max_calls=2)
    client.get("Leads")
    client.get("Leads")
    with pytest.raises(ApiBudgetExceeded):
        client.get("Leads")
    assert len(session.calls) == 2


def test_credit_header_is_tracked():
    client, _, _, _ = make([FakeResponse(200, {}, {"X-API-CREDITS-REMAINING": "4200"})])
    client.get("Leads")
    assert client.credits_remaining == 4200
