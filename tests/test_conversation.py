"""Tests for the per-agent conversation entities."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.paperclip.const import (
    CONF_DEFAULT_PROJECT,
    CONF_NOTIFY_LATE_REPLIES,
    CONF_REPLY_WAIT,
    EVENT_AGENT_REPLIED,
    MAX_ASSIST_REPLY_WAIT,
)
from custom_components.paperclip.conversation import plain_speech

from .conftest import COMPANY_ID, HOME_OPS_ID, PROJECT_ID, FakePaperclip

STILL_WORKING_NOTIFY = (
    "Home Ops is still working on it (ACME-43). I'll send the answer as a notification."
)
STILL_WORKING_PAPERCLIP = (
    "Home Ops is still working on it (ACME-43). The answer will be in Paperclip."
)
NOTIFY = "custom_components.paperclip.coordinator.persistent_notification.async_create"


@pytest.mark.parametrize(
    ("markdown", "speech"),
    [
        ("Done.", "Done."),
        ("All good", "All good."),
        (
            (
                "Yes: the backup ran (job `nightly`, state `ok`).\n\n"
                "- Checked read-only. Nothing was changed.\n"
                "- Side note: an **update** is available."
            ),
            (
                "Yes: the backup ran (job nightly, state ok). Checked read-only. "
                "Nothing was changed. Side note: an update is available."
            ),
        ),
        (
            "## Summary\n1. First step\n2) Second step\n> quoted",
            "Summary. First step. Second step. quoted.",
        ),
        (
            "See [the docs](https://example.com/docs) for *details*.",
            "See the docs for details.",
        ),
        ("Run:\n```bash\nreload all\n```", "Run: reload all."),
        ("| a | b |\n|---|---|", "| a | b |."),
        (
            "Keep sensor.living_room_temp and 2 * 3 = 6",
            "Keep sensor.living_room_temp and 2 * 3 = 6.",
        ),
        ("---", "---"),
        # A fence on one line keeps its words.
        ("```reload all```", "reload all."),
        ("Run ```core check``` now", "Run core check now."),
        # Nested list markers at any depth, and a list inside a quote.
        (
            "1. Kitchen\n    - lights off\n        - blinds down\n> - quoted item",
            "Kitchen. lights off. blinds down. quoted item.",
        ),
        # Lines that already end in punctuation get no extra full stop.
        # Full-width stop and exclamation mark (CJK).
        ("你好。\n世界！", "你好。 世界！"),  # noqa: RUF001
        ('He said "ok."\n(done)', 'He said "ok." (done)'),
    ],
)
def test_plain_speech(markdown: str, speech: str) -> None:
    """Agent markdown is read aloud as plain sentences."""
    assert plain_speech(markdown) == speech


AGENT = "conversation.home_ops"


async def _say(
    hass: HomeAssistant, text: str, conversation_id: str | None = None
) -> conversation.ConversationResult:
    return await conversation.async_converse(
        hass, text, conversation_id, Context(), language="en", agent_id=AGENT
    )


async def test_agent_is_listed(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Each Paperclip agent is an Assist conversation agent."""
    state = hass.states.get(AGENT)
    assert state is not None
    assert state.name == "Home Ops"
    agent = conversation.async_get_agent(hass, AGENT)
    assert agent is not None
    assert agent.supported_languages == "*"


async def test_first_message_creates_issue_and_reply_is_spoken(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """The first message creates an issue; a reply within the wait is spoken."""
    events = async_capture_events(hass, EVENT_AGENT_REPLIED)

    def reply(issue_id: str) -> None:
        if not fake.comments.get(issue_id):
            fake.agent_comment(
                issue_id, HOME_OPS_ID, "All lights are off. Anything else?"
            )

    fake.on_get_comments = reply
    result = await _say(hass, "Are all the downstairs lights off?")
    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE
    assert (
        result.response.speech["plain"]["speech"]
        == "All lights are off. Anything else?"
    )
    assert result.continue_conversation is True

    body = fake.calls_to("POST", f"/api/companies/{COMPANY_ID}/issues")[0][3]
    assert body["assigneeAgentId"] == HOME_OPS_ID
    assert body["title"] == "Are all the downstairs lights off?"
    assert body["description"].startswith("Are all the downstairs lights off?")
    assert len(events) == 1
    assert events[0].data["delivered"] is True

    # A follow-up in the same conversation becomes a comment on that issue.
    fake.on_get_comments = None
    follow_up = await _say(hass, "Turn on the porch light too.", result.conversation_id)
    issue_id = events[0].data["issue_id"]
    comment_calls = fake.calls_to("POST", f"/api/issues/{issue_id}/comments")
    assert comment_calls[0][3] == {"body": "Turn on the porch light too."}
    assert len(fake.calls_to("POST", f"/api/companies/{COMPANY_ID}/issues")) == 1
    assert follow_up.response.speech["plain"]["speech"] == STILL_WORKING_NOTIFY


async def test_reply_arrives_later_as_event_and_notification(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake: FakePaperclip,
) -> None:
    """A late reply comes as an event and, once, as the promised notification."""
    events = async_capture_events(hass, EVENT_AGENT_REPLIED)
    result = await _say(hass, "Order more coffee beans")
    speech = result.response.speech["plain"]["speech"]
    assert speech == STILL_WORKING_NOTIFY
    assert events == []

    issue = next(i for i in fake.issues.values() if i["identifier"] == "ACME-43")
    coordinator = init_integration.runtime_data.coordinator
    assert coordinator.tracker.issues[issue["id"]].notify_late_reply is True
    fake.agent_comment(issue["id"], HOME_OPS_ID, "Ordered two bags.")
    with patch(NOTIFY) as notify:
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        notify.assert_called_once_with(
            hass,
            "Ordered two bags.\n\n"
            "[ACME-43](http://paperclip.local:3100/ACME/issues/ACME-43)",
            title="Home Ops replied",
            notification_id=f"paperclip_{issue['id']}",
        )
        assert coordinator.tracker.issues[issue["id"]].notify_late_reply is False

        # Only the reply Assist promised becomes a notification.
        fake.agent_comment(issue["id"], HOME_OPS_ID, "They arrive on Friday.")
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert notify.call_count == 1
    assert len(events) == 2
    events = events[:1]
    assert events[0].data == {
        "config_entry_id": init_integration.entry_id,
        "company_id": COMPANY_ID,
        "issue_id": issue["id"],
        "identifier": "ACME-43",
        "title": "Order more coffee beans",
        "agent_id": HOME_OPS_ID,
        "agent_name": "Home Ops",
        "comment_id": events[0].data["comment_id"],
        "body": "Ordered two bags.",
        "url": "http://paperclip.local:3100/ACME/issues/ACME-43",
        "delivered": False,
    }


async def test_conversation_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """A Paperclip failure is spoken as an error."""
    fake.fail(
        "POST", f"/api/companies/{COMPANY_ID}/issues", 500, "Internal server error"
    )
    result = await _say(hass, "Water the plants")
    assert result.response.response_type is intent.IntentResponseType.ERROR
    assert "could not reach Paperclip" in result.response.speech["plain"]["speech"]


async def test_conversation_survives_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """The conversation-to-issue mapping is persisted across reloads."""
    result = await _say(hass, "Check the freezer temperature")
    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    tracker = init_integration.runtime_data.coordinator.tracker
    assert result.conversation_id in tracker.conversations
    await _say(hass, "And the fridge?", result.conversation_id)
    assert len(fake.calls_to("POST", f"/api/companies/{COMPANY_ID}/issues")) == 1


async def test_default_project_name_is_resolved(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> None:
    """A default project name becomes its id; an unknown one is left out."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={**mock_config_entry.options, CONF_DEFAULT_PROJECT: "Household"},
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await _say(hass, "Water the plants")
    body = fake.calls_to("POST", f"/api/companies/{COMPANY_ID}/issues")[0][3]
    assert body["projectId"] == PROJECT_ID

    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={**mock_config_entry.options, CONF_DEFAULT_PROJECT: "Garden"},
    )
    result = await _say(hass, "Mow the lawn")
    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE
    body = fake.calls_to("POST", f"/api/companies/{COMPANY_ID}/issues")[1][3]
    assert "projectId" not in body


async def test_late_reply_without_notifications(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """With notifications off, Assist points to Paperclip and nothing is posted."""
    hass.config_entries.async_update_entry(
        init_integration,
        options={**init_integration.options, CONF_NOTIFY_LATE_REPLIES: False},
    )
    await hass.async_block_till_done()
    result = await _say(hass, "Order more coffee beans")
    assert result.response.speech["plain"]["speech"] == STILL_WORKING_PAPERCLIP

    coordinator = init_integration.runtime_data.coordinator
    issue = next(i for i in fake.issues.values() if i["identifier"] == "ACME-43")
    assert coordinator.tracker.issues[issue["id"]].notify_late_reply is False
    fake.agent_comment(issue["id"], HOME_OPS_ID, "Ordered two bags.")
    with patch(NOTIFY) as notify:
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    notify.assert_not_called()


async def test_assist_wait_is_capped(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Assist never waits past the cap, whatever an old option says."""
    hass.config_entries.async_update_entry(
        init_integration, options={**init_integration.options, CONF_REPLY_WAIT: 300}
    )
    await hass.async_block_till_done()
    coordinator = init_integration.runtime_data.coordinator
    with patch.object(
        coordinator, "async_wait_for_reply", AsyncMock(return_value=None)
    ) as wait:
        await _say(hass, "Order more coffee beans")
    assert wait.call_args.args[1] == MAX_ASSIST_REPLY_WAIT


async def test_reply_in_time_is_not_notified(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """A reply spoken by Assist leaves no notification promise behind."""

    def reply(issue_id: str) -> None:
        if not fake.comments.get(issue_id):
            fake.agent_comment(issue_id, HOME_OPS_ID, "Done.")

    fake.on_get_comments = reply
    with patch(NOTIFY) as notify:
        result = await _say(hass, "Close the garage")
        assert result.response.speech["plain"]["speech"] == "Done."
        coordinator = init_integration.runtime_data.coordinator
        issue = next(i for i in fake.issues.values() if i["identifier"] == "ACME-43")
        assert coordinator.tracker.issues[issue["id"]].notify_late_reply is False
        fake.on_get_comments = None
        fake.agent_comment(issue["id"], HOME_OPS_ID, "Also locked the side door.")
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    notify.assert_not_called()


async def _start_waiting_turn(
    hass: HomeAssistant, entry: MockConfigEntry, fake: FakePaperclip
) -> tuple[asyncio.Task[conversation.ConversationResult], str]:
    """Start an Assist turn with a long wait; return it and its issue id."""
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_REPLY_WAIT: 60}
    )
    await hass.async_block_till_done()
    task = hass.async_create_task(_say(hass, "Order more coffee beans"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if any(i["identifier"] == "ACME-43" for i in fake.issues.values()):
            break
    issue = next(i for i in fake.issues.values() if i["identifier"] == "ACME-43")
    return task, issue["id"]


async def test_cancelled_turn_still_gets_its_notification(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """Closing the dialog mid-wait keeps the promise of a notification."""
    task, issue_id = await _start_waiting_turn(hass, init_integration, fake)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    coordinator = init_integration.runtime_data.coordinator
    assert coordinator.tracker.issues[issue_id].notify_late_reply is True

    fake.agent_comment(issue_id, HOME_OPS_ID, "Ordered two bags.")
    with patch(NOTIFY) as notify:
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert notify.call_count == 1


async def test_reload_during_wait_promises_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """A reload ends the wait without promising a notification it cannot keep."""
    task, issue_id = await _start_waiting_turn(hass, init_integration, fake)
    old_tracker = init_integration.runtime_data.coordinator.tracker
    await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    result = await task
    assert result.response.speech["plain"]["speech"] == STILL_WORKING_PAPERCLIP
    assert old_tracker.issues[issue_id].notify_late_reply is False
    new_tracker = init_integration.runtime_data.coordinator.tracker
    assert new_tracker.issues[issue_id].notify_late_reply is False
