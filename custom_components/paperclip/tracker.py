"""Persisted record of the issues and conversations this integration started."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import Issue, parse_datetime
from .const import (
    CLOSED_ISSUE_STATUSES,
    CONVERSATION_EXPIRY,
    DOMAIN,
    MAX_TRACKED_ISSUES,
    POLL_COMMENTS_AFTER_CLOSE,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
    TRACK_AT_MOST,
    TRACK_CLOSED_FOR,
)

if TYPE_CHECKING:
    from datetime import datetime


class _StoredData(TypedDict, total=False):
    issues: dict[str, dict[str, Any]]
    conversations: dict[str, dict[str, Any]]
    cursor: str | None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass(slots=True)
class TrackedIssue:
    """An issue whose status and agent replies the integration follows."""

    id: str
    identifier: str | None
    title: str
    agent_id: str | None
    status: str
    tracked_at: datetime
    last_comment_id: str | None = None
    closed_at: datetime | None = None
    last_comment_at: datetime | None = None

    @property
    def is_closed(self) -> bool:
        """Return True when the issue is done or cancelled."""
        return self.status in CLOSED_ISSUE_STATUSES

    def as_dict(self) -> dict[str, Any]:
        """Serialise for storage."""
        return {
            "id": self.id,
            "identifier": self.identifier,
            "title": self.title,
            "agent_id": self.agent_id,
            "status": self.status,
            "tracked_at": _iso(self.tracked_at),
            "last_comment_id": self.last_comment_id,
            "closed_at": _iso(self.closed_at),
            "last_comment_at": _iso(self.last_comment_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackedIssue | None:
        """Restore from storage; return None for a corrupt row."""
        issue_id = data.get("id")
        tracked_at = parse_datetime(data.get("tracked_at"))
        if not isinstance(issue_id, str) or tracked_at is None:
            return None
        return cls(
            id=issue_id,
            identifier=data.get("identifier"),
            title=str(data.get("title") or ""),
            agent_id=data.get("agent_id"),
            status=str(data.get("status") or "todo"),
            tracked_at=tracked_at,
            last_comment_id=data.get("last_comment_id"),
            closed_at=parse_datetime(data.get("closed_at")),
            last_comment_at=parse_datetime(data.get("last_comment_at")),
        )


@dataclass(slots=True)
class ConversationLink:
    """Maps an Assist conversation to the issue it created."""

    issue_id: str
    agent_id: str
    expires: datetime


class IssueTracker:
    """Store-backed tracker for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Create the tracker; call async_load before use."""
        self._store: Store[_StoredData] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )
        self.issues: dict[str, TrackedIssue] = {}
        self.conversations: dict[str, ConversationLink] = {}
        self.cursor: datetime | None = None
        self._closed = False

    async def async_load(self) -> None:
        """Load the persisted state."""
        data = await self._store.async_load() or {}
        for raw in (data.get("issues") or {}).values():
            if tracked := TrackedIssue.from_dict(raw):
                self.issues[tracked.id] = tracked
        for conversation_id, raw in (data.get("conversations") or {}).items():
            expires = parse_datetime(raw.get("expires"))
            issue_id = raw.get("issue_id")
            agent_id = raw.get("agent_id")
            if expires and isinstance(issue_id, str) and isinstance(agent_id, str):
                self.conversations[conversation_id] = ConversationLink(
                    issue_id, agent_id, expires
                )
        self.cursor = parse_datetime(data.get("cursor"))

    async def async_flush(self) -> None:
        """Write pending changes now."""
        await self._store.async_save(self._data_to_save())

    async def async_close(self) -> None:
        """
        Write pending changes and stop saving (the entry is unloading).

        A reply wait or poll still running on the old coordinator must not
        overwrite what the reloaded entry saves, or recreate the file of a
        removed entry.
        """
        await self.async_flush()
        self._closed = True

    async def async_remove(self) -> None:
        """Delete the persisted state (config entry removed)."""
        await self._store.async_remove()

    def _data_to_save(self) -> _StoredData:
        return {
            "issues": {key: value.as_dict() for key, value in self.issues.items()},
            "conversations": {
                key: {
                    "issue_id": link.issue_id,
                    "agent_id": link.agent_id,
                    "expires": link.expires.isoformat(),
                }
                for key, link in self.conversations.items()
            },
            "cursor": _iso(self.cursor),
        }

    @callback
    def async_schedule_save(self) -> None:
        """Save soon, coalescing bursts of changes."""
        if not self._closed:
            self._store.async_delay_save(self._data_to_save, STORAGE_SAVE_DELAY)

    @callback
    def track(
        self,
        issue: Issue,
        *,
        agent_id: str | None,
        last_comment_id: str | None = None,
        last_comment_at: datetime | None = None,
    ) -> TrackedIssue:
        """Start (or refresh) tracking an issue."""
        now = dt_util.utcnow()
        closed_at = now if issue.status in CLOSED_ISSUE_STATUSES else None
        if (tracked := self.issues.get(issue.id)) is not None:
            # Update in place so callers holding the object see the change.
            tracked.identifier = issue.identifier
            tracked.title = issue.title
            tracked.agent_id = agent_id or issue.assignee_agent_id or tracked.agent_id
            tracked.status = issue.status
            tracked.closed_at = closed_at
            if last_comment_id:
                tracked.last_comment_id = last_comment_id
                tracked.last_comment_at = last_comment_at
        else:
            tracked = TrackedIssue(
                id=issue.id,
                identifier=issue.identifier,
                title=issue.title,
                agent_id=agent_id or issue.assignee_agent_id,
                status=issue.status,
                tracked_at=now,
                last_comment_id=last_comment_id,
                closed_at=closed_at,
                last_comment_at=last_comment_at,
            )
            self.issues[issue.id] = tracked
        # Issue updates are read with a server-side updatedSince cursor. Start
        # it at the state we already know, so only later changes are listed.
        # An existing cursor is never moved back for an older issue: its state
        # was just read, and any later change gets a newer updatedAt anyway.
        seen_at = issue.updated_at or issue.created_at
        if seen_at and self.cursor is None:
            self.cursor = seen_at
        self.prune(now)
        self.async_schedule_save()
        return tracked

    @callback
    def find(self, issue_ref: str) -> TrackedIssue | None:
        """Find a tracked issue by UUID or identifier."""
        if tracked := self.issues.get(issue_ref):
            return tracked
        ref = issue_ref.casefold()
        return next(
            (
                item
                for item in self.issues.values()
                if item.identifier and item.identifier.casefold() == ref
            ),
            None,
        )

    @callback
    def set_status(self, issue_id: str, status: str) -> None:
        """Record a new status."""
        tracked = self.issues[issue_id]
        tracked.status = status
        tracked.closed_at = dt_util.utcnow() if tracked.is_closed else None
        self.async_schedule_save()

    @callback
    def set_last_comment(
        self, issue_id: str, comment_id: str, created_at: datetime | None
    ) -> None:
        """Record the newest comment seen on an issue, and when it was posted."""
        if tracked := self.issues.get(issue_id):
            tracked.last_comment_id = comment_id
            tracked.last_comment_at = created_at
            self.async_schedule_save()

    @callback
    def set_cursor(self, cursor: datetime) -> None:
        """Advance the updatedSince cursor."""
        if self.cursor is None or cursor > self.cursor:
            self.cursor = cursor
            self.async_schedule_save()

    @callback
    def comment_poll_candidates(self) -> list[TrackedIssue]:
        """Return issues worth polling for new comments."""
        now = dt_util.utcnow()
        return [
            item
            for item in self.issues.values()
            if not item.is_closed
            or (item.closed_at and now - item.closed_at < POLL_COMMENTS_AFTER_CLOSE)
        ]

    @callback
    def link_conversation(
        self, conversation_id: str, issue_id: str, agent_id: str
    ) -> None:
        """Remember which issue an Assist conversation belongs to."""
        self.conversations[conversation_id] = ConversationLink(
            issue_id, agent_id, dt_util.utcnow() + CONVERSATION_EXPIRY
        )
        self.async_schedule_save()

    @callback
    def conversation_issue(self, conversation_id: str, agent_id: str) -> str | None:
        """Return the issue of a live conversation with this agent."""
        link = self.conversations.get(conversation_id)
        if link is None or link.agent_id != agent_id:
            return None
        if link.expires <= dt_util.utcnow() or link.issue_id not in self.issues:
            return None
        link.expires = dt_util.utcnow() + CONVERSATION_EXPIRY
        self.async_schedule_save()
        return link.issue_id

    @callback
    def prune(self, now: datetime | None = None) -> None:
        """Forget old issues and expired conversations."""
        now = now or dt_util.utcnow()
        for issue_id, item in list(self.issues.items()):
            closed_long_ago = item.closed_at and now - item.closed_at > TRACK_CLOSED_FOR
            if closed_long_ago or now - item.tracked_at > TRACK_AT_MOST:
                del self.issues[issue_id]
        if len(self.issues) > MAX_TRACKED_ISSUES:
            oldest = sorted(self.issues.values(), key=lambda item: item.tracked_at)
            for item in oldest[: len(self.issues) - MAX_TRACKED_ISSUES]:
                del self.issues[item.id]
        for conversation_id, link in list(self.conversations.items()):
            if link.expires <= now or link.issue_id not in self.issues:
                del self.conversations[conversation_id]
        if not self.issues:
            self.cursor = None
