"""Tests for the standalone Paperclip API client."""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.paperclip.api import (
    Approval,
    Comment,
    Health,
    PaperclipClient,
    PaperclipConnectionError,
    PaperclipInvalidResponseError,
    PaperclipNotFoundError,
    PaperclipRateLimitError,
    PaperclipResponseError,
    is_agent_key,
    is_board_key,
    parse_datetime,
    sign_webhook,
)

from .conftest import BASE_URL, PUBLIC_ID


def test_sign_webhook_known_vector() -> None:
    """The signature matches a vector computed independently (openssl)."""
    body = b'{"event":"doorbell","variables":{"room":"hall"}}'
    assert sign_webhook("test-secret", "1767603600", body) == (
        "sha256=d53ab3085de21e70927b3fdd2085671373ea3a1ea88237e061f145c9c0840280"
    )


def test_key_kinds() -> None:
    """Board and agent keys are told apart by prefix."""
    assert is_board_key("pcp_board_" + "0" * 48)
    assert not is_agent_key("pcp_board_" + "0" * 48)
    assert is_agent_key("pcp_" + "0" * 48)
    assert not is_agent_key("something-else")


def test_parsers_tolerate_odd_payloads() -> None:
    """Missing or wrongly typed fields fall back to defaults."""
    assert parse_datetime("not a date") is None
    assert parse_datetime("2026-01-05T09:00:00").tzinfo is not None
    health = Health.from_dict({"serverVersion": "0.3.1"})
    assert health.version == "0.3.1"
    assert health.status == "unknown"
    approval = Approval.from_dict({"id": "a", "type": "something_new", "payload": {}})
    assert approval.title == "something_new"
    comment = Comment.from_dict(
        {"id": "c", "authorType": "agent", "deletedAt": "2026-01-05T09:00:00.000Z"}
    )
    assert not comment.is_agent
    sentinel = Comment.from_dict(
        {
            "id": "c",
            "authorType": "user",
            "authorUserId": "local-board",
            "derivedAuthorAgentId": "agent-1",
        }
    )
    assert sentinel.is_agent
    assert sentinel.author_agent_id == "agent-1"
    human = Comment.from_dict(
        {"id": "c", "authorType": "user", "derivedAuthorAgentId": ""}
    )
    assert not human.is_agent
    assert human.author_agent_id is None


async def test_webhook_with_fixed_timestamp(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A given timestamp is used verbatim; an unsigned call sends no signature."""
    url = f"{BASE_URL}/api/routine-triggers/public/{PUBLIC_ID}/fire"
    aioclient_mock.post(url, status=202, json={"status": "skipped", "id": "r"})
    client = PaperclipClient(async_get_clientsession(hass), BASE_URL, "pcp_board_x")
    result = await client.fire_routine_webhook(
        PUBLIC_ID,
        {"event": "doorbell", "variables": {"room": "hall"}},
        secret="test-secret",
        timestamp=1767603600,
    )
    assert result.status == "skipped"
    headers = aioclient_mock.mock_calls[-1][3]
    assert headers["X-Paperclip-Signature"] == (
        "sha256=d53ab3085de21e70927b3fdd2085671373ea3a1ea88237e061f145c9c0840280"
    )
    await client.fire_routine_webhook(PUBLIC_ID, {}, secret=None)
    headers = aioclient_mock.mock_calls[-1][3]
    assert "X-Paperclip-Signature" not in headers
    assert "Authorization" not in headers


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"exc": aiohttp.ClientError("refused")}, PaperclipConnectionError),
        ({"exc": TimeoutError()}, PaperclipConnectionError),
        ({"status": 500, "text": "oops"}, PaperclipResponseError),
        (
            {"status": 429, "json": {"error": "Too many", "retryAfterSeconds": 7}},
            PaperclipRateLimitError,
        ),
    ],
)
async def test_request_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    kwargs: dict[str, Any],
    error: type[Exception],
) -> None:
    """Transport and HTTP failures map to client exceptions."""
    aioclient_mock.get(f"{BASE_URL}/api/health", **kwargs)
    client = PaperclipClient(async_get_clientsession(hass), BASE_URL)
    with pytest.raises(error) as err:
        await client.get_health()
    if isinstance(err.value, PaperclipRateLimitError):
        assert err.value.retry_after == 7


async def test_webhook_transport_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A webhook that cannot be delivered raises a connection error."""
    aioclient_mock.post(
        f"{BASE_URL}/api/routine-triggers/public/{PUBLIC_ID}/fire",
        exc=aiohttp.ClientError("refused"),
    )
    client = PaperclipClient(async_get_clientsession(hass), BASE_URL)
    with pytest.raises(PaperclipConnectionError):
        await client.fire_routine_webhook(PUBLIC_ID, {}, secret="s")


@pytest.mark.parametrize(
    ("kwargs", "from_paperclip"),
    [
        ({"json": {"error": "API route not found"}}, True),
        ({"text": "404 page not found\n"}, False),
        ({"text": "<html><body>Not Found</body></html>"}, False),
        ({"json": {"message": "Not Found"}}, False),
    ],
)
async def test_not_found_tells_paperclip_from_a_proxy(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    kwargs: dict[str, Any],
    from_paperclip: bool,
) -> None:
    """Only a 404 with Paperclip's JSON error body says the route is missing."""
    aioclient_mock.get(f"{BASE_URL}/api/health", status=404, **kwargs)
    client = PaperclipClient(async_get_clientsession(hass), BASE_URL)
    with pytest.raises(PaperclipNotFoundError) as err:
        await client.get_health()
    assert err.value.from_paperclip is from_paperclip


async def test_non_json_and_no_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without a key no Authorization header is sent; odd fields are tolerated."""
    aioclient_mock.get(f"{BASE_URL}/api/health", json={"unexpected": True})
    client = PaperclipClient(async_get_clientsession(hass), BASE_URL + "/", "")
    health = await client.get_health()
    assert health.status == "unknown"
    headers = aioclient_mock.mock_calls[0][3]
    assert "Authorization" not in headers


@pytest.mark.parametrize(
    ("path", "kwargs"),
    [
        # A proxy or captive portal answering 200 with its own page.
        ("/api/companies", {"text": "<html><body>Sign in</body></html>"}),
        ("/api/companies", {"json": {"unexpected": True}}),
        ("/api/companies", {"text": ""}),
        ("/api/health", {"text": "<html><body>Sign in</body></html>"}),
        ("/api/health", {"json": []}),
    ],
)
async def test_successful_status_with_wrong_body_is_an_error(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    """A 2xx body that is not the JSON Paperclip sends is never read as empty."""
    aioclient_mock.get(f"{BASE_URL}{path}", **kwargs)
    client = PaperclipClient(async_get_clientsession(hass), BASE_URL)
    call = client.list_companies if path == "/api/companies" else client.get_health
    with pytest.raises(PaperclipInvalidResponseError):
        await call()


async def test_ids_are_escaped_in_paths(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An id can never step out of its path segment into another route."""
    aioclient_mock.post(re.compile(r"^http://paperclip\.local:3100/"), json={})
    aioclient_mock.get(re.compile(r"^http://paperclip\.local:3100/"), json={})
    client = PaperclipClient(async_get_clientsession(hass), BASE_URL, "pcp_board_x")
    crafted = "../agents/a/pause?x="
    await client.approve(crafted)
    await client.reject(crafted)
    await client.get_approval(crafted)
    await client.wake_agent(crafted)
    await client.get_company(crafted)
    paths = [call[1].raw_path for call in aioclient_mock.mock_calls]
    escaped = "..%2Fagents%2Fa%2Fpause%3Fx="
    assert paths == [
        f"/api/approvals/{escaped}/approve",
        f"/api/approvals/{escaped}/reject",
        f"/api/approvals/{escaped}",
        f"/api/agents/{escaped}/wakeup",
        f"/api/companies/{escaped}",
    ]
    assert all(not call[1].query for call in aioclient_mock.mock_calls)


async def test_webhook_timestamp_is_in_milliseconds(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Two fires in the same second must not share a timestamp (a replay)."""
    url = f"{BASE_URL}/api/routine-triggers/public/{PUBLIC_ID}/fire"
    aioclient_mock.post(url, status=202, json={"status": "issue_created"})
    client = PaperclipClient(async_get_clientsession(hass), BASE_URL, None)
    with patch(
        "custom_components.paperclip.api.time.time_ns",
        return_value=1_767_603_600_123_456_789,
    ):
        await client.fire_routine_webhook(PUBLIC_ID, {}, secret="test-secret")
    headers = aioclient_mock.mock_calls[-1][3]
    assert headers["X-Paperclip-Timestamp"] == "1767603600123"
    assert headers["X-Paperclip-Signature"] == sign_webhook(
        "test-secret", "1767603600123", b"{}"
    )
