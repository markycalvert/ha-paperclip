"""Tests for the per-agent conversation entities."""

from __future__ import annotations

import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.paperclip.const import CONF_DEFAULT_PROJECT, EVENT_AGENT_REPLIED
from custom_components.paperclip.conversation import plain_speech

from .conftest import COMPANY_ID, HOME_OPS_ID, PROJECT_ID, FakePaperclip


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
    assert follow_up.response.speech["plain"]["speech"] == (
        "Sent to Home Ops as ACME-43. The reply will be in Paperclip."
    )


async def test_reply_arrives_later_as_event(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake: FakePaperclip,
) -> None:
    """Without a reply in time, Assist says so and the reply comes as an event."""
    events = async_capture_events(hass, EVENT_AGENT_REPLIED)
    result = await _say(hass, "Order more coffee beans")
    speech = result.response.speech["plain"]["speech"]
    assert speech == "Sent to Home Ops as ACME-43. The reply will be in Paperclip."
    assert events == []

    issue = next(i for i in fake.issues.values() if i["identifier"] == "ACME-43")
    fake.agent_comment(issue["id"], HOME_OPS_ID, "Ordered two bags.")
    await init_integration.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(events) == 1
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
