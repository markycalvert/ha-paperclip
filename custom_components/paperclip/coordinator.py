"""Data update coordinator for the Paperclip integration."""

from __future__ import annotations

import asyncio
import re
from collections import Counter, OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, override
from urllib.parse import quote

from awesomeversion import AwesomeVersion, AwesomeVersionException
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    Agent,
    Approval,
    Comment,
    Company,
    Dashboard,
    Health,
    Issue,
    LiveRun,
    PaperclipAuthError,
    PaperclipClient,
    PaperclipConnectionError,
    PaperclipError,
    PaperclipHostnameNotAllowedError,
    PaperclipNotFoundError,
    PaperclipRateLimitError,
)
from .const import (
    COMMENT_FETCH_CONCURRENCY,
    CONF_COMPANY_ID,
    CONF_DEFAULT_PROJECT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_AGENT_REPLIED,
    EVENT_AGENT_STATUS_CHANGED,
    EVENT_APPROVAL_REQUESTED,
    EVENT_ISSUE_STATUS_CHANGED,
    EVENT_TITLE_MAX_LENGTH,
    ISSUE_CACHE_SIZE,
    ISSUE_PAGE_SIZE,
    ISSUE_UPDATE_OVERLAP,
    LOGGER,
    MAX_ISSUE_PAGES,
    MIN_PAPERCLIP_VERSION,
    NO_PROJECT,
    PLACEHOLDER_PAPERCLIP_VERSIONS,
    REPLY_MAX_LENGTH,
    REPLY_POLL_INTERVAL,
    UNSUPPORTED_ROUTE_RETRY,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .data import PaperclipConfigEntry
    from .tracker import IssueTracker, TrackedIssue

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
MAX_TASK_LOOKUPS_PER_POLL = 10


def shorten(text: str, limit: int) -> str:
    """Trim text to ``limit`` characters on a word boundary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    # Drop a partial last word, unless the cut already falls between words.
    if text[limit - 1] != " " and " " in cut[limit // 2 :]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:") + "…"


def truncate(text: str, limit: int) -> str:
    """Hard-truncate long text such as comment bodies."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _reached(cursor_id: str | None, cursor_at: datetime | None, ours: Comment) -> bool:
    """Return True if a comment cursor is at or past our own comment."""
    if cursor_id is None:
        return False
    if cursor_id == ours.id:
        return True
    return (
        cursor_at is not None
        and ours.created_at is not None
        and cursor_at >= ours.created_at
    )


def _posted_after(comment: Comment, ours: Comment) -> bool:
    """Return True if a comment was posted after our own comment."""
    return (
        comment.created_at is not None
        and ours.created_at is not None
        and comment.created_at > ours.created_at
    )


def _split_at_ours(
    batch: CommentBatch, ours: Comment | None, *, seen_ours: bool
) -> tuple[list[Comment], list[Comment], bool]:
    """
    Split new comments into those before our own comment and those after it.

    Return both lists and whether our comment has now been passed.
    """
    if ours is None:
        return [], list(batch.comments), True
    if _reached(batch.cursor_id, batch.cursor_at, ours):
        seen_ours = True
    earlier: list[Comment] = []
    later: list[Comment] = []
    for comment in batch.comments:
        if not seen_ours:
            if comment.id == ours.id:
                seen_ours = True
                continue
            if not _posted_after(comment, ours):
                earlier.append(comment)
                continue
            seen_ours = True
        later.append(comment)
    return earlier, later, seen_ours


def comparable_version(version: str | None) -> AwesomeVersion | None:
    """
    Return a server version that can be compared, or None if it is unknown.

    A source checkout without release tags reports the package.json
    placeholder, which says nothing about its age. Build metadata after "+"
    (commits since the tag, the git sha) does not change the release.
    """
    if not version:
        return None
    base = version.strip().removeprefix("v").split("+", 1)[0]
    if not base or base in PLACEHOLDER_PAPERCLIP_VERSIONS:
        return None
    return AwesomeVersion(base)


@dataclass(frozen=True, slots=True)
class CurrentTask:
    """What an agent is working on right now, from the live-runs list."""

    run_id: str
    run_status: str
    issue_id: str | None
    identifier: str | None
    title: str | None


@dataclass(frozen=True, slots=True)
class CommentBatch:
    """New comments on one issue, and the cursor they were read after."""

    cursor_id: str | None
    cursor_at: datetime | None
    comments: list[Comment]


@dataclass(slots=True)
class PaperclipState:
    """One poll's worth of data."""

    health: Health
    company: Company
    agents: dict[str, Agent]
    dashboard: Dashboard | None
    approvals: list[Approval] | None
    live_runs: list[LiveRun] | None
    current_tasks: dict[str, CurrentTask] = field(default_factory=dict)


class PaperclipCoordinator(DataUpdateCoordinator[PaperclipState]):
    """Polls one Paperclip company and turns changes into bus events."""

    config_entry: PaperclipConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PaperclipConfigEntry,
        client: PaperclipClient,
        tracker: IssueTracker,
    ) -> None:
        """Create the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.tracker = tracker
        self.company_id: str = entry.data[CONF_COMPANY_ID]
        self.issue_prefix: str = ""
        # Optional routes the server answered 404 to, and when to try again.
        self.unsupported: dict[str, datetime] = {}
        self._issue_cache: OrderedDict[str, tuple[str | None, str]] = OrderedDict()
        self._waiting: Counter[str] = Counter()
        self._locks: dict[str, asyncio.Lock] = {}
        self._known_approvals: set[str] | None = None
        self._checked_version: str | None = None
        self._projects: dict[str, str] | None = None
        # Set when the entry unloads: reply waits still running in an action
        # or Assist turn stop, and stop touching this entry's tracker.
        self._stopping = asyncio.Event()

    # URLs ------------------------------------------------------------------

    def _ui_url(self, path: str) -> str:
        prefix = f"/{self.issue_prefix}" if self.issue_prefix else ""
        return f"{self.client.base_url}{prefix}{path}"

    def issue_url(self, issue_ref: str) -> str:
        """Return the Paperclip UI URL of an issue."""
        return self._ui_url(f"/issues/{issue_ref}")

    def approval_url(self, approval_id: str) -> str:
        """Return the Paperclip UI URL of an approval."""
        return self._ui_url(f"/approvals/{quote(approval_id, safe='')}")

    def agent_url(self, agent: Agent) -> str:
        """Return the Paperclip UI URL of an agent."""
        return self._ui_url(f"/agents/{quote(agent.url_key or agent.id, safe='')}")

    def dashboard_url(self) -> str:
        """Return the Paperclip UI URL of the company dashboard."""
        return self._ui_url("/dashboard")

    # Polling ---------------------------------------------------------------

    @override
    async def _async_setup(self) -> None:
        await self.tracker.async_load()

    @callback
    def async_stop_waits(self) -> None:
        """End every reply wait of this coordinator (the entry is unloading)."""
        self._stopping.set()

    @override
    async def async_shutdown(self) -> None:
        self.async_stop_waits()
        await super().async_shutdown()

    @override
    async def _async_update_data(self) -> PaperclipState:
        try:
            return await self._async_fetch()
        except PaperclipAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except PaperclipHostnameNotAllowedError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="hostname_not_allowed"
            ) from err
        except PaperclipRateLimitError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="rate_limited",
                retry_after=err.retry_after,
            ) from err
        except PaperclipNotFoundError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="company_not_found"
            ) from err
        except PaperclipError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err

    async def _optional[T](
        self, name: str, fetch: Callable[[], Awaitable[T]]
    ) -> T | None:
        """
        Call a route that older or newer servers may not have.

        Paperclip's own 404 skips the route for UNSUPPORTED_ROUTE_RETRY, so a
        server upgrade is picked up without a reload. A 404 without
        Paperclip's error body (a reverse proxy while the server restarts)
        only skips this poll.
        """
        retry_at = self.unsupported.get(name)
        if retry_at is not None:
            if dt_util.utcnow() < retry_at:
                return None
            del self.unsupported[name]
        try:
            return await fetch()
        except PaperclipNotFoundError as err:
            if not err.from_paperclip:
                LOGGER.debug(
                    "Paperclip route for %s answered a 404 that is not Paperclip's "
                    "(%s); trying it again next poll",
                    name,
                    err,
                )
                return None
            LOGGER.debug(
                "Paperclip route for %s is not available; skipping it for %s",
                name,
                UNSUPPORTED_ROUTE_RETRY,
            )
            self.unsupported[name] = dt_util.utcnow() + UNSUPPORTED_ROUTE_RETRY
            return None

    async def _async_fetch(self) -> PaperclipState:
        client = self.client
        cid = self.company_id
        health = await client.get_health()
        self._check_server_version(health.version)
        company = await client.get_company(cid)
        self.issue_prefix = company.issue_prefix
        agents = {agent.id: agent for agent in await client.list_agents(cid)}
        dashboard = await self._optional("dashboard", lambda: client.get_dashboard(cid))
        approvals = await self._optional(
            "approvals", lambda: client.list_pending_approvals(cid)
        )
        live_runs = await self._optional(
            "live_runs", lambda: client.list_live_runs(cid)
        )
        current_tasks = await self._async_current_tasks(live_runs or [])
        if self.hass.state is CoreState.running:
            await self._async_update_tracked_issues(agents)
        # Otherwise Home Assistant is still starting and automations are not
        # listening yet: replies and status changes that came in while it was
        # down would fire to nobody, and their cursors would move past them.
        # They are read once Home Assistant has started (async_setup_entry).

        state = PaperclipState(
            health=health,
            company=company,
            agents=agents,
            dashboard=dashboard,
            approvals=approvals,
            live_runs=live_runs,
            current_tasks=current_tasks,
        )
        self._fire_agent_events(state)
        self._fire_approval_events(state)
        return state

    def _check_server_version(self, version: str | None) -> None:
        """Raise or clear the "Paperclip too old" repair issue."""
        if version == self._checked_version:
            return
        self._checked_version = version
        issue_id = f"outdated_paperclip_{self.config_entry.entry_id}"
        outdated = False
        if (current := comparable_version(version)) is not None:
            with suppress(AwesomeVersionException):
                outdated = current < AwesomeVersion(MIN_PAPERCLIP_VERSION)
        if outdated:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="outdated_paperclip",
                translation_placeholders={
                    "version": str(version),
                    "minimum": MIN_PAPERCLIP_VERSION,
                    "title": self.config_entry.title,
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def _async_current_tasks(
        self, live_runs: list[LiveRun]
    ) -> dict[str, CurrentTask]:
        """Pick one live run per agent, preferring running over queued."""
        chosen: dict[str, LiveRun] = {}
        for run in live_runs:
            current = chosen.get(run.agent_id)
            if current is None or (
                current.status != "running" and run.status == "running"
            ):
                chosen[run.agent_id] = run
        lookups = 0
        tasks: dict[str, CurrentTask] = {}
        for agent_id, run in chosen.items():
            identifier: str | None = None
            title: str | None = None
            if run.issue_id:
                if (tracked := self.tracker.issues.get(run.issue_id)) is not None:
                    identifier, title = tracked.identifier, tracked.title
                elif (cached := self._issue_cache.get(run.issue_id)) is not None:
                    identifier, title = cached
                elif lookups < MAX_TASK_LOOKUPS_PER_POLL:
                    lookups += 1
                    with suppress(PaperclipNotFoundError):
                        issue = await self.client.get_issue(run.issue_id)
                        identifier, title = issue.identifier, issue.title
                        self._cache_issue(run.issue_id, identifier, title)
            tasks[agent_id] = CurrentTask(
                run_id=run.id,
                run_status=run.status,
                issue_id=run.issue_id,
                identifier=identifier,
                title=title,
            )
        return tasks

    def _cache_issue(self, issue_id: str, identifier: str | None, title: str) -> None:
        self._issue_cache[issue_id] = (identifier, title)
        while len(self._issue_cache) > ISSUE_CACHE_SIZE:
            self._issue_cache.popitem(last=False)

    async def _async_update_tracked_issues(self, agents: dict[str, Agent]) -> None:
        """Follow status changes and agent replies on tracked issues."""
        tracker = self.tracker
        tracker.prune()
        if not tracker.issues:
            return
        if tracker.cursor is not None:
            await self._async_follow_issue_statuses(tracker.cursor)
        await self._async_poll_tracked_comments(agents)

    async def _async_list_updated_issues(
        self, since: datetime
    ) -> tuple[list[Issue], bool] | None:
        """
        Read every page of issues updated after ``since``.

        Return the rows and whether they can be trusted to hold every issue
        changed since then, or None if the route could not be read. Only a
        single short page can: the list is ordered by activity and paged by
        offset, so an issue that gets new activity while the pages are read
        moves to the end and shifts the rest up, and a row can slip from one
        page to the one already read. Several pages, or more pages than
        MAX_ISSUE_PAGES, need the tracked issues re-read.
        """
        rows: list[Issue] = []
        for page in range(MAX_ISSUE_PAGES):
            batch = await self._optional(
                "issue_updates",
                partial(
                    self.client.list_issues_updated_since,
                    self.company_id,
                    since,
                    limit=ISSUE_PAGE_SIZE,
                    offset=page * ISSUE_PAGE_SIZE,
                ),
            )
            if batch is None:
                return None
            rows.extend(batch)
            if len(batch) < ISSUE_PAGE_SIZE:
                return rows, page == 0
        return rows, False

    async def _async_follow_issue_statuses(self, since: datetime) -> None:
        """Fire status events for tracked issues and advance the cursor."""
        # Read from a little before the cursor: an update stamped earlier than
        # the newest one listed can still commit after that list was read.
        listed = await self._async_list_updated_issues(since - ISSUE_UPDATE_OVERLAP)
        if listed is None:
            return
        rows, complete = listed
        for issue in rows:
            self._note_issue_status(issue)
        if not complete:
            # More issues changed than one page holds (a long outage, or a busy
            # company). The list is ordered by activity, not by updatedAt, and
            # rows can shift between pages, so the newest updatedAt seen does
            # not cover the rows left out. Read the tracked issues themselves;
            # only then may the cursor move.
            LOGGER.debug(
                "%s issues changed since %s, more than one page; "
                "re-reading tracked issues",
                len(rows),
                since,
            )
            refreshed = await self._async_refresh_tracked_issues()
            if refreshed is None:
                return
            rows = [*rows, *refreshed]
        stamps = [issue.updated_at for issue in rows if issue.updated_at]
        if stamps:
            self.tracker.set_cursor(max(stamps))

    async def _async_refresh_tracked_issues(self) -> list[Issue] | None:
        """Re-read every tracked issue; return None if any read failed."""
        semaphore = asyncio.Semaphore(COMMENT_FETCH_CONCURRENCY)

        async def fetch(issue_id: str) -> Issue | PaperclipError | None:
            async with semaphore:
                try:
                    issue = await self.client.get_issue(issue_id)
                except PaperclipNotFoundError:
                    self._forget_issue(issue_id)
                    return None
                except PaperclipError as err:
                    LOGGER.debug("Could not re-read issue %s: %s", issue_id, err)
                    return err
            self._note_issue_status(issue)
            return issue

        results = await asyncio.gather(
            *(fetch(issue_id) for issue_id in list(self.tracker.issues))
        )
        if any(isinstance(result, PaperclipError) for result in results):
            return None
        return [result for result in results if isinstance(result, Issue)]

    def _note_issue_status(self, issue: Issue) -> None:
        """Record a tracked issue's status and fire an event if it changed."""
        tracked = self.tracker.issues.get(issue.id)
        if tracked is None or issue.status == tracked.status:
            return
        old_status = tracked.status
        self.tracker.set_status(issue.id, issue.status)
        self.hass.bus.async_fire(
            EVENT_ISSUE_STATUS_CHANGED,
            {
                **self._issue_event_base(tracked),
                "old_status": old_status,
                "new_status": issue.status,
            },
        )

    def _forget_issue(self, issue_id: str) -> None:
        LOGGER.debug("Tracked issue %s is gone; forgetting it", issue_id)
        self.tracker.issues.pop(issue_id, None)
        self.tracker.async_schedule_save()

    async def _async_poll_tracked_comments(self, agents: dict[str, Agent]) -> None:
        """
        Fire reply events for new agent comments on tracked issues.

        An issue that cannot be read (a refused read, a busy server) is skipped
        and retried on the next poll, because its comment cursor did not move.
        The poll only fails when no issue can be reached at all.
        """
        semaphore = asyncio.Semaphore(COMMENT_FETCH_CONCURRENCY)

        async def poll(tracked: TrackedIssue) -> PaperclipError | None:
            async with semaphore:
                try:
                    batch = await self._async_fetch_new_comments(tracked)
                except PaperclipNotFoundError:
                    self._forget_issue(tracked.id)
                    return None
                except PaperclipError as err:
                    LOGGER.debug(
                        "Could not read new comments on %s: %s",
                        tracked.identifier or tracked.id,
                        err,
                    )
                    return err
            for comment in batch.comments:
                if comment.is_agent:
                    self._fire_reply(tracked, comment, agents, delivered=False)
            return None

        candidates = [
            item
            for item in self.tracker.comment_poll_candidates()
            if not self._waiting[item.id]
        ]
        if not candidates:
            return
        results = await asyncio.gather(*(poll(item) for item in candidates))
        failures = [err for err in results if err is not None]
        if len(failures) == len(candidates) and all(
            isinstance(err, PaperclipConnectionError) for err in failures
        ):
            raise failures[0]

    async def _async_fetch_new_comments(self, tracked: TrackedIssue) -> CommentBatch:
        """Return comments newer than the last one seen and advance the cursor."""
        lock = self._locks.setdefault(tracked.id, asyncio.Lock())
        async with lock:
            # Read the cursor from the tracker: the caller may hold a copy that
            # an earlier poll has already advanced.
            current = self.tracker.issues.get(tracked.id, tracked)
            cursor_id, cursor_at = current.last_comment_id, current.last_comment_at
            comments, newest = await self._async_list_after_cursor(
                tracked.id, cursor_id, cursor_at
            )
            if self._stopping.is_set():
                return CommentBatch(cursor_id, cursor_at, [])
            if last := (comments[-1] if comments else newest):
                self.tracker.set_last_comment(tracked.id, last.id, last.created_at)
            return CommentBatch(cursor_id, cursor_at, comments)

    async def _async_list_after_cursor(
        self, issue_id: str, cursor_id: str | None, cursor_at: datetime | None
    ) -> tuple[list[Comment], Comment | None]:
        """
        Return the comments after the cursor, and where to move a dead cursor.

        A quiet issue costs one request: its newest comment is the cursor.
        """
        client = self.client
        if cursor_id is None:
            return await client.list_comments(issue_id), None
        newest = await client.newest_comment(issue_id)
        if newest is None or newest.id == cursor_id:
            return [], None
        if comments := await client.list_comments(issue_id, after=cursor_id):
            return comments, None
        # Paperclip answers an empty list when the "after" comment no longer
        # exists (a discarded queued comment, a deleted agent's comments).
        # Find the newer comments by time instead, then move the cursor on.
        LOGGER.debug("Comment cursor of issue %s is gone; reading by time", issue_id)
        if cursor_at is None:
            return [], newest
        recent = await client.list_recent_comments(issue_id)
        return [
            comment
            for comment in recent
            if comment.created_at is not None and comment.created_at > cursor_at
        ], newest

    # Events ------------------------------------------------------------------

    def _event_base(self) -> dict[str, Any]:
        return {
            "config_entry_id": self.config_entry.entry_id,
            "company_id": self.company_id,
        }

    def _issue_event_base(self, tracked: TrackedIssue) -> dict[str, Any]:
        return {
            **self._event_base(),
            "issue_id": tracked.id,
            "identifier": tracked.identifier,
            "title": shorten(tracked.title, EVENT_TITLE_MAX_LENGTH),
            "agent_id": tracked.agent_id,
            "url": self.issue_url(tracked.identifier or tracked.id),
        }

    def _fire_reply(
        self,
        tracked: TrackedIssue,
        comment: Comment,
        agents: dict[str, Agent],
        *,
        delivered: bool,
    ) -> None:
        agent = agents.get(comment.author_agent_id or "")
        self.hass.bus.async_fire(
            EVENT_AGENT_REPLIED,
            {
                **self._issue_event_base(tracked),
                "agent_id": comment.author_agent_id,
                "agent_name": agent.name if agent else None,
                "comment_id": comment.id,
                "body": truncate(comment.body, REPLY_MAX_LENGTH),
                "delivered": delivered,
            },
        )

    def _fire_agent_events(self, state: PaperclipState) -> None:
        if self.data is None:
            return
        for agent_id, agent in state.agents.items():
            previous = self.data.agents.get(agent_id)
            if previous is None or previous.status == agent.status:
                continue
            self.hass.bus.async_fire(
                EVENT_AGENT_STATUS_CHANGED,
                {
                    **self._event_base(),
                    "agent_id": agent_id,
                    "agent_name": agent.name,
                    "old_status": previous.status,
                    "new_status": agent.status,
                },
            )

    def _fire_approval_events(self, state: PaperclipState) -> None:
        if state.approvals is None:
            return
        current = {approval.id for approval in state.approvals}
        known = self._known_approvals
        self._known_approvals = current
        if known is None:
            # First poll: take a baseline instead of replaying old approvals.
            return
        for approval in state.approvals:
            if approval.id in known:
                continue
            requester = state.agents.get(approval.requested_by_agent_id or "")
            self.hass.bus.async_fire(
                EVENT_APPROVAL_REQUESTED,
                {
                    **self._event_base(),
                    "approval_id": approval.id,
                    "type": approval.type,
                    "title": shorten(approval.title, EVENT_TITLE_MAX_LENGTH),
                    "requested_by_agent_id": approval.requested_by_agent_id,
                    "requested_by_agent_name": requester.name if requester else None,
                    "url": self.approval_url(approval.id),
                },
            )

    # Actions used by services and the conversation agent --------------------

    @callback
    def agent_name(self, agent_id: str) -> str:
        """Return an agent's display name, or its id if unknown."""
        if self.data and (agent := self.data.agents.get(agent_id)):
            return agent.name
        return agent_id

    @property
    def default_project(self) -> str | None:
        """Return the default project option (an id or a name), if one is set."""
        value = self.config_entry.options.get(CONF_DEFAULT_PROJECT)
        if not isinstance(value, str) or value.strip() in ("", NO_PROJECT):
            return None
        return value.strip()

    async def async_resolve_project(self, project_ref: str) -> str | None:
        """Turn a project id or name into a project id (None if unknown)."""
        ref = project_ref.strip()
        if UUID_RE.match(ref):
            return ref
        key = ref.casefold()
        if self._projects is not None and (project_id := self._projects.get(key)):
            return project_id
        # Not cached: projects can be added or renamed at any time, so read the
        # list again (once per lookup) before giving up.
        projects = await self.client.list_projects(self.company_id)
        self._projects = {item.name.casefold(): item.id for item in projects}
        return self._projects.get(key)

    async def async_create_tracked_issue(  # noqa: PLR0913
        self,
        *,
        title: str,
        description: str | None,
        agent_id: str | None,
        project_id: str | None,
        priority: str | None,
        allow_duplicate: bool,
    ) -> tuple[TrackedIssue, bool]:
        """
        Create an issue in Paperclip and start following it.

        Return the tracked issue and whether Paperclip handed back an existing
        open issue with the same title instead of creating one.
        """
        issue = await self.client.create_issue(
            self.company_id,
            title=title,
            description=description,
            assignee_agent_id=agent_id,
            project_id=project_id,
            priority=priority,
            allow_duplicate=allow_duplicate,
        )
        if not issue.deduplicated:
            return self.tracker.track(issue, agent_id=agent_id), False
        # Paperclip's title de-duplication ignores the assignee, so the open
        # issue it returned may belong to another agent, or to none.
        if agent_id and issue.assignee_agent_id != agent_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="duplicate_issue_other_assignee",
                translation_placeholders={
                    "identifier": issue.identifier or issue.id,
                    "agent": self.agent_name(agent_id),
                },
            )
        newest: Comment | None = None
        if issue.id not in self.tracker.issues:
            # Its earlier comments are not replies to this request, so follow
            # it from its newest one.
            newest = await self.client.newest_comment(issue.id)
        tracked = self.tracker.track(
            issue,
            agent_id=issue.assignee_agent_id,
            last_comment_id=newest.id if newest else None,
            last_comment_at=newest.created_at if newest else None,
        )
        return tracked, True

    async def async_comment(
        self, issue_ref: str, body: str
    ) -> tuple[TrackedIssue, Comment]:
        """Comment on an issue and follow it from now on."""
        tracked = self.tracker.find(issue_ref)
        if tracked is None:
            # A board key can open every company its user belongs to, so an
            # issue this entry does not follow yet must belong to its company
            # before it is commented on (which wakes the assignee) or tracked.
            found = await self.client.get_issue(issue_ref)
            if found.company_id != self.company_id:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="issue_wrong_company",
                    translation_placeholders={
                        "issue": issue_ref,
                        "title": self.config_entry.title,
                    },
                )
            issue_ref = found.id
        comment = await self.client.add_comment(
            tracked.id if tracked else issue_ref, body
        )
        if tracked is None:
            # Read it again: the comment may have reopened a closed issue.
            issue = await self.client.get_issue(issue_ref)
            tracked = self.tracker.track(
                issue,
                agent_id=issue.assignee_agent_id,
                last_comment_id=comment.id,
                last_comment_at=comment.created_at,
            )
        return tracked, comment

    async def async_wait_for_reply(
        self,
        tracked: TrackedIssue,
        wait_seconds: float,
        *,
        after: Comment | None = None,
    ) -> Comment | None:
        """
        Wait up to ``wait_seconds`` seconds for an agent comment on an issue.

        Only agent comments posted after our own comment ``after`` (or any
        agent comment when it is None) count as the reply. Every agent comment
        seen here still fires ``paperclip_agent_replied`` exactly once, with
        ``delivered`` true for the one returned to the caller. The wait ends
        early, without a reply, when the entry unloads.
        """
        if wait_seconds <= 0 or self._stopping.is_set():
            return None
        agents = self.data.agents if self.data else {}
        # Whether our comment was already passed is decided from the cursor
        # read under the issue's lock: a poll that was already reading the
        # issue may move the cursor to (or past) our comment at any moment.
        seen_ours = after is None
        self._waiting[tracked.id] += 1
        try:
            async with asyncio.timeout(wait_seconds):
                while not self._stopping.is_set():
                    try:
                        batch = await self._async_fetch_new_comments(tracked)
                    except PaperclipError as err:
                        LOGGER.debug(
                            "Polling %s for a reply failed: %s", tracked.id, err
                        )
                        batch = CommentBatch(None, None, [])
                    earlier, later, seen_ours = _split_at_ours(
                        batch, after, seen_ours=seen_ours
                    )
                    for comment in earlier:
                        if comment.is_agent:
                            self._fire_reply(tracked, comment, agents, delivered=False)
                    reply: Comment | None = None
                    for comment in later:
                        if comment.is_agent:
                            first = reply is None
                            reply = reply or comment
                            self._fire_reply(tracked, comment, agents, delivered=first)
                    if reply is not None:
                        return reply
                    with suppress(TimeoutError):
                        async with asyncio.timeout(REPLY_POLL_INTERVAL):
                            await self._stopping.wait()
        except TimeoutError:
            return None
        finally:
            self._waiting[tracked.id] -= 1
            if not self._waiting[tracked.id]:
                del self._waiting[tracked.id]
        return None
