"""Tests for setting up and unloading the Paperclip integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.paperclip import async_remove_config_entry_device
from custom_components.paperclip.const import (
    CONF_DEFAULT_PROJECT,
    DOMAIN,
    EVENT_AGENT_REPLIED,
)

from .conftest import COMPANY_ID, ENGINEER_ID, HOME_OPS_ID, PROJECT_ID, FakePaperclip


def _device(hass: HomeAssistant, key: str) -> dr.DeviceEntry | None:
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, key), entry.entry_id
    )


async def test_setup_and_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The entry loads, creates devices and unloads cleanly."""
    entry = init_integration
    assert entry.state is ConfigEntryState.LOADED

    company = _device(hass, COMPANY_ID)
    assert company is not None
    assert company.name == "Acme"
    assert company.sw_version == "2026.1006.0-beta.0"
    engineer = _device(hass, ENGINEER_ID)
    assert engineer is not None
    assert engineer.via_device_id == company.id
    assert engineer.model == "Software Engineer"
    home_ops = _device(hass, HOME_OPS_ID)
    assert home_ops is not None
    assert home_ops.model == "General"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_retries_when_unreachable(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """A server error during the first refresh means setup is retried."""
    fake.fail("GET", "/api/health", 503, "database_unreachable")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_auth_failure_starts_reauth(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """A 401 during the first refresh starts a reauth flow."""
    fake.fail("GET", "/api/health", 401, "Board authentication required")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == SOURCE_REAUTH for flow in flows)


async def test_hostname_guard_is_not_auth_failure(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """The private-hostname guard's 403 is retried, not treated as a bad key."""
    fake.fail(
        "GET",
        "/api/health",
        403,
        "This hostname is not allowed for this Paperclip instance.",
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_rate_limited_and_missing_company(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """Rate limits and a deleted company both retry setup."""
    fake.fail("GET", "/api/health", 429, "Too many requests")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY

    fake.clear_failures()
    fake.fail("GET", f"/api/companies/{COMPANY_ID}", 404, "Company not found")
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_optional_routes_missing(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """A 404 on an optional route is treated as "not supported"."""
    fake.fail("GET", f"/api/companies/{COMPANY_ID}/dashboard", 404, "Not found")
    fake.fail("GET", f"/api/companies/{COMPANY_ID}/live-runs", 404, "Not found")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.acme_open_issues").state == "unavailable"
    assert hass.states.get("sensor.acme_active_runs").state == "unavailable"
    # Spend falls back to the company row, approvals to the approvals list.
    assert hass.states.get("sensor.acme_spend_this_month").state == "12.34"
    assert hass.states.get("sensor.acme_pending_approvals").state == "1"
    coordinator = mock_config_entry.runtime_data.coordinator
    assert coordinator.unsupported == {"dashboard", "live_runs"}


async def test_outdated_server_raises_repair_issue(
    hass: HomeAssistant,
    fake: FakePaperclip,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An old Paperclip version creates a repair issue, cleared after upgrade."""
    fake.health["version"] = "2025.1201.0"
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    issue_id = f"outdated_paperclip_{mock_config_entry.entry_id}"
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, issue_id) is not None

    fake.health["version"] = "2026.1020.0"
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert issues.async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize(
    ("version", "outdated"),
    [
        ("2026.1006.0-beta.0", False),
        ("2026.1006.0", False),
        ("2026.1005.3", True),
        ("2026.1001.0-canary.4", True),
        # A source checkout reports the package.json placeholder: unknown age.
        ("0.3.1", False),
        ("0.0.0", False),
        # Build metadata (commits since the tag) does not change the release.
        ("2026.1006.0+12.git.0123abc", False),
        ("2025.1201.0+3.git.0123abc.dirty", True),
        (None, False),
    ],
)
async def test_server_version_comparison(
    hass: HomeAssistant,
    fake: FakePaperclip,
    mock_config_entry: MockConfigEntry,
    version: str | None,
    outdated: bool,
) -> None:
    """Only real CalVer releases older than the tested one are flagged."""
    fake.health["version"] = version
    fake.health["serverVersion"] = version
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"outdated_paperclip_{mock_config_entry.entry_id}"
    )
    assert (issue is not None) is outdated

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()


async def test_stale_agent_device_removed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake: FakePaperclip,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A terminated agent disappears from the list and loses its device."""
    registry = dr.async_get(hass)
    assert _device(hass, HOME_OPS_ID) is not None
    fake.agents = [a for a in fake.agents if a["id"] != HOME_OPS_ID]
    fake.agents[0]["name"] = "Chief Executive"
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _device(hass, HOME_OPS_ID) is None
    assert hass.states.get("switch.home_ops_enabled") is None
    ceo = next(
        d
        for d in dr.async_entries_for_config_entry(registry, init_integration.entry_id)
        if d.name == "Chief Executive"
    )
    assert ceo.model == "CEO"


async def test_remove_config_entry_device(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Devices of live agents cannot be removed by hand; stale ones can."""
    assert await async_setup_component(hass, "config", {})
    registry = dr.async_get(hass)
    engineer = _device(hass, ENGINEER_ID)
    assert engineer is not None
    assert not await async_remove_config_entry_device(hass, init_integration, engineer)
    stale = registry.async_get_or_create(
        config_entry_id=init_integration.entry_id,
        identifiers={(DOMAIN, "33333333-3333-4333-8333-000000000000")},
    )
    assert await async_remove_config_entry_device(hass, init_integration, stale)


async def test_options_flow_lists_projects(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """With the entry loaded, the project selector lists Paperclip projects."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    schema = result["data_schema"].schema
    selector = next(
        value for key, value in schema.items() if key == CONF_DEFAULT_PROJECT
    )
    values = [option["value"] for option in selector.config["options"]]
    assert PROJECT_ID in values


async def test_agent_devices_are_services(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Agents are software, so their devices are service devices."""
    for key in (COMPANY_ID, ENGINEER_ID, HOME_OPS_ID):
        device = _device(hass, key)
        assert device is not None
        assert device.entry_type is dr.DeviceEntryType.SERVICE


async def test_reload_ends_reply_waits(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """A wait running during a reload ends; the reply then fires only once."""
    events = async_capture_events(hass, EVENT_AGENT_REPLIED)
    old = init_integration.runtime_data.coordinator
    task = hass.async_create_task(
        hass.services.async_call(
            DOMAIN,
            "ask_agent",
            {"agent": "Engineer", "message": "Check the backups", "wait_for_reply": 60},
            blocking=True,
            return_response=True,
        )
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        if old._waiting:
            break
    assert old._waiting

    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    response = await asyncio.wait_for(task, 5)
    assert response is not None
    assert response["replied"] is False
    assert not old._waiting
    # The old coordinator keeps its hands off the tracking data now.
    assert old.tracker._closed

    issue_id = response["issue_id"]
    fake.agent_comment(issue_id, ENGINEER_ID, "Backups are fine.")
    await old.async_wait_for_reply(old.tracker.issues[issue_id], 1)
    new = init_integration.runtime_data.coordinator
    assert new is not old
    await new.async_refresh()
    await new.async_refresh()
    assert [(e.data["body"], e.data["delivered"]) for e in events] == [
        ("Backups are fine.", False)
    ]
