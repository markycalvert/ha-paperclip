"""Tests for issue tracking, its storage and reply waiting edge cases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.paperclip.api import Comment, Issue
from custom_components.paperclip.const import (
    DOMAIN,
    EVENT_AGENT_REPLIED,
    EVENT_ISSUE_STATUS_CHANGED,
    MAX_TRACKED_ISSUES,
)
from custom_components.paperclip.coordinator import shorten
from custom_components.paperclip.tracker import IssueTracker

from .conftest import COMPANY_ID, ENGINEER_ID, RUNNING_ISSUE_ID, FakePaperclip


def _issue(issue_id: str, status: str = "todo", **kwargs: Any) -> Issue:
    stamp = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    return Issue(
        id=issue_id,
        identifier=kwargs.get("identifier"),
        title=kwargs.get("title", "Title"),
        status=status,
        priority=None,
        assignee_agent_id=kwargs.get("agent"),
        project_id=None,
        created_at=stamp,
        updated_at=stamp,
    )


def test_shorten() -> None:
    """Titles are cut on a word boundary with an ellipsis."""
    assert shorten("short", 60) == "short"
    assert shorten("word " * 30, 20) == "word word word word…"
    assert shorten("x" * 30, 10) == "xxxxxxxxx…"


async def test_tracker_storage_round_trip(hass: HomeAssistant) -> None:
    """Tracked issues and conversations survive storage; bad rows are dropped."""
    tracker = IssueTracker(hass, "entry")
    tracker.track(_issue("a", identifier="ACME-1"), agent_id="agent")
    tracker.link_conversation("conv", "a", "agent")
    await tracker.async_flush()

    with patch.object(
        tracker._store,
        "async_load",
        return_value={
            **tracker._data_to_save(),
            "issues": {
                **tracker._data_to_save()["issues"],
                "bad": {"id": None},
            },
            "conversations": {
                **tracker._data_to_save()["conversations"],
                "broken": {"issue_id": "a"},
            },
        },
    ):
        loaded = IssueTracker(hass, "entry")
        loaded._store = tracker._store
        await loaded.async_load()
    assert set(loaded.issues) == {"a"}
    assert loaded.find("acme-1") is loaded.issues["a"]
    assert loaded.find("missing") is None
    assert loaded.conversation_issue("conv", "agent") == "a"
    assert loaded.conversation_issue("conv", "other-agent") is None
    assert loaded.conversation_issue("broken", "agent") is None
    await loaded.async_remove()


async def test_tracker_prunes(hass: HomeAssistant) -> None:
    """Closed, old and excess issues are forgotten, with their conversations."""
    tracker = IssueTracker(hass, "entry")
    tracker.track(_issue("closed", "done"), agent_id=None)
    tracker.track(_issue("open"), agent_id=None)
    tracker.link_conversation("conv", "closed", "agent")
    tracker.set_last_comment("unknown", "c1", None)
    later = dt_util.utcnow() + timedelta(days=2)
    tracker.prune(later)
    assert set(tracker.issues) == {"open"}
    assert tracker.conversations == {}

    tracker.prune(dt_util.utcnow() + timedelta(days=31))
    assert tracker.issues == {}
    assert tracker.cursor is None

    for index in range(MAX_TRACKED_ISSUES + 3):
        tracker.track(_issue(f"i{index}"), agent_id=None)
    assert len(tracker.issues) == MAX_TRACKED_ISSUES

    tracker.link_conversation("old", "i199", "agent")
    tracker.conversations["old"].expires = dt_util.utcnow() - timedelta(seconds=1)
    assert tracker.conversation_issue("old", "agent") is None
    tracker.prune()
    assert "old" not in tracker.conversations


async def test_vanished_tracked_issue_is_forgotten(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """A tracked issue that now 404s is dropped instead of failing the poll."""
    coordinator = init_integration.runtime_data.coordinator
    await hass.services.async_call(
        DOMAIN,
        "add_comment",
        {
            "config_entry_id": init_integration.entry_id,
            "issue": "ACME-7",
            "message": "Hi",
        },
        blocking=True,
    )
    assert RUNNING_ISSUE_ID in coordinator.tracker.issues
    # The engineer's live run now resolves its title from the tracker.
    await coordinator.async_refresh()
    task = coordinator.data.current_tasks[ENGINEER_ID]
    assert task.identifier == "ACME-7"

    del fake.issues[RUNNING_ISSUE_ID]
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert RUNNING_ISSUE_ID not in coordinator.tracker.issues


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (500, "boom"),
        (429, "Too many concurrent requests"),
        (403, "Board access to this issue is not allowed"),
    ],
)
async def test_comment_poll_error_skips_one_issue(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake: FakePaperclip,
    status: int,
    error: str,
) -> None:
    """One unreadable issue is skipped and retried; the update still succeeds."""
    coordinator = init_integration.runtime_data.coordinator
    events = async_capture_events(hass, EVENT_AGENT_REPLIED)
    tracked, _ = await coordinator.async_comment("ACME-7", "Hi")
    other, _ = await coordinator.async_create_tracked_issue(
        title="Water the plants",
        description=None,
        agent_id=ENGINEER_ID,
        project_id=None,
        priority=None,
        allow_duplicate=True,
    )
    fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "Done with the first.")
    fake.agent_comment(other.id, ENGINEER_ID, "Watered.")
    fake.fail("GET", f"/api/issues/{RUNNING_ISSUE_ID}/comments", status, error)
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert [e.data["body"] for e in events] == ["Watered."]
    # A refused read on one issue is not a bad key.
    assert not hass.config_entries.flow.async_progress()

    fake.clear_failures()
    await coordinator.async_refresh()
    assert [e.data["body"] for e in events] == ["Watered.", "Done with the first."]
    assert tracked.id in coordinator.tracker.issues


async def test_comment_poll_fails_when_nothing_connects(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """Only when no tracked issue can be reached does the update fail."""
    coordinator = init_integration.runtime_data.coordinator
    await coordinator.async_comment("ACME-7", "Hi")
    fake.disconnect("GET", f"/api/issues/{RUNNING_ISSUE_ID}/comments")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success


async def test_wait_skips_older_agent_comments_and_survives_errors(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """Agent comments before ours fire undelivered events; poll errors are retried."""
    coordinator = init_integration.runtime_data.coordinator
    events = async_capture_events(hass, EVENT_AGENT_REPLIED)
    tracked, _ = await coordinator.async_comment("ACME-7", "First")
    fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "Answer to the first.")
    ours = fake._add_comment(RUNNING_ISSUE_ID, "Second")
    fake.fail("GET", f"/api/issues/{RUNNING_ISSUE_ID}/comments", 500, "boom")
    polls = 0

    def recover(_: str) -> None:
        nonlocal polls
        polls += 1

    original = fake._route

    def route(method: str, path: str, query: dict[str, str], body: Any) -> Any:
        if path.endswith("/comments") and method == "GET" and polls >= 1:
            fake.clear_failures()
        if path.endswith("/comments") and method == "GET":
            recover(path)
            if polls == 3:
                fake.agent_comment(
                    RUNNING_ISSUE_ID, ENGINEER_ID, "Answer to the second."
                )
                fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "And a postscript.")
        return original(method, path, query, body)

    fake._route = route  # type: ignore[method-assign]
    reply = await coordinator.async_wait_for_reply(
        tracked, 5, after=Comment.from_dict(ours)
    )
    assert reply is not None
    assert reply.body == "Answer to the second."
    delivered = [(e.data["body"], e.data["delivered"]) for e in events]
    assert delivered == [
        ("Answer to the first.", False),
        ("Answer to the second.", True),
        ("And a postscript.", False),
    ]


async def _track_three(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    coordinator = entry.runtime_data.coordinator
    ids = []
    for title in ("Water the plants", "Feed the cat", "Check the mail"):
        tracked, _ = await coordinator.async_create_tracked_issue(
            title=title,
            description=None,
            agent_id=ENGINEER_ID,
            project_id=None,
            priority=None,
            allow_duplicate=True,
        )
        ids.append(tracked.id)
    return ids


async def test_issue_updates_read_every_page(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """The cursor only moves after the activity-ordered list is read to the end."""
    coordinator = init_integration.runtime_data.coordinator
    statuses = async_capture_events(hass, EVENT_ISSUE_STATUS_CHANGED)
    first, second, third = await _track_three(hass, init_integration)
    cursor = coordinator.tracker.cursor
    # updatedAt order is third < second < first, but by activity third sorts
    # last: a single page would hold only first and second, and a cursor at
    # first's updatedAt would skip third for good.
    for issue_id, activity in ((third, "9"), (second, "2"), (first, "1")):
        fake.set_issue_status(issue_id, "in_review")
        fake.issues[issue_id]["lastActivityAt"] = activity
    with patch("custom_components.paperclip.coordinator.ISSUE_PAGE_SIZE", 2):
        await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert {event.data["issue_id"] for event in statuses} == {first, second, third}
    assert coordinator.tracker.cursor == max(
        datetime.fromisoformat(fake.issues[key]["updatedAt"])
        for key in (first, second, third)
    )
    assert coordinator.tracker.cursor > cursor
    pages = fake.calls_to("GET", f"/api/companies/{COMPANY_ID}/issues")
    assert [call[2].get("offset") for call in pages] == [None, "2"]
    assert all(call[2]["sortDir"] == "asc" for call in pages)
    assert all(call[2]["sortField"] == "updated" for call in pages)
    assert all(call[2]["limit"] == "2" for call in pages)


async def test_issue_updates_shifting_between_pages(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """A row that slips onto an already-read page is caught by the re-read."""
    coordinator = init_integration.runtime_data.coordinator
    statuses = async_capture_events(hass, EVENT_ISSUE_STATUS_CHANGED)
    first, second, third = await _track_three(hass, init_integration)
    cursor = coordinator.tracker.cursor
    # By activity: first, second | third. updatedAt: third < second < first.
    for issue_id, activity in ((third, "3"), (second, "2"), (first, "1")):
        fake.set_issue_status(issue_id, "in_review")
        fake.issues[issue_id]["lastActivityAt"] = activity
    list_path = f"/api/companies/{COMPANY_ID}/issues"
    original = fake._route

    def route(method: str, path: str, query: dict[str, str], body: Any) -> Any:
        result = original(method, path, query, body)
        if method == "GET" and path == list_path and "offset" not in query:
            # New activity on first while the pages are read: it moves to the
            # end, and third slips onto page one, which was already read.
            fake.issues[first]["lastActivityAt"] = "9"
        return result

    fake._route = route  # type: ignore[method-assign]
    with patch("custom_components.paperclip.coordinator.ISSUE_PAGE_SIZE", 2):
        await coordinator.async_refresh()
    pages = fake.calls_to("GET", list_path)
    assert [call[2].get("offset") for call in pages] == [None, "2"]
    assert {event.data["issue_id"] for event in statuses} == {first, second, third}
    assert coordinator.tracker.cursor is not None
    assert cursor is not None
    assert coordinator.tracker.cursor > cursor


async def test_issue_updates_beyond_the_page_limit(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """When the pages run out, the tracked issues are re-read one by one."""
    coordinator = init_integration.runtime_data.coordinator
    statuses = async_capture_events(hass, EVENT_ISSUE_STATUS_CHANGED)
    issue_ids = await _track_three(hass, init_integration)
    for index, issue_id in enumerate(issue_ids):
        fake.set_issue_status(issue_id, "blocked")
        fake.issues[issue_id]["lastActivityAt"] = str(9 - index)
    cursor = coordinator.tracker.cursor
    with (
        patch("custom_components.paperclip.coordinator.ISSUE_PAGE_SIZE", 1),
        patch("custom_components.paperclip.coordinator.MAX_ISSUE_PAGES", 1),
    ):
        fake.fail("GET", f"/api/issues/{issue_ids[2]}", 500, "boom")
        await coordinator.async_refresh()
        # One tracked issue could not be re-read: the cursor must stay put.
        assert coordinator.tracker.cursor == cursor
        fake.clear_failures()
        await coordinator.async_refresh()
    assert sorted(event.data["issue_id"] for event in statuses) == sorted(issue_ids)
    assert coordinator.tracker.cursor > cursor


async def test_comment_reads_ask_for_oldest_first(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """Paperclip lists newest first by default, so order=asc must be sent."""
    coordinator = init_integration.runtime_data.coordinator
    events = async_capture_events(hass, EVENT_AGENT_REPLIED)
    tracked, comment = await coordinator.async_comment("ACME-7", "Status?")
    fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "First reply.")
    fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "Second reply.")
    await coordinator.async_refresh()
    reads = fake.calls_to("GET", f"/api/issues/{RUNNING_ISSUE_ID}/comments")
    assert reads[-1][2] == {"order": "asc", "limit": "100", "after": comment.id}
    assert [event.data["body"] for event in events] == [
        "First reply.",
        "Second reply.",
    ]
    assert (
        coordinator.tracker.issues[tracked.id].last_comment_id
        == (fake.comments[RUNNING_ISSUE_ID][-1]["id"])
    )


async def test_reply_under_sentinel_user_is_an_agent_reply(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """A reply stored under local-board but attributed to the agent counts."""
    coordinator = init_integration.runtime_data.coordinator
    events = async_capture_events(hass, EVENT_AGENT_REPLIED)
    tracked, comment = await coordinator.async_comment("ACME-7", "Status?")

    def reply(_: str) -> None:
        if len(fake.comments[RUNNING_ISSUE_ID]) == 1:
            fake.sentinel_agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "On it.")

    fake.on_get_comments = reply
    answer = await coordinator.async_wait_for_reply(tracked, 5, after=comment)
    assert answer is not None
    assert answer.body == "On it."
    assert answer.author_agent_id == ENGINEER_ID
    assert events[0].data["agent_name"] == "Engineer"


async def test_wait_finds_reply_after_a_poll_passed_our_comment(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """A poll that read past our comment before the wait began does not hide it."""
    coordinator = init_integration.runtime_data.coordinator
    events = async_capture_events(hass, EVENT_AGENT_REPLIED)
    tracked, _ = await coordinator.async_comment("ACME-7", "First")
    ours = Comment.from_dict(fake._add_comment(RUNNING_ISSUE_ID, "Second"))
    quick = fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "Quick answer.")
    # A scheduled poll was already reading the issue when our comment landed.
    await coordinator.async_refresh()
    assert coordinator.tracker.issues[tracked.id].last_comment_id == quick["id"]

    def reply(_: str) -> None:
        if len(fake.comments[RUNNING_ISSUE_ID]) == 3:
            fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "Full answer.")

    fake.on_get_comments = reply
    answer = await coordinator.async_wait_for_reply(tracked, 5, after=ours)
    assert answer is not None
    assert answer.body == "Full answer."
    assert [(e.data["body"], e.data["delivered"]) for e in events] == [
        ("Quick answer.", False),
        ("Full answer.", True),
    ]


async def test_deleted_cursor_comment_does_not_stop_replies(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """Paperclip lists nothing after a deleted comment; replies still arrive."""
    coordinator = init_integration.runtime_data.coordinator
    events = async_capture_events(hass, EVENT_AGENT_REPLIED)
    earlier = fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "Earlier note.")
    tracked, comment = await coordinator.async_comment("ACME-7", "Status?")
    # Our comment was queued and then discarded: Paperclip hard-deletes it.
    fake.comments[RUNNING_ISSUE_ID] = [
        item for item in fake.comments[RUNNING_ISSUE_ID] if item["id"] != comment.id
    ]
    await coordinator.async_refresh()
    assert events == []
    # The cursor moved to the newest comment that still exists.
    assert coordinator.tracker.issues[tracked.id].last_comment_id == earlier["id"]

    path = f"/api/issues/{RUNNING_ISSUE_ID}/comments"
    reads = len(fake.calls_to("GET", path))
    await coordinator.async_refresh()
    # A quiet issue costs one request.
    assert len(fake.calls_to("GET", path)) == reads + 1

    fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "Here is the status.")
    await coordinator.async_refresh()
    assert [event.data["body"] for event in events] == ["Here is the status."]

    # The anchor disappears again, with a new reply behind it.
    fake.comments[RUNNING_ISSUE_ID].pop()
    fake.agent_comment(RUNNING_ISSUE_ID, ENGINEER_ID, "One more thing.")
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert [event.data["body"] for event in events] == [
        "Here is the status.",
        "One more thing.",
    ]


async def test_late_committed_status_change_is_seen(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake: FakePaperclip
) -> None:
    """An update stamped before the cursor but committed later is still read."""
    coordinator = init_integration.runtime_data.coordinator
    statuses = async_capture_events(hass, EVENT_ISSUE_STATUS_CHANGED)
    first, second, _ = await _track_three(hass, init_integration)
    fake.set_issue_status(second, "in_review")
    await coordinator.async_refresh()
    cursor = coordinator.tracker.cursor
    assert cursor is not None
    fake.issues[first]["status"] = "done"
    fake.issues[first]["updatedAt"] = (cursor - timedelta(seconds=5)).isoformat()
    await coordinator.async_refresh()
    assert [(e.data["issue_id"], e.data["new_status"]) for e in statuses] == [
        (second, "in_review"),
        (first, "done"),
    ]
    assert coordinator.tracker.cursor == cursor
    since = fake.calls_to("GET", f"/api/companies/{COMPANY_ID}/issues")[-1][2]
    assert datetime.fromisoformat(since["updatedSince"]) == cursor - timedelta(
        seconds=60
    )


async def test_tracker_stores_comment_time_and_stops_after_close(
    hass: HomeAssistant,
) -> None:
    """The comment cursor's time is stored; a closed tracker saves nothing more."""
    stamp = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)
    tracker = IssueTracker(hass, "entry")
    tracker.track(
        _issue("a"), agent_id=None, last_comment_id="c1", last_comment_at=stamp
    )
    tracker.set_notify_late_reply("a", notify=True)
    tracker.set_notify_late_reply("missing", notify=True)
    await tracker.async_close()
    loaded = IssueTracker(hass, "entry")
    await loaded.async_load()
    assert loaded.issues["a"].last_comment_id == "c1"
    assert loaded.issues["a"].last_comment_at == stamp
    assert loaded.issues["a"].notify_late_reply is True

    with patch.object(tracker._store, "async_delay_save") as delay_save:
        tracker.set_last_comment("a", "c2", stamp)
    delay_save.assert_not_called()
    await loaded.async_remove()


async def test_change_during_close_schedules_no_write(hass: HomeAssistant) -> None:
    """A change made while the final flush awaits must not write later."""
    tracker = IssueTracker(hass, "entry")
    tracker.track(_issue("a"), agent_id=None)
    real_save = tracker._store.async_save

    async def save_while_changing(data: Any) -> None:
        tracker.set_notify_late_reply("a", notify=True)
        await real_save(data)

    with (
        patch.object(tracker._store, "async_save", side_effect=save_while_changing),
        patch.object(tracker._store, "async_delay_save") as delay_save,
    ):
        await tracker.async_close()
    delay_save.assert_not_called()
