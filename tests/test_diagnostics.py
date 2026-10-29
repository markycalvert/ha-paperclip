"""Tests for Paperclip diagnostics."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.paperclip.diagnostics import async_get_config_entry_diagnostics

from .conftest import API_KEY, BASE_URL, CEO_ID


async def test_diagnostics_redacts_secrets(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The key, URL and names never appear in diagnostics."""
    await hass.services.async_call(
        "paperclip",
        "ask_agent",
        {"agent": CEO_ID, "message": "Plan the private dinner"},
        blocking=True,
    )
    result = await async_get_config_entry_diagnostics(hass, init_integration)
    dumped = json.dumps(result)
    assert API_KEY not in dumped
    assert BASE_URL not in dumped
    assert "Plan the private dinner" not in dumped
    assert '"Acme"' not in dumped
    assert result["entry"]["data"]["api_key"] == "**REDACTED**"
    assert result["entry"]["data"]["url"] == "**REDACTED**"
    assert result["health"]["version"] == "2026.1006.0-beta.0"
    assert result["agents"][0]["name"] == "**REDACTED**"
    assert result["agents"][0]["status"] == "idle"
    assert result["tracked_issues"][0]["title"] == "**REDACTED**"
    assert result["dashboard"]["open_issues"] == 7
