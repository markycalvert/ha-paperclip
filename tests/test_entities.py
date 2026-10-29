"""Tests for Paperclip entities and the events fired on changes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.switch import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.paperclip.const import (
    EVENT_AGENT_STATUS_CHANGED,
    EVENT_APPROVAL_REQUESTED,
)

from .conftest import (
    APPROVAL_ID,
    CEO_ID,
    COMPANY_ID,
    ENGINEER_ID,
    HOME_OPS_ID,
    FakePaperclip,
)


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_company_entities(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Company sensors reflect the dashboard and lists."""
    assert hass.states.get("binary_sensor.acme_server").state == STATE_ON
    assert hass.states.get("sensor.acme_open_issues").state == "7"
    assert hass.states.get("sensor.acme_pending_approvals").state == "1"
    assert hass.states.get("sensor.acme_active_runs").state == "1"
    spend = hass.states.get("sensor.acme_spend_this_month")
    assert spend.state == "12.34"
    assert spend.attributes["unit_of_measurement"] == "USD"

    registry = er.async_get(hass)
    entry = registry.async_get("sensor.acme_open_issues")
    assert entry.unique_id == f"{COMPANY_ID}_open_issues"


async def test_agent_entities(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Every agent gets a status sensor, task sensor, switch and button."""
    status = hass.states.get("sensor.engineer_status")
    assert status.state == "running"
    assert "pending_approval" in status.attributes["options"]
    assert hass.states.get("sensor.ceo_status").state == "idle"
    assert hass.states.get("sensor.home_ops_status").state == "paused"

    task = hass.states.get("sensor.engineer_current_task")
    assert task.state == "ACME-7 Update the dependency list"
    assert task.attributes["run_status"] == "running"
    assert task.attributes["url"] == "http://paperclip.local:3100/ACME/issues/ACME-7"
    assert hass.states.get("sensor.ceo_current_task").state == "none"

    assert hass.states.get("switch.ceo_enabled").state == STATE_ON
    assert hass.states.get("switch.home_ops_enabled").state == STATE_OFF
    assert hass.states.get("button.ceo_wake") is not None

    registry = er.async_get(hass)
    for entity_id in ("sensor.ceo_spend_this_month", "sensor.ceo_last_heartbeat"):
        entry = registry.async_get(entity_id)
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert (
        registry.async_get("conversation.ceo").unique_id
        == f"{COMPANY_ID}_{CEO_ID}_agent"
    )


async def test_disabled_agent_sensors(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """The disabled-by-default sensors work once enabled."""
    registry = er.async_get(hass)
    mock_config_entry.add_to_hass(hass)
    for key in ("agent_spend_this_month", "last_heartbeat"):
        registry.async_get_or_create(
            "sensor",
            "paperclip",
            f"{COMPANY_ID}_{CEO_ID}_{key}",
            config_entry=mock_config_entry,
            suggested_object_id=f"ceo_{key}",
            disabled_by=None,
        )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ceo_agent_spend_this_month").state == "8.0"
    assert (
        hass.states.get("sensor.ceo_last_heartbeat").state
        == "2026-01-05T08:55:00+00:00"
    )


async def test_current_task_fallbacks(
    hass: HomeAssistant,
    fake: FakePaperclip,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A run on an unknown issue shows the issue id; queued yields to running."""
    fake.live_runs = [
        {"id": "r1", "status": "queued", "agentId": CEO_ID, "issueId": "missing"},
        {"id": "r2", "status": "running", "agentId": CEO_ID, "issueId": None},
        {"id": "r3", "status": "queued", "agentId": ENGINEER_ID, "issueId": "gone"},
    ]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ceo_current_task").state == "running"
    assert hass.states.get("sensor.engineer_current_task").state == "gone"


async def test_connectivity_off_when_unreachable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake: FakePaperclip,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The server sensor turns off instead of becoming unavailable."""
    fake.fail("GET", "/api/health", 503, "database_unreachable")
    await _poll(hass, freezer)
    assert hass.states.get("binary_sensor.acme_server").state == STATE_OFF
    assert hass.states.get("sensor.acme_open_issues").state == "unavailable"


async def test_switch_pause_and_resume(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """The switch pauses and resumes the agent."""
    await hass.services.async_call(
        "switch",
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: "switch.ceo_enabled"},
        blocking=True,
    )
    assert fake.calls_to("POST", f"/api/agents/{CEO_ID}/pause")
    await hass.async_block_till_done()
    assert hass.states.get("switch.ceo_enabled").state == STATE_OFF

    await hass.services.async_call(
        "switch",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: "switch.home_ops_enabled"},
        blocking=True,
    )
    assert fake.calls_to("POST", f"/api/agents/{HOME_OPS_ID}/resume")

    fake.fail("POST", f"/api/agents/{CEO_ID}/resume", 409, "Cannot resume")
    with pytest.raises(HomeAssistantError, match="Could not resume CEO"):
        await hass.services.async_call(
            "switch",
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: "switch.ceo_enabled"},
            blocking=True,
        )


async def test_wake_button(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """The wake button asks for an on-demand run."""
    await hass.services.async_call(
        "button", SERVICE_PRESS, {ATTR_ENTITY_ID: "button.ceo_wake"}, blocking=True
    )
    call = fake.calls_to("POST", f"/api/agents/{CEO_ID}/wakeup")[0]
    assert call[3]["source"] == "on_demand"

    fake.fail("POST", f"/api/agents/{CEO_ID}/wakeup", 409, "Agent is not invokable")
    with pytest.raises(HomeAssistantError, match="Could not wake CEO"):
        await hass.services.async_call(
            "button", SERVICE_PRESS, {ATTR_ENTITY_ID: "button.ceo_wake"}, blocking=True
        )


async def test_agent_status_event_and_new_agent(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake: FakePaperclip,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A status change fires an event; a new agent gets entities."""
    events = async_capture_events(hass, EVENT_AGENT_STATUS_CHANGED)
    fake.agents[0]["status"] = "error"
    fake.agents.append(
        {
            **fake.agents[1],
            "id": "33333333-3333-4333-8333-333333333336",
            "name": "Designer",
            "role": "designer",
            "title": None,
            "status": "idle",
        }
    )
    await _poll(hass, freezer)
    assert len(events) == 1
    assert events[0].data == {
        "config_entry_id": init_integration.entry_id,
        "company_id": COMPANY_ID,
        "agent_id": CEO_ID,
        "agent_name": "CEO",
        "old_status": "idle",
        "new_status": "error",
    }
    assert hass.states.get("sensor.ceo_status").state == "error"
    assert hass.states.get("sensor.designer_status").state == "idle"


async def test_approval_event(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake: FakePaperclip,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Only approvals that are new since the last poll fire an event."""
    events = async_capture_events(hass, EVENT_APPROVAL_REQUESTED)
    await _poll(hass, freezer)
    assert events == []

    new_id = "99999999-9999-4999-8999-999999999998"
    fake.approvals.append(
        {
            "id": new_id,
            "companyId": COMPANY_ID,
            "type": "budget_override_required",
            "requestedByAgentId": ENGINEER_ID,
            "status": "pending",
            "payload": {"summary": "Raise the monthly limit"},
            "createdAt": "2026-01-05T09:30:00.000Z",
        }
    )
    await _poll(hass, freezer)
    assert len(events) == 1
    assert events[0].data == {
        "config_entry_id": init_integration.entry_id,
        "company_id": COMPANY_ID,
        "approval_id": new_id,
        "type": "budget_override_required",
        "title": "Budget Override: Raise the monthly limit",
        "requested_by_agent_id": ENGINEER_ID,
        "requested_by_agent_name": "Engineer",
        "url": f"http://paperclip.local:3100/ACME/approvals/{new_id}",
    }
    assert hass.states.get("sensor.acme_pending_approvals").state == "2"
    assert APPROVAL_ID in {a["id"] for a in fake.approvals}


async def test_spend_sensors_reset_monthly(
    hass: HomeAssistant,
    fake: FakePaperclip,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Month-to-date spend totals report the UTC month start as last_reset."""
    freezer.move_to("2026-02-14T15:30:00+00:00")
    mock_config_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        "paperclip",
        f"{COMPANY_ID}_{CEO_ID}_agent_spend_this_month",
        config_entry=mock_config_entry,
        suggested_object_id="ceo_agent_spend_this_month",
        disabled_by=None,
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    for entity_id in (
        "sensor.acme_spend_this_month",
        "sensor.ceo_agent_spend_this_month",
    ):
        state = hass.states.get(entity_id)
        assert state.attributes["state_class"] == "total"
        assert state.attributes["last_reset"] == "2026-02-01T00:00:00+00:00"
    assert "last_reset" not in hass.states.get("sensor.acme_open_issues").attributes
