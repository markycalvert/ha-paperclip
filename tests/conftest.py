"""Fixtures for the Paperclip integration tests."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.const import (
    CONF_API_KEY,
    CONF_URL,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.paperclip.const import (
    CONF_ALLOW_APPROVALS,
    CONF_COMPANY_ID,
    CONF_DEFAULT_PROJECT,
    CONF_REPLY_WAIT,
    DOMAIN,
    NO_PROJECT,
)

BASE_URL = "http://paperclip.local:3100"
API_KEY = "pcp_board_" + "0" * 48
COMPANY_ID = "11111111-1111-4111-8111-111111111111"
CEO_ID = "33333333-3333-4333-8333-333333333333"
ENGINEER_ID = "33333333-3333-4333-8333-333333333334"
HOME_OPS_ID = "33333333-3333-4333-8333-333333333335"
APPROVAL_ID = "99999999-9999-4999-8999-999999999999"
PROJECT_ID = "77777777-7777-4777-8777-777777777777"
RUNNING_ISSUE_ID = "55555555-5555-4555-8555-555555555500"
PUBLIC_ID = "0123456789abcdef01234567"
# A second company on the same server, with one agent of its own.
OTHER_COMPANY_ID = "11111111-1111-4111-8111-111111111112"
OTHER_AGENT_ID = "33333333-3333-4333-8333-333333333336"

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str) -> Any:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


class FakePaperclip:
    """An in-memory Paperclip server behind aioclient_mock."""

    def __init__(self) -> None:
        """Seed the server with the neutral sample data."""
        self.now = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
        self.health: Any = load_json("health.json")
        self.companies: list[dict[str, Any]] = load_json("companies.json")
        self.agents: list[dict[str, Any]] = load_json("agents.json")
        self.other_agents: list[dict[str, Any]] = [
            {
                **self.agents[2],
                "id": OTHER_AGENT_ID,
                "companyId": OTHER_COMPANY_ID,
                "name": "Gardener",
                "urlKey": "gardener",
            }
        ]
        self.dashboard: Any = load_json("dashboard.json")
        self.approvals: list[dict[str, Any]] = load_json("approvals.json")
        self.live_runs: list[dict[str, Any]] = load_json("live_runs.json")
        self.projects: list[dict[str, Any]] = load_json("projects.json")
        running = load_json("running_issue.json")
        self.issues: dict[str, dict[str, Any]] = {running["id"]: running}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.counter = 42
        self.overrides: dict[tuple[str, str], tuple[int, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.on_get_comments: Callable[[str], None] | None = None

    # Helpers used by tests ----------------------------------------------

    def tick(self) -> str:
        """Advance the server clock and return it as an ISO string."""
        self.now += timedelta(seconds=1)
        return _iso(self.now)

    def fail(self, method: str, path: str, status: int, error: str = "boom") -> None:
        """Make one route answer with an error."""
        self.overrides[(method, path)] = (status, {"error": error})

    def proxy_not_found(self, method: str, path: str) -> None:
        """Make one route answer a reverse proxy's plain-text 404."""
        self.overrides[(method, path)] = (404, b"404 page not found\n")

    def disconnect(self, method: str, path: str) -> None:
        """Make one route fail at the transport level."""
        self.overrides[(method, path)] = (0, None)

    def clear_failures(self) -> None:
        """Remove all route overrides."""
        self.overrides.clear()

    def set_issue_status(self, issue_id: str, status: str) -> None:
        """Change an issue's status as an agent would."""
        self.issues[issue_id]["status"] = status
        self.issues[issue_id]["updatedAt"] = self.tick()

    def agent_comment(self, issue_id: str, agent_id: str, body: str) -> dict[str, Any]:
        """Post a comment authored by an agent."""
        return self._add_comment(issue_id, body, author_type="agent", agent_id=agent_id)

    def sentinel_agent_comment(
        self, issue_id: str, agent_id: str, body: str
    ) -> dict[str, Any]:
        """Post an agent comment stored under the local-board sentinel user."""
        comment = self._add_comment(issue_id, body)
        comment["authorUserId"] = "local-board"
        comment["derivedAuthorAgentId"] = agent_id
        return comment

    def calls_to(
        self, method: str, path: str
    ) -> list[tuple[str, str, dict[str, str], Any]]:
        """Return recorded calls for one route."""
        return [call for call in self.calls if call[0] == method and call[1] == path]

    # Request handling -----------------------------------------------------

    def _add_comment(
        self,
        issue_id: str,
        body: str,
        *,
        author_type: str = "user",
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        stamp = self.tick()
        comments = self.comments.setdefault(issue_id, [])
        comment = {
            "id": f"66666666-6666-4666-8666-{len(self.calls):04d}{len(comments):08d}",
            "companyId": COMPANY_ID,
            "issueId": issue_id,
            "authorType": author_type,
            "authorAgentId": agent_id,
            "authorUserId": "user_abc" if author_type == "user" else None,
            "body": body,
            "deletedAt": None,
            "createdAt": stamp,
            "updatedAt": stamp,
        }
        comments.append(comment)
        return comment

    def _find_issue(self, ref: str) -> dict[str, Any] | None:
        if ref in self.issues:
            return self.issues[ref]
        return next(
            (item for item in self.issues.values() if item["identifier"] == ref), None
        )

    def _route(  # noqa: PLR0911, PLR0912
        self, method: str, path: str, query: dict[str, str], body: Any
    ) -> tuple[int, Any]:
        if (method, path) in self.overrides:
            return self.overrides[(method, path)]
        company = f"/api/companies/{COMPANY_ID}"
        if method == "GET":
            if path == "/api/health":
                return 200, self.health
            if path == "/api/companies":
                return 200, self.companies
            if path == company:
                return 200, self.companies[0]
            if path == f"{company}/agents":
                if query:
                    return 400, {"error": "Unsupported query parameter"}
                return 200, self.agents
            if path == f"{company}/dashboard":
                return 200, self.dashboard
            if path == f"{company}/approvals":
                return 200, [
                    a
                    for a in self.approvals
                    if a["status"] == query["status"] and a["companyId"] == COMPANY_ID
                ]
            if path == f"{company}/live-runs":
                return 200, self.live_runs
            if path == f"{company}/projects":
                return 200, self.projects
            if path == f"{company}/issues":
                return 200, self._list_issues(query)
            if match := re.fullmatch(r"/api/issues/([^/]+)/comments", path):
                issue = self._find_issue(match.group(1))
                if issue is None:
                    return 404, {"error": "Issue not found"}
                if self.on_get_comments:
                    self.on_get_comments(issue["id"])
                return 200, self._list_comments(issue["id"], query)
            if match := re.fullmatch(r"/api/issues/([^/]+)", path):
                issue = self._find_issue(match.group(1))
                return (200, issue) if issue else (404, {"error": "Issue not found"})
        if method == "POST":
            if path == f"{company}/issues":
                return self._create_issue(body)
            if match := re.fullmatch(r"/api/issues/([^/]+)/comments", path):
                issue = self._find_issue(match.group(1))
                if issue is None:
                    return 404, {"error": "Issue not found"}
                return 201, self._add_comment(issue["id"], body["body"])
            if match := re.fullmatch(r"/api/agents/([^/]+)/(pause|resume)", path):
                agent = next(a for a in self.agents if a["id"] == match.group(1))
                agent["status"] = "paused" if match.group(2) == "pause" else "idle"
                return 200, agent
            if match := re.fullmatch(r"/api/agents/([^/]+)/wakeup", path):
                return 202, {
                    "id": "44444444-4444-4444-8444-444444444445",
                    "agentId": match.group(1),
                    "status": "queued",
                    "invocationSource": "on_demand",
                    "triggerDetail": "manual",
                }
            if match := re.fullmatch(r"/api/approvals/([^/]+)/(approve|reject)", path):
                approval = next(a for a in self.approvals if a["id"] == match.group(1))
                approval["status"] = (
                    "approved" if match.group(2) == "approve" else "rejected"
                )
                approval["decisionNote"] = body.get("decisionNote")
                return 200, approval
            if path == f"/api/routine-triggers/public/{PUBLIC_ID}/fire":
                return 202, {
                    "id": "88888888-8888-4888-8888-888888888888",
                    "routineId": "88888888-8888-4888-8888-888888888889",
                    "source": "webhook",
                    "status": "issue_created",
                    "linkedIssueId": RUNNING_ISSUE_ID,
                }
        return 404, {"error": "Not found"}

    def _route_approval(self, method: str, path: str) -> tuple[int, Any] | None:
        """Answer GET /api/approvals/{id}; other approval routes are in _route."""
        match = re.fullmatch(r"/api/approvals/([^/]+)", path)
        if method != "GET" or match is None or (method, path) in self.overrides:
            return None
        approval = next((a for a in self.approvals if a["id"] == match.group(1)), None)
        if approval is None:
            return 404, {"error": "Approval not found"}
        return 200, approval

    def _route_other_company(
        self, method: str, path: str, query: dict[str, str], body: Any
    ) -> tuple[int, Any] | None:
        """Answer the second company's own routes; the rest is served as Acme's."""
        other = f"/api/companies/{OTHER_COMPANY_ID}"
        if method == "GET" and path == other:
            return 200, {
                **self.companies[0],
                "id": OTHER_COMPANY_ID,
                "name": "Globex",
                "issuePrefix": "GLO",
            }
        if method == "GET" and path == f"{other}/agents":
            return 200, self.other_agents
        if path.startswith(other):
            company = f"/api/companies/{COMPANY_ID}"
            return self._route(method, company + path.removeprefix(other), query, body)
        return None

    def _list_issues(self, query: dict[str, str]) -> list[dict[str, Any]]:
        """
        List issues like Paperclip's sortField=updated.

        The real server orders by last activity first (``lastActivityAt``
        here), which need not follow ``updatedAt``; then it pages.
        """
        since = datetime.fromisoformat(query["updatedSince"])
        rows = [
            item
            for item in self.issues.values()
            if datetime.fromisoformat(item["updatedAt"]) > since
        ]
        rows.sort(
            key=lambda item: (
                item.get("lastActivityAt", item["updatedAt"]),
                item["updatedAt"],
                item["id"],
            ),
            reverse=query.get("sortDir") != "asc",
        )
        offset = int(query.get("offset", 0))
        return rows[offset : offset + int(query.get("limit", 500))]

    def _list_comments(
        self, issue_id: str, query: dict[str, str]
    ) -> list[dict[str, Any]]:
        """
        List comments like Paperclip: newest first unless order=asc.

        ``after`` returns the comments after the anchor in the chosen order,
        so without order=asc it returns the OLDER ones.
        """
        comments = list(self.comments.get(issue_id, []))
        if query.get("order", "desc") != "asc":
            comments.reverse()
        if after := query.get("after"):
            ids = [item["id"] for item in comments]
            if after not in ids:
                return []
            comments = comments[ids.index(after) + 1 :]
        return comments[: int(query.get("limit", 100))]

    def _create_issue(self, body: dict[str, Any]) -> tuple[int, Any]:
        if not body.get("allowDuplicate"):
            for item in self.issues.values():
                if item["title"].casefold() == body["title"].casefold() and item[
                    "status"
                ] not in ("done", "cancelled"):
                    return 200, {
                        **item,
                        "deduplicated": True,
                        "deduplicationReason": "recent_open_title",
                    }
        self.counter += 1
        stamp = self.tick()
        issue_id = f"55555555-5555-4555-8555-{self.counter:012d}"
        issue = {
            "id": issue_id,
            "companyId": COMPANY_ID,
            "identifier": f"ACME-{self.counter}",
            "issueNumber": self.counter,
            "title": body["title"],
            "description": body.get("description"),
            "status": "todo" if body.get("assigneeAgentId") else "backlog",
            "priority": body.get("priority", "medium"),
            "assigneeAgentId": body.get("assigneeAgentId"),
            "projectId": body.get("projectId"),
            "createdAt": stamp,
            "updatedAt": stamp,
        }
        self.issues[issue_id] = issue
        return 201, issue

    async def handle(
        self, method: str, url: URL, data: Any
    ) -> AiohttpClientMockResponse:
        """Answer one mocked request."""
        method = method.upper()
        body: Any = data
        if isinstance(data, bytes | str):
            try:
                body = json.loads(data)
            except ValueError:
                body = None
        query = dict(url.query)
        self.calls.append((method, url.path, query, body))
        status, payload = (
            self._route_approval(method, url.path)
            or self._route_other_company(method, url.path, query, body)
            or self._route(method, url.path, query, body)
        )
        if status == 0:
            msg = "Connection refused"
            raise aiohttp.ClientConnectionError(msg)
        if isinstance(payload, bytes):
            return AiohttpClientMockResponse(
                method, url, status=status, text=payload.decode()
            )
        return AiohttpClientMockResponse(
            method, url, status=status, json=copy.deepcopy(payload)
        )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading the custom integration in every test."""
    return


@pytest.fixture(autouse=True)
async def setup_homeassistant(hass: HomeAssistant) -> None:
    """Set up the core integration that conversation agents depend on."""
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture(autouse=True)
def fast_reply_polling() -> Generator[None]:
    """Poll for agent replies without real delays."""
    with patch("custom_components.paperclip.coordinator.REPLY_POLL_INTERVAL", 0.01):
        yield


@pytest.fixture
def fake(aioclient_mock: AiohttpClientMocker) -> FakePaperclip:
    """Serve every request from a FakePaperclip."""
    server = FakePaperclip()
    pattern = re.compile(r"^http://paperclip\.local:3100/")
    for method in ("get", "post"):
        aioclient_mock.request(method, pattern, side_effect=server.handle)
    return server


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for the Acme company."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Acme",
        unique_id=COMPANY_ID,
        data={
            CONF_URL: BASE_URL,
            CONF_API_KEY: API_KEY,
            CONF_VERIFY_SSL: True,
            CONF_COMPANY_ID: COMPANY_ID,
        },
        options={
            # Tests that expect no reply should not sleep on the real clock.
            CONF_REPLY_WAIT: 0.05,
            CONF_ALLOW_APPROVALS: False,
            CONF_DEFAULT_PROJECT: NO_PROJECT,
        },
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, fake: FakePaperclip, mock_config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the integration with the fake server."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
