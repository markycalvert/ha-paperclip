"""Tests for the standalone Paperclip API client."""

from __future__ import annotations

from typing import Any

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


async def test_non_json_and_no_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without a key no Authorization header is sent; odd bodies are tolerated."""
    aioclient_mock.get(f"{BASE_URL}/api/health", text="not json")
    aioclient_mock.get(f"{BASE_URL}/api/companies", json={"unexpected": True})
    client = PaperclipClient(async_get_clientsession(hass), BASE_URL + "/", "")
    health = await client.get_health()
    assert health.status == "unknown"
    assert await client.list_companies() == []
    headers = aioclient_mock.mock_calls[0][3]
    assert "Authorization" not in headers
