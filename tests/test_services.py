"""Tests for the Paperclip actions."""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from pathlib import Path
from typing import Any

import probatio
import pytest
import yaml
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.paperclip.const import (
    CONF_ALLOW_APPROVALS,
    CONF_COMPANY_ID,
    CONF_DEFAULT_PROJECT,
    DOMAIN,
    EVENT_AGENT_REPLIED,
    EVENT_ISSUE_STATUS_CHANGED,
)

from .conftest import (
    APPROVAL_ID,
    CEO_ID,
    COMPANY_ID,
    ENGINEER_ID,
    HOME_OPS_ID,
    OTHER_AGENT_ID,
    OTHER_COMPANY_ID,
    PROJECT_ID,
    PUBLIC_ID,
    RUNNING_ISSUE_ID,
    FakePaperclip,
)


async def _call(
    hass: HomeAssistant, service: str, data: dict[str, Any]
) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )
    assert isinstance(response, dict)
    return response


def _agent_device(hass: HomeAssistant, entry: MockConfigEntry, agent_id: str) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, agent_id), entry.entry_id
    )
    assert device is not None
    return device.id


async def test_ask_agent_without_wait(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """ask_agent creates an issue assigned to the chosen agent device."""
    response = await _call(
        hass,
        "ask_agent",
        {
            "agent": _agent_device(hass, init_integration, HOME_OPS_ID),
            "message": (
                "Is the garage door closed? If it is open, find out who opened it."
            ),
            "priority": "high",
        },
    )
    body = fake.calls_to("POST", f"/api/companies/{COMPANY_ID}/issues")[0][3]
    assert body["assigneeAgentId"] == HOME_OPS_ID
    assert body["allowDuplicate"] is True
    assert body["priority"] == "high"
    assert body["title"] == "Is the garage door closed? If it is open, find out who…"
    assert response == {
        "issue_id": "55555555-5555-4555-8555-000000000043",
        "identifier": "ACME-43",
        "url": "http://paperclip.local:3100/ACME/issues/ACME-43",
        "status": "todo",
        "agent_id": HOME_OPS_ID,
        "agent_name": "Home Ops",
        "replied": False,
        "reply": None,
        "reply_comment_id": None,
    }


async def test_ask_agent_waits_for_reply(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """With wait_for_reply, the agent's first comment comes back."""
    events = async_capture_events(hass, EVENT_AGENT_REPLIED)
    polls = 0

    def reply_on_second_poll(issue_id: str) -> None:
        nonlocal polls
        polls += 1
        if polls == 2:
            fake.agent_comment(issue_id, CEO_ID, "The garage door is closed.")

    fake.on_get_comments = reply_on_second_poll
    response = await _call(
        hass,
        "ask_agent",
        {
            "agent": "ceo",
            "message": "Is the garage door closed?",
            "title": "Garage door",
            "project": "Household",
            "wait_for_reply": 5,
        },
    )
    assert response["replied"] is True
    assert response["reply"] == "The garage door is closed."
    body = fake.calls_to("POST", f"/api/companies/{COMPANY_ID}/issues")[0][3]
    assert body["title"] == "Garage door"
    assert body["projectId"] == PROJECT_ID
    assert len(events) == 1
    assert events[0].data["delivered"] is True
    assert events[0].data["agent_name"] == "CEO"
    assert events[0].data["identifier"] == "ACME-43"


async def test_ask_agent_wait_times_out(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """No reply within the wait returns the issue without a reply."""
    response = await _call(
        hass,
        "ask_agent",
        {
            "agent": ENGINEER_ID,
            "message": "Summarise the build log",
            "wait_for_reply": 0.02,
        },
    )
    assert response["replied"] is False
    assert response["identifier"] == "ACME-43"


async def test_ask_agent_reply_arrives_later(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake: FakePaperclip,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A late reply and later status changes arrive as events."""
    replies = async_capture_events(hass, EVENT_AGENT_REPLIED)
    statuses = async_capture_events(hass, EVENT_ISSUE_STATUS_CHANGED)
    response = await _call(
        hass, "ask_agent", {"agent": "Engineer", "message": "Check the backups"}
    )
    issue_id = response["issue_id"]
    fake.agent_comment(issue_id, ENGINEER_ID, "x" * 2500)
    fake.set_issue_status(issue_id, "done")

    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(replies) == 1
    data = replies[0].data
    assert data["delivered"] is False
    assert data["agent_id"] == ENGINEER_ID
    assert len(data["body"]) == 2000
    assert data["url"] == "http://paperclip.local:3100/ACME/issues/ACME-43"
    assert len(statuses) == 1
    assert statuses[0].data["old_status"] == "todo"
    assert statuses[0].data["new_status"] == "done"

    # Nothing new: no repeated events.
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(replies) == 1
    assert len(statuses) == 1


async def test_ask_agent_uses_default_project(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """The default project option applies when the action names none."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={**mock_config_entry.options, CONF_DEFAULT_PROJECT: PROJECT_ID},
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await _call(hass, "ask_agent", {"agent": CEO_ID, "message": "Plan the week"})
    body = fake.calls_to("POST", f"/api/companies/{COMPANY_ID}/issues")[0][3]
    assert body["projectId"] == PROJECT_ID


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"agent": "Nobody", "message": "Hi"}, "agent_not_found"),
        ({"agent": CEO_ID, "message": "Hi", "project": "Garden"}, "project_not_found"),
    ],
)
async def test_ask_agent_validation(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    data: dict[str, Any],
    match: str,
) -> None:
    """Unknown agents and projects are caller errors."""
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "ask_agent", data)
    assert err.value.translation_key == match


async def test_ask_agent_company_device_is_not_an_agent(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Picking the company device is refused."""
    device = _agent_device(hass, init_integration, COMPANY_ID)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "ask_agent", {"agent": device, "message": "Hi"})
    assert err.value.translation_key == "not_an_agent"


async def test_ask_agent_ambiguous_across_entries(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """An agent name matching agents of two entries needs the entry id."""
    second = MockConfigEntry(
        domain=DOMAIN,
        title="Acme copy",
        unique_id="other",
        data=dict(init_integration.data),
        options=dict(init_integration.options),
    )
    second.add_to_hass(hass)
    await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "ask_agent", {"agent": "CEO", "message": "Hi"})
    assert err.value.translation_key == "agent_ambiguous"
    response = await _call(
        hass,
        "ask_agent",
        {"agent": "CEO", "message": "Hi", "config_entry_id": second.entry_id},
    )
    assert response["agent_id"] == CEO_ID


async def test_api_errors_become_home_assistant_errors(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """Upstream failures raise HomeAssistantError; 401 also starts reauth."""
    path = f"/api/companies/{COMPANY_ID}/issues"
    fake.fail("POST", path, 409, "Cannot assign work to terminated agents")
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "ask_agent", {"agent": CEO_ID, "message": "Hi"})
    assert err.value.translation_key == "api_error"

    fake.fail("POST", path, 401, "Board authentication required")
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "ask_agent", {"agent": CEO_ID, "message": "Hi"})
    assert err.value.translation_key == "invalid_auth"
    await hass.async_block_till_done()
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_create_issue(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """create_issue creates (or deduplicates) and tracks an issue."""
    entry_id = init_integration.entry_id
    response = await _call(
        hass,
        "create_issue",
        {
            "config_entry_id": entry_id,
            "title": "Replace the hallway smoke alarm battery",
            "description": "Low battery reported at 07:00.",
            "assignee": "Home Ops",
            "priority": "low",
        },
    )
    assert response["identifier"] == "ACME-43"
    assert response["assignee_agent_id"] == HOME_OPS_ID
    body = fake.calls_to("POST", f"/api/companies/{COMPANY_ID}/issues")[0][3]
    assert "allowDuplicate" not in body
    assert body["description"] == "Low battery reported at 07:00."

    again = await _call(
        hass,
        "create_issue",
        {
            "config_entry_id": entry_id,
            "title": "Replace the hallway smoke alarm battery",
        },
    )
    assert again["issue_id"] == response["issue_id"]

    unassigned = await _call(
        hass,
        "create_issue",
        {
            "config_entry_id": entry_id,
            "title": "Replace the hallway smoke alarm battery",
            "allow_duplicate": True,
        },
    )
    assert unassigned["identifier"] == "ACME-44"
    assert unassigned["status"] == "backlog"
    tracker = init_integration.runtime_data.coordinator.tracker
    assert set(tracker.issues) >= {response["issue_id"], unassigned["issue_id"]}


async def test_add_comment_tracks_and_waits(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """add_comment follows the issue and returns a reply posted after it."""
    fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "Old progress note.")
    polls = 0

    def reply(issue_id: str) -> None:
        nonlocal polls
        polls += 1
        if polls == 1:
            fake.agent_comment(issue_id, ENGINEER_ID, "Updated, see the diff.")

    fake.on_get_comments = reply
    response = await _call(
        hass,
        "add_comment",
        {
            "config_entry_id": init_integration.entry_id,
            "issue": "ACME-7",
            "message": "Please also update the lock file.",
            "wait_for_reply": 5,
        },
    )
    assert response["replied"] is True
    assert response["reply"] == "Updated, see the diff."
    assert response["identifier"] == "ACME-7"
    tracker = init_integration.runtime_data.coordinator.tracker
    assert RUNNING_ISSUE_ID in tracker.issues

    # Commenting again uses the tracked issue.
    fake.on_get_comments = None
    again = await _call(
        hass,
        "add_comment",
        {
            "config_entry_id": init_integration.entry_id,
            "issue": RUNNING_ISSUE_ID,
            "message": "Thanks.",
        },
    )
    assert again["replied"] is False

    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "add_comment",
            {
                "config_entry_id": init_integration.entry_id,
                "issue": "ACME-999",
                "message": "Hello",
            },
        )
    assert err.value.translation_key == "not_found"


async def test_wake_agent(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """wake_agent requests an on-demand run with the given reason."""
    response = await _call(
        hass,
        "wake_agent",
        {
            "agent": _agent_device(hass, init_integration, CEO_ID),
            "reason": "Motion in the office",
        },
    )
    assert response == {"agent_id": CEO_ID, "status": "queued"}
    body = fake.calls_to("POST", f"/api/agents/{CEO_ID}/wakeup")[0][3]
    assert body["reason"] == "Motion in the office"


async def test_fire_routine_webhook_signs_request(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake: FakePaperclip,
    aioclient_mock: Any,
) -> None:
    """The webhook is HMAC-signed and carries no Authorization header."""
    payload = {"event": "doorbell", "variables": {"room": "hall"}}
    response = await _call(
        hass,
        "fire_routine_webhook",
        {
            "config_entry_id": init_integration.entry_id,
            "public_id": f"http://paperclip.local:3100/api/routine-triggers/public/{PUBLIC_ID}/fire",
            "secret": "test-secret",
            "payload": payload,
            "idempotency_key": "doorbell-1",
        },
    )
    assert response == {
        "http_status": 202,
        "status": "issue_created",
        "run_id": "88888888-8888-4888-8888-888888888888",
        "linked_issue_id": RUNNING_ISSUE_ID,
    }
    _, url, data, headers = aioclient_mock.mock_calls[-1]
    assert url.path.endswith(f"/public/{PUBLIC_ID}/fire")
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"
    assert headers["Idempotency-Key"] == "doorbell-1"
    timestamp = headers["X-Paperclip-Timestamp"]
    expected = hmac.new(
        b"test-secret", timestamp.encode() + b"." + data, hashlib.sha256
    ).hexdigest()
    assert headers["X-Paperclip-Signature"] == f"sha256={expected}"
    assert data == b'{"event":"doorbell","variables":{"room":"hall"}}'


async def test_fire_routine_webhook_errors(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """Bad ids, rejected signatures and unreachable servers are reported."""
    entry_id = init_integration.entry_id
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "fire_routine_webhook",
            {"config_entry_id": entry_id, "public_id": "not-an-id"},
        )
    assert err.value.translation_key == "invalid_public_id"

    path = f"/api/routine-triggers/public/{PUBLIC_ID}/fire"
    fake.fail("POST", path, 401, "Unauthorized")
    with pytest.raises(HomeAssistantError) as err:
        await _call(
            hass,
            "fire_routine_webhook",
            {"config_entry_id": entry_id, "public_id": PUBLIC_ID},
        )
    assert err.value.translation_key == "webhook_rejected"
    assert err.value.translation_placeholders == {
        "status": "401",
        "error": "Unauthorized",
    }
    # A bad trigger secret must never start a reauth of the board key.
    assert not hass.config_entries.flow.async_progress()


async def test_approvals_gate(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """Approve and reject refuse to run until the option is turned on."""
    for service in ("approve", "reject"):
        with pytest.raises(ServiceValidationError) as err:
            await _call(
                hass,
                service,
                {
                    "config_entry_id": init_integration.entry_id,
                    "approval_id": APPROVAL_ID,
                },
            )
        assert err.value.translation_key == "approvals_disabled"
    assert not fake.calls_to("POST", f"/api/approvals/{APPROVAL_ID}/approve")


async def test_approve_and_reject(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """With the option on, decisions reach Paperclip."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={**mock_config_entry.options, CONF_ALLOW_APPROVALS: True},
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    response = await _call(
        hass,
        "approve",
        {
            "config_entry_id": mock_config_entry.entry_id,
            "approval_id": APPROVAL_ID,
            "decision_note": "Go ahead.",
        },
    )
    assert response == {"approval_id": APPROVAL_ID, "status": "approved"}
    assert fake.calls_to("POST", f"/api/approvals/{APPROVAL_ID}/approve")[0][3] == {
        "decisionNote": "Go ahead."
    }
    response = await _call(
        hass,
        "reject",
        {"config_entry_id": mock_config_entry.entry_id, "approval_id": APPROVAL_ID},
    )
    assert response["status"] == "rejected"


async def test_actions_need_a_loaded_entry(
    hass: HomeAssistant, fake: FakePaperclip
) -> None:
    """Actions exist without entries and fail with a clear error."""
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError):
        await _call(
            hass, "create_issue", {"config_entry_id": "missing", "title": "Hello"}
        )
    with pytest.raises(ServiceValidationError):
        await _call(hass, "ask_agent", {"agent": "CEO", "message": "Hi"})


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("create_issue", {"title": "Hello"}),
        ("add_comment", {"issue": "ACME-7", "message": "Hello"}),
        ("fire_routine_webhook", {"public_id": PUBLIC_ID}),
        ("approve", {"approval_id": APPROVAL_ID}),
        ("reject", {"approval_id": APPROVAL_ID}),
    ],
)
async def test_entry_level_actions_need_the_entry(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake: FakePaperclip,
    service: str,
    data: dict[str, Any],
) -> None:
    """Actions on a company or server have no implicit default target."""
    with pytest.raises(probatio.Invalid):
        await _call(hass, service, data)
    assert not [call for call in fake.calls if call[0] == "POST"]


def test_agent_pickers_only_offer_agent_devices() -> None:
    """The device pickers only list devices with a Paperclip conversation agent."""
    path = Path(__file__).parents[1] / "custom_components" / DOMAIN / "services.yaml"
    services = yaml.safe_load(path.read_text(encoding="utf-8"))
    for service, field in (
        ("ask_agent", "agent"),
        ("create_issue", "assignee"),
        ("wake_agent", "agent"),
    ):
        config = services[service]["fields"][field]["selector"]
        selector.validate_selector(config)
        assert config["device"] == {
            "integration": DOMAIN,
            "entity": [{"integration": DOMAIN, "domain": "conversation"}],
        }
    for service in ("create_issue", "add_comment", "fire_routine_webhook"):
        assert services[service]["fields"]["config_entry_id"]["required"] is True
    for service in ("approve", "reject"):
        assert services[service]["fields"]["config_entry_id"]["required"] is True


async def test_no_response_variable_skips_the_wait(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """Without a response consumer, actions return nothing and do not wait."""
    response = await hass.services.async_call(
        DOMAIN,
        "ask_agent",
        {"agent": CEO_ID, "message": "Plan the week", "wait_for_reply": 300},
        blocking=True,
    )
    assert response is None
    assert len(fake.calls_to("POST", f"/api/companies/{COMPANY_ID}/issues")) == 1
    assert not [call for call in fake.calls if call[1].endswith("/comments")]

    response = await hass.services.async_call(
        DOMAIN,
        "add_comment",
        {
            "config_entry_id": init_integration.entry_id,
            "issue": "ACME-7",
            "message": "Any news?",
            "wait_for_reply": 300,
        },
        blocking=True,
    )
    assert response is None
    comment_reads = fake.calls_to("GET", f"/api/issues/{RUNNING_ISSUE_ID}/comments")
    assert not comment_reads

    for service, data in (
        (
            "create_issue",
            {"config_entry_id": init_integration.entry_id, "title": "Hello"},
        ),
        ("wake_agent", {"agent": CEO_ID}),
        (
            "fire_routine_webhook",
            {"config_entry_id": init_integration.entry_id, "public_id": PUBLIC_ID},
        ),
    ):
        assert (
            await hass.services.async_call(DOMAIN, service, data, blocking=True) is None
        )


async def test_forbidden_action_does_not_start_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """A 403 on one write is reported with the server's reason; the key is kept."""
    fake.fail("POST", f"/api/agents/{CEO_ID}/wakeup", 403, "Missing agent:wake grant")
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "wake_agent", {"agent": CEO_ID})
    assert err.value.translation_key == "forbidden"
    assert err.value.translation_placeholders == {"error": "Missing agent:wake grant"}

    fake.fail(
        "POST", f"/api/companies/{COMPANY_ID}/issues", 403, "Viewer access is read-only"
    )
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "ask_agent", {"agent": CEO_ID, "message": "Hi"})
    assert err.value.translation_key == "forbidden"
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress()


async def test_deduplicated_issue_does_not_replay_old_comments(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """An existing open issue handed back by deduplication is followed from now."""
    events = async_capture_events(hass, EVENT_AGENT_REPLIED)
    # Created elsewhere (the Paperclip UI), with an agent conversation on it.
    fake.issues[RUNNING_ISSUE_ID]["title"] = "Water the plants"
    fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "Old answer one.")
    fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "Old answer two.")

    response = await _call(
        hass,
        "create_issue",
        {"config_entry_id": init_integration.entry_id, "title": "Water the plants"},
    )
    assert response["issue_id"] == RUNNING_ISSUE_ID
    newest = fake.calls_to("GET", f"/api/issues/{RUNNING_ISSUE_ID}/comments")
    assert newest[-1][2] == {"order": "desc", "limit": "1"}

    coordinator = init_integration.runtime_data.coordinator
    await coordinator.async_refresh()
    assert events == []

    fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "A new answer.")
    await coordinator.async_refresh()
    assert [event.data["body"] for event in events] == ["A new answer."]


async def test_default_project_name_is_resolved(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """A default project stored as a name is sent to Paperclip as its id."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={**mock_config_entry.options, CONF_DEFAULT_PROJECT: "household"},
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await _call(hass, "ask_agent", {"agent": CEO_ID, "message": "Plan the week"})
    body = fake.calls_to("POST", f"/api/companies/{COMPANY_ID}/issues")[0][3]
    assert body["projectId"] == PROJECT_ID

    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={**mock_config_entry.options, CONF_DEFAULT_PROJECT: "Garden"},
    )
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "ask_agent", {"agent": CEO_ID, "message": "Plan again"})
    assert err.value.translation_key == "project_not_found"
    assert err.value.translation_placeholders == {"project": "Garden"}


async def test_new_project_is_found_without_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """A project created after the first lookup is found by name."""
    await _call(
        hass,
        "ask_agent",
        {"agent": CEO_ID, "message": "Hi", "project": "Household"},
    )
    new_project = "77777777-7777-4777-8777-777777777778"
    fake.projects = [*fake.projects, {"id": new_project, "name": "Garden"}]
    await _call(
        hass,
        "ask_agent",
        {"agent": CEO_ID, "message": "Mow the lawn", "project": "Garden"},
    )
    body = fake.calls_to("POST", f"/api/companies/{COMPANY_ID}/issues")[-1][3]
    assert body["projectId"] == new_project


async def _second_company(
    hass: HomeAssistant, first: MockConfigEntry
) -> MockConfigEntry:
    """Add an entry for the server's second company."""
    second = MockConfigEntry(
        domain=DOMAIN,
        title="Globex",
        unique_id=OTHER_COMPANY_ID,
        data={**first.data, CONF_COMPANY_ID: OTHER_COMPANY_ID},
        options=dict(first.options),
    )
    second.add_to_hass(hass)
    await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    return second


async def test_agent_device_of_another_entry_is_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """An agent device must belong to the entry the action names."""
    second = await _second_company(hass, init_integration)
    gardener = _agent_device(hass, second, OTHER_AGENT_ID)
    first_id = init_integration.entry_id
    for service, data in (
        ("create_issue", {"title": "Mow the lawn", "assignee": gardener}),
        ("ask_agent", {"agent": gardener, "message": "Mow the lawn"}),
        ("wake_agent", {"agent": gardener}),
    ):
        with pytest.raises(ServiceValidationError) as err:
            await _call(hass, service, {"config_entry_id": first_id, **data})
        assert err.value.translation_key == "agent_wrong_entry"
        assert err.value.translation_placeholders == {
            "agent": "Gardener",
            "title": "Globex",
        }
    assert not [call for call in fake.calls if call[0] == "POST"]

    # Without an entry, or with its own entry, the device picks its company.
    response = await _call(hass, "wake_agent", {"agent": gardener})
    assert response["agent_id"] == OTHER_AGENT_ID
    response = await _call(
        hass, "wake_agent", {"agent": gardener, "config_entry_id": second.entry_id}
    )
    assert response["agent_id"] == OTHER_AGENT_ID


async def test_create_issue_duplicate_of_another_agent(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """A title match on another agent's open issue is not reported as success."""
    entry_id = init_integration.entry_id
    first = await _call(
        hass,
        "create_issue",
        {"config_entry_id": entry_id, "title": "Check the gate", "assignee": "CEO"},
    )
    assert first["deduplicated"] is False
    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "create_issue",
            {
                "config_entry_id": entry_id,
                "title": "Check the gate",
                "assignee": "Engineer",
            },
        )
    assert err.value.translation_key == "duplicate_issue_other_assignee"
    assert err.value.translation_placeholders == {
        "identifier": first["identifier"],
        "agent": "Engineer",
    }
    tracker = init_integration.runtime_data.coordinator.tracker
    assert tracker.issues[first["issue_id"]].agent_id == CEO_ID

    # Without an assignee the existing issue is returned with its real assignee.
    again = await _call(
        hass, "create_issue", {"config_entry_id": entry_id, "title": "Check the gate"}
    )
    assert again["issue_id"] == first["issue_id"]
    assert again["deduplicated"] is True
    assert again["assignee_agent_id"] == CEO_ID


@pytest.mark.parametrize(
    "approval_id",
    ["../agents/33333333-3333-4333-8333-333333333333/pause?a=", "not-a-uuid", ""],
)
async def test_approval_id_must_be_a_uuid(
    hass: HomeAssistant,
    fake: FakePaperclip,
    mock_config_entry: MockConfigEntry,
    approval_id: str,
) -> None:
    """An approval id that is not a UUID never reaches Paperclip."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={**mock_config_entry.options, CONF_ALLOW_APPROVALS: True},
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises((ServiceValidationError, probatio.Invalid)) as err:
        await _call(
            hass,
            "approve",
            {
                "config_entry_id": mock_config_entry.entry_id,
                "approval_id": approval_id,
            },
        )
    if isinstance(err.value, ServiceValidationError):
        assert err.value.translation_key == "invalid_approval_id"
    assert not [call for call in fake.calls if call[0] == "POST"]


async def test_approval_of_another_company_is_refused(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """The approvals option of one entry cannot decide another company's approval."""
    foreign = "99999999-9999-4999-8999-999999999998"
    revised = "99999999-9999-4999-8999-999999999997"
    fake.approvals += [
        {**fake.approvals[0], "id": foreign, "companyId": OTHER_COMPANY_ID},
        {**fake.approvals[0], "id": revised, "status": "revision_requested"},
    ]
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={**mock_config_entry.options, CONF_ALLOW_APPROVALS: True},
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    entry_id = mock_config_entry.entry_id

    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass, "approve", {"config_entry_id": entry_id, "approval_id": foreign}
        )
    assert err.value.translation_key == "approval_wrong_company"
    assert not fake.calls_to("POST", f"/api/approvals/{foreign}/approve")

    with pytest.raises(ServiceValidationError) as err:
        await _call(
            hass,
            "reject",
            {
                "config_entry_id": entry_id,
                "approval_id": "99999999-9999-4999-8999-999999999990",
            },
        )
    assert err.value.translation_key == "not_found"

    # This company's approval that is not pending (revision requested) is
    # checked with the server, then decided.
    response = await _call(
        hass,
        "approve",
        {"config_entry_id": entry_id, "approval_id": revised.upper()},
    )
    assert response == {"approval_id": revised, "status": "approved"}
    assert fake.calls_to("GET", f"/api/approvals/{revised}")
