"""
Small typed async client for the Paperclip REST API.

This module deliberately has no Home Assistant imports so it can be split out
into a standalone library later. Paperclip's API is unversioned and still in
beta, so every parser here is tolerant of missing or extra fields.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote

import aiohttp

DEFAULT_TIMEOUT: Final = 30
HOSTNAME_NOT_ALLOWED_MARKER: Final = "hostname is not allowed"
BOARD_KEY_PREFIX: Final = "pcp_board_"
AGENT_KEY_PREFIX: Final = "pcp_"

type JsonObject = dict[str, Any]


class PaperclipError(Exception):
    """Base error for the Paperclip client."""


class PaperclipConnectionError(PaperclipError):
    """The server could not be reached or did not answer in time."""


class PaperclipResponseError(PaperclipError):
    """The server answered with an unexpected HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        """Store the HTTP status and the server's error message."""
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class PaperclipInvalidResponseError(PaperclipError):
    """
    A successful status came with a body that is not what Paperclip sends.

    Paperclip answers every API route with JSON, so a 2xx body of another
    shape (an HTML page from a proxy or captive portal) never comes from
    Paperclip itself. Reading it as an empty list would look like a company
    without agents and remove their devices.
    """


class PaperclipAuthError(PaperclipResponseError):
    """The credentials were rejected (401) or lack access (403)."""


class PaperclipForbiddenError(PaperclipAuthError):
    """
    The server refused one operation (403) for otherwise valid credentials.

    Paperclip answers 403 both for a key without access to the company and for
    a single refused write (a read-only membership, a missing grant). Polling
    still treats it as an auth failure; actions report it without a reauth.
    """


class PaperclipHostnameNotAllowedError(PaperclipResponseError):
    """The server's private-hostname guard rejected the Host header."""


class PaperclipNotFoundError(PaperclipResponseError):
    """The resource or route does not exist (404)."""


class PaperclipRateLimitError(PaperclipResponseError):
    """The server asked the client to slow down (429)."""

    def __init__(self, status: int, message: str, retry_after: float | None) -> None:
        """Store the suggested retry delay."""
        super().__init__(status, message)
        self.retry_after = retry_after


def _str(data: JsonObject, key: str, default: str = "") -> str:
    value = data.get(key)
    return value if isinstance(value, str) else default


def _opt_str(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _int(data: JsonObject, key: str, default: int = 0) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _obj(data: JsonObject, key: str) -> JsonObject:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp as sent by Paperclip."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _seg(value: str) -> str:
    """Escape one path segment, so an id can never reach another route."""
    return quote(value, safe="")


def is_board_key(api_key: str) -> bool:
    """Return True if the key looks like a Paperclip board API key."""
    return api_key.startswith(BOARD_KEY_PREFIX)


def is_agent_key(api_key: str) -> bool:
    """Return True if the key looks like an agent API key, not a board key."""
    return api_key.startswith(AGENT_KEY_PREFIX) and not is_board_key(api_key)


@dataclass(frozen=True, slots=True)
class Health:
    """Subset of GET /api/health."""

    status: str
    version: str | None
    commit: str | None
    deployment_mode: str | None

    @classmethod
    def from_dict(cls, data: JsonObject) -> Health:
        """Build from the API payload."""
        return cls(
            status=_str(data, "status", "unknown"),
            version=_opt_str(data, "version") or _opt_str(data, "serverVersion"),
            commit=_opt_str(data, "commit"),
            deployment_mode=_opt_str(data, "deploymentMode"),
        )


@dataclass(frozen=True, slots=True)
class Company:
    """Subset of a Paperclip company."""

    id: str
    name: str
    status: str
    issue_prefix: str
    spent_monthly_cents: int
    budget_monthly_cents: int

    @classmethod
    def from_dict(cls, data: JsonObject) -> Company:
        """Build from the API payload."""
        return cls(
            id=_str(data, "id"),
            name=_str(data, "name", "Paperclip"),
            status=_str(data, "status", "active"),
            issue_prefix=_str(data, "issuePrefix"),
            spent_monthly_cents=_int(data, "spentMonthlyCents"),
            budget_monthly_cents=_int(data, "budgetMonthlyCents"),
        )


@dataclass(frozen=True, slots=True)
class Agent:
    """Subset of a Paperclip agent."""

    id: str
    name: str
    url_key: str | None
    role: str | None
    title: str | None
    status: str
    pause_reason: str | None
    spent_monthly_cents: int
    last_heartbeat_at: datetime | None

    @classmethod
    def from_dict(cls, data: JsonObject) -> Agent:
        """Build from the API payload."""
        return cls(
            id=_str(data, "id"),
            name=_str(data, "name", "Agent"),
            url_key=_opt_str(data, "urlKey"),
            role=_opt_str(data, "role"),
            title=_opt_str(data, "title"),
            status=_str(data, "status", "unknown"),
            pause_reason=_opt_str(data, "pauseReason"),
            spent_monthly_cents=_int(data, "spentMonthlyCents"),
            last_heartbeat_at=parse_datetime(data.get("lastHeartbeatAt")),
        )


@dataclass(frozen=True, slots=True)
class Dashboard:
    """Subset of GET /api/companies/{id}/dashboard."""

    open_issues: int
    in_progress_issues: int
    blocked_issues: int
    running_agents: int
    pending_approvals: int
    month_spend_cents: int
    month_budget_cents: int

    @classmethod
    def from_dict(cls, data: JsonObject) -> Dashboard:
        """Build from the API payload."""
        tasks = _obj(data, "tasks")
        agents = _obj(data, "agents")
        costs = _obj(data, "costs")
        return cls(
            open_issues=_int(tasks, "open"),
            in_progress_issues=_int(tasks, "inProgress"),
            blocked_issues=_int(tasks, "blocked"),
            running_agents=_int(agents, "running"),
            pending_approvals=_int(data, "pendingApprovals"),
            month_spend_cents=_int(costs, "monthSpendCents"),
            month_budget_cents=_int(costs, "monthBudgetCents"),
        )


APPROVAL_TYPE_LABELS: Final = {
    "hire_agent": "Hire Agent",
    "approve_ceo_strategy": "CEO Strategy",
    "budget_override_required": "Budget Override",
    "request_board_approval": "Board Approval",
}


@dataclass(frozen=True, slots=True)
class Approval:
    """Subset of a Paperclip approval."""

    id: str
    type: str
    status: str
    title: str
    requested_by_agent_id: str | None
    created_at: datetime | None
    company_id: str | None = None

    @classmethod
    def from_dict(cls, data: JsonObject) -> Approval:
        """Build from the API payload, labelled the way the Paperclip UI does."""
        approval_type = _str(data, "type")
        payload = _obj(data, "payload")
        label = APPROVAL_TYPE_LABELS.get(approval_type, approval_type or "Approval")
        detail = next(
            (
                value.strip()
                for key in ("title", "name", "summary", "recommendedAction")
                if isinstance(value := payload.get(key), str) and value.strip()
            ),
            None,
        )
        return cls(
            id=_str(data, "id"),
            type=approval_type,
            status=_str(data, "status"),
            title=f"{label}: {detail}" if detail else label,
            requested_by_agent_id=_opt_str(data, "requestedByAgentId"),
            created_at=parse_datetime(data.get("createdAt")),
            company_id=_opt_str(data, "companyId"),
        )


@dataclass(frozen=True, slots=True)
class LiveRun:
    """Subset of a live (queued or running) heartbeat run."""

    id: str
    status: str
    agent_id: str
    issue_id: str | None

    @classmethod
    def from_dict(cls, data: JsonObject) -> LiveRun:
        """Build from the API payload."""
        return cls(
            id=_str(data, "id"),
            status=_str(data, "status"),
            agent_id=_str(data, "agentId"),
            issue_id=_opt_str(data, "issueId"),
        )


@dataclass(frozen=True, slots=True)
class Issue:
    """Subset of a Paperclip issue."""

    id: str
    identifier: str | None
    title: str
    status: str
    priority: str | None
    assignee_agent_id: str | None
    project_id: str | None
    created_at: datetime | None
    updated_at: datetime | None
    deduplicated: bool = False
    company_id: str | None = None

    @classmethod
    def from_dict(cls, data: JsonObject) -> Issue:
        """Build from the API payload."""
        return cls(
            id=_str(data, "id"),
            company_id=_opt_str(data, "companyId"),
            identifier=_opt_str(data, "identifier"),
            title=_str(data, "title"),
            status=_str(data, "status", "todo"),
            priority=_opt_str(data, "priority"),
            assignee_agent_id=_opt_str(data, "assigneeAgentId"),
            project_id=_opt_str(data, "projectId"),
            created_at=parse_datetime(data.get("createdAt")),
            updated_at=parse_datetime(data.get("updatedAt")),
            deduplicated=data.get("deduplicated") is True,
        )


@dataclass(frozen=True, slots=True)
class Comment:
    """Subset of an issue comment."""

    id: str
    issue_id: str
    author_type: str
    author_agent_id: str | None
    body: str
    created_at: datetime | None
    deleted: bool

    derived_author_agent_id: str | None = None

    @property
    def is_agent(self) -> bool:
        """
        Return True for a live comment written by an agent.

        Agents can post under a non-human sentinel user (``local-board``), so
        the comment keeps ``authorType`` "user" and the server attributes it
        to the agent through ``derivedAuthorAgentId`` instead.
        """
        if self.deleted:
            return False
        return self.author_type == "agent" or bool(self.derived_author_agent_id)

    @classmethod
    def from_dict(cls, data: JsonObject) -> Comment:
        """Build from the API payload."""
        derived = _opt_str(data, "derivedAuthorAgentId") or None
        return cls(
            id=_str(data, "id"),
            issue_id=_str(data, "issueId"),
            author_type=_str(data, "authorType"),
            author_agent_id=_opt_str(data, "authorAgentId") or derived,
            body=_str(data, "body"),
            created_at=parse_datetime(data.get("createdAt")),
            deleted=data.get("deletedAt") is not None,
            derived_author_agent_id=derived,
        )


@dataclass(frozen=True, slots=True)
class Project:
    """Subset of a Paperclip project."""

    id: str
    name: str

    @classmethod
    def from_dict(cls, data: JsonObject) -> Project:
        """Build from the API payload."""
        return cls(id=_str(data, "id"), name=_str(data, "name"))


@dataclass(frozen=True, slots=True)
class WebhookResult:
    """Outcome of firing a routine webhook trigger."""

    http_status: int
    status: str | None
    run_id: str | None
    linked_issue_id: str | None
    raw: JsonObject = field(default_factory=dict)


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    """
    Return the Paperclip ``hmac_sha256`` signature header value.

    The signed string is the timestamp header text, a literal dot, then the
    exact request body bytes; the digest is lowercase hex.
    """
    digest = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def _error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return str(payload["error"])
    return fallback


def _retry_after(headers: Any, payload: Any) -> float | None:
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None and isinstance(payload, dict):
        raw = payload.get("retryAfterSeconds")
    try:
        return float(raw) if raw is not None else None
    except TypeError, ValueError:
        return None


class PaperclipClient:
    """Async client for one Paperclip server."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Create a client; ``api_key`` may be empty for local_trusted mode."""
        self._session = session
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key or None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _headers(self, *, auth: bool = True) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        # Never send an empty or stale bearer: local_trusted mode validates any
        # Authorization header it receives and answers 401 to an unknown token.
        if auth and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: JsonObject | None = None,
    ) -> Any:
        url = f"{self.base_url}/api{path}"
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=self._timeout,
            ) as resp:
                text = await resp.text()
                status = resp.status
                headers = resp.headers
        except TimeoutError as err:
            msg = f"Timeout talking to {self.base_url}"
            raise PaperclipConnectionError(msg) from err
        except aiohttp.ClientError as err:
            msg = f"Cannot reach {self.base_url}: {err}"
            raise PaperclipConnectionError(msg) from err
        payload: Any = None
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
        if status < 300:  # noqa: PLR2004
            return payload
        message = _error_message(payload, text[:200] if text else "")
        self._raise_for_status(status, message, headers, payload)
        return None  # pragma: no cover

    @staticmethod
    def _raise_for_status(
        status: int, message: str, headers: Any, payload: Any
    ) -> None:
        if status == 401:  # noqa: PLR2004
            raise PaperclipAuthError(status, message)
        if status == 403:  # noqa: PLR2004
            if HOSTNAME_NOT_ALLOWED_MARKER in message.lower():
                raise PaperclipHostnameNotAllowedError(status, message)
            raise PaperclipForbiddenError(status, message)
        if status == 404:  # noqa: PLR2004
            raise PaperclipNotFoundError(status, message)
        if status == 429:  # noqa: PLR2004
            raise PaperclipRateLimitError(
                status, message, _retry_after(headers, payload)
            )
        raise PaperclipResponseError(status, message)

    async def _get_list(
        self, path: str, params: dict[str, str] | None = None
    ) -> list[JsonObject]:
        payload = await self._request("GET", path, params=params)
        if not isinstance(payload, list):
            msg = f"Expected a JSON list from {self.base_url}/api{path}"
            raise PaperclipInvalidResponseError(msg)
        return [item for item in payload if isinstance(item, dict)]

    async def _get_object(
        self, path: str, params: dict[str, str] | None = None
    ) -> JsonObject:
        payload = await self._request("GET", path, params=params)
        if not isinstance(payload, dict):
            msg = f"Expected a JSON object from {self.base_url}/api{path}"
            raise PaperclipInvalidResponseError(msg)
        return payload

    async def _post_object(
        self, path: str, body: JsonObject | None = None
    ) -> JsonObject:
        payload = await self._request("POST", path, json_body=body)
        return payload if isinstance(payload, dict) else {}

    # Read endpoints -------------------------------------------------------

    async def get_health(self) -> Health:
        """GET /api/health (validates the key if one is sent)."""
        return Health.from_dict(await self._get_object("/health"))

    async def list_companies(self) -> list[Company]:
        """List the companies this board key can open."""
        rows = await self._get_list("/companies", {"scope": "accessible"})
        return [Company.from_dict(row) for row in rows]

    async def get_company(self, company_id: str) -> Company:
        """Get one company."""
        return Company.from_dict(
            await self._get_object(f"/companies/{_seg(company_id)}")
        )

    async def list_agents(self, company_id: str) -> list[Agent]:
        """List non-terminated agents (the route rejects any query parameter)."""
        rows = await self._get_list(f"/companies/{_seg(company_id)}/agents")
        return [Agent.from_dict(row) for row in rows]

    async def get_dashboard(self, company_id: str) -> Dashboard:
        """Get the dashboard summary counts."""
        return Dashboard.from_dict(
            await self._get_object(f"/companies/{_seg(company_id)}/dashboard")
        )

    async def list_pending_approvals(self, company_id: str) -> list[Approval]:
        """List pending approvals, oldest first."""
        rows = await self._get_list(
            f"/companies/{_seg(company_id)}/approvals", {"status": "pending"}
        )
        approvals = [Approval.from_dict(row) for row in rows]
        epoch = datetime.min.replace(tzinfo=UTC)
        return sorted(approvals, key=lambda item: item.created_at or epoch)

    async def list_live_runs(self, company_id: str) -> list[LiveRun]:
        """List queued and running heartbeat runs."""
        rows = await self._get_list(f"/companies/{_seg(company_id)}/live-runs")
        return [LiveRun.from_dict(row) for row in rows]

    async def list_issues_updated_since(
        self, company_id: str, since: datetime, *, limit: int = 500, offset: int = 0
    ) -> list[Issue]:
        """
        List one page of issues updated after ``since`` (compact view).

        The server orders this list by last activity, not by ``updatedAt``,
        so a caller must read every page before trusting the newest
        ``updatedAt`` it saw.
        """
        params = {
            "view": "compact",
            "updatedSince": since.astimezone(UTC).isoformat(),
            "sortField": "updated",
            "sortDir": "asc",
            "limit": str(limit),
        }
        if offset:
            params["offset"] = str(offset)
        rows = await self._get_list(f"/companies/{_seg(company_id)}/issues", params)
        return [Issue.from_dict(row) for row in rows]

    async def get_issue(self, issue_id: str) -> Issue:
        """Get one issue by UUID or identifier (for example ACME-12)."""
        return Issue.from_dict(await self._get_object(f"/issues/{_seg(issue_id)}"))

    async def list_comments(
        self, issue_id: str, *, after: str | None = None, limit: int = 100
    ) -> list[Comment]:
        """
        List comments oldest first, optionally after a comment id.

        ``order=asc`` is required: the server defaults to newest first, and
        ``after`` then returns the comments older than the anchor.
        """
        params = {"order": "asc", "limit": str(limit)}
        if after:
            params["after"] = after
        rows = await self._get_list(f"/issues/{_seg(issue_id)}/comments", params)
        return [Comment.from_dict(row) for row in rows]

    async def list_recent_comments(
        self, issue_id: str, *, limit: int = 100
    ) -> list[Comment]:
        """List an issue's newest ``limit`` comments, returned oldest first."""
        rows = await self._get_list(
            f"/issues/{_seg(issue_id)}/comments",
            {"order": "desc", "limit": str(limit)},
        )
        comments = [Comment.from_dict(row) for row in reversed(rows)]
        return [comment for comment in comments if comment.id]

    async def newest_comment(self, issue_id: str) -> Comment | None:
        """Return an issue's newest comment, or None if it has none."""
        comments = await self.list_recent_comments(issue_id, limit=1)
        return comments[-1] if comments else None

    async def list_projects(self, company_id: str) -> list[Project]:
        """List non-archived projects."""
        rows = await self._get_list(f"/companies/{_seg(company_id)}/projects")
        return [Project.from_dict(row) for row in rows]

    # Write endpoints ------------------------------------------------------

    async def create_issue(  # noqa: PLR0913
        self,
        company_id: str,
        *,
        title: str,
        description: str | None = None,
        assignee_agent_id: str | None = None,
        project_id: str | None = None,
        priority: str | None = None,
        allow_duplicate: bool = False,
    ) -> Issue:
        """Create an issue; assigning an agent makes Paperclip wake it."""
        body: JsonObject = {"title": title}
        if description:
            body["description"] = description
        if assignee_agent_id:
            body["assigneeAgentId"] = assignee_agent_id
        if project_id:
            body["projectId"] = project_id
        if priority:
            body["priority"] = priority
        if allow_duplicate:
            body["allowDuplicate"] = True
        return Issue.from_dict(
            await self._post_object(f"/companies/{_seg(company_id)}/issues", body)
        )

    async def add_comment(self, issue_id: str, body: str) -> Comment:
        """Add a comment; this wakes the assignee (and reopens closed issues)."""
        return Comment.from_dict(
            await self._post_object(
                f"/issues/{_seg(issue_id)}/comments", {"body": body}
            )
        )

    async def pause_agent(self, agent_id: str) -> Agent:
        """Pause an agent (cancels its active runs)."""
        return Agent.from_dict(
            await self._post_object(f"/agents/{_seg(agent_id)}/pause")
        )

    async def resume_agent(self, agent_id: str) -> Agent:
        """Resume a paused agent (it becomes idle)."""
        return Agent.from_dict(
            await self._post_object(f"/agents/{_seg(agent_id)}/resume")
        )

    async def wake_agent(self, agent_id: str, *, reason: str | None = None) -> str:
        """Ask for an on-demand heartbeat; return the run or skip status."""
        body: JsonObject = {"source": "on_demand", "triggerDetail": "manual"}
        if reason:
            body["reason"] = reason
        result = await self._post_object(f"/agents/{_seg(agent_id)}/wakeup", body)
        return _str(result, "status", "queued")

    async def get_approval(self, approval_id: str) -> Approval:
        """Get one approval (any status)."""
        return Approval.from_dict(
            await self._get_object(f"/approvals/{_seg(approval_id)}")
        )

    async def approve(self, approval_id: str, note: str | None = None) -> Approval:
        """Approve a pending approval."""
        return Approval.from_dict(
            await self._post_object(
                f"/approvals/{_seg(approval_id)}/approve", {"decisionNote": note}
            )
        )

    async def reject(self, approval_id: str, note: str | None = None) -> Approval:
        """Reject a pending approval."""
        return Approval.from_dict(
            await self._post_object(
                f"/approvals/{_seg(approval_id)}/reject", {"decisionNote": note}
            )
        )

    async def fire_routine_webhook(
        self,
        public_id: str,
        payload: JsonObject,
        *,
        secret: str | None,
        idempotency_key: str | None = None,
        timestamp: int | None = None,
    ) -> WebhookResult:
        """
        POST a routine webhook trigger, HMAC-signed when a secret is given.

        This route ignores board and agent credentials, so no Authorization
        header is sent; only the trigger's own secret counts. ``timestamp``
        (Unix time in milliseconds) defaults to now. Paperclip only honours
        ``idempotency_key`` for unsigned triggers: a signed delivery is
        de-duplicated by its own timestamp and signature instead.
        """
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if secret:
            # Milliseconds: Paperclip rejects a second delivery with the same
            # timestamp and signature as a replay, so second resolution would
            # refuse two identical fires within one second.
            ts = str(
                timestamp if timestamp is not None else time.time_ns() // 1_000_000
            )
            headers["X-Paperclip-Timestamp"] = ts
            headers["X-Paperclip-Signature"] = sign_webhook(secret, ts, body)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        url = f"{self.base_url}/api/routine-triggers/public/{_seg(public_id)}/fire"
        try:
            async with self._session.post(
                url, data=body, headers=headers, timeout=self._timeout
            ) as resp:
                text = await resp.text()
                status = resp.status
                resp_headers = resp.headers
        except (TimeoutError, aiohttp.ClientError) as err:
            msg = f"Cannot reach {self.base_url}: {err}"
            raise PaperclipConnectionError(msg) from err
        data: Any = None
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        if status >= 300:  # noqa: PLR2004
            self._raise_for_status(
                status, _error_message(data, text[:200]), resp_headers, data
            )
        result = data if isinstance(data, dict) else {}
        return WebhookResult(
            http_status=status,
            status=_opt_str(result, "status"),
            run_id=_opt_str(result, "id"),
            linked_issue_id=_opt_str(result, "linkedIssueId"),
            raw=result,
        )


__all__ = [
    "Agent",
    "Approval",
    "Comment",
    "Company",
    "Dashboard",
    "Health",
    "Issue",
    "LiveRun",
    "PaperclipAuthError",
    "PaperclipClient",
    "PaperclipConnectionError",
    "PaperclipError",
    "PaperclipForbiddenError",
    "PaperclipHostnameNotAllowedError",
    "PaperclipInvalidResponseError",
    "PaperclipNotFoundError",
    "PaperclipRateLimitError",
    "PaperclipResponseError",
    "Project",
    "WebhookResult",
    "is_agent_key",
    "is_board_key",
    "sign_webhook",
]
