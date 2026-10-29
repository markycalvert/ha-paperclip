"""Actions (services) of the Paperclip integration."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

import probatio
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    service,
)
from homeassistant.helpers.json import json_bytes
from homeassistant.util.json import json_loads_object

from .api import (
    PaperclipAuthError,
    PaperclipError,
    PaperclipForbiddenError,
    PaperclipNotFoundError,
    PaperclipResponseError,
)
from .const import (
    ATTR_AGENT,
    ATTR_ALLOW_DUPLICATE,
    ATTR_APPROVAL_ID,
    ATTR_ASSIGNEE,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DECISION_NOTE,
    ATTR_DESCRIPTION,
    ATTR_IDEMPOTENCY_KEY,
    ATTR_ISSUE,
    ATTR_MESSAGE,
    ATTR_PAYLOAD,
    ATTR_PRIORITY,
    ATTR_PROJECT,
    ATTR_PUBLIC_ID,
    ATTR_REASON,
    ATTR_SECRET,
    ATTR_TITLE,
    ATTR_WAIT_FOR_REPLY,
    CONF_ALLOW_APPROVALS,
    DOMAIN,
    ISSUE_PRIORITIES,
    MAX_REPLY_WAIT,
    REPLY_MAX_LENGTH,
    SERVICE_ADD_COMMENT,
    SERVICE_APPROVE,
    SERVICE_ASK_AGENT,
    SERVICE_CREATE_ISSUE,
    SERVICE_FIRE_ROUTINE_WEBHOOK,
    SERVICE_REJECT,
    SERVICE_WAKE_AGENT,
    TITLE_MAX_LENGTH,
)
from .coordinator import UUID_RE, PaperclipCoordinator, shorten, truncate

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from .api import Comment
    from .data import PaperclipConfigEntry
    from .tracker import TrackedIssue

PUBLIC_ID_RE = re.compile(r"^[0-9a-f]{24}$")
PUBLIC_URL_RE = re.compile(r"/routine-triggers/public/([0-9a-f]{24})(?:/fire)?/?$")
WAKE_REASON = "Requested from Home Assistant"

_WAIT = probatio.All(probatio.Coerce(float), probatio.Range(min=0, max=MAX_REPLY_WAIT))
_NON_EMPTY = probatio.All(cv.string, probatio.Length(min=1))
# Actions that act on a company or server need the entry: no implicit default
# target. Actions that target an agent only use it to tell agents apart.
_ENTRY = {probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string}
_ENTRY_HINT = {probatio.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}

ASK_AGENT_SCHEMA = probatio.Schema(
    {
        **_ENTRY_HINT,
        probatio.Required(ATTR_AGENT): cv.string,
        probatio.Required(ATTR_MESSAGE): _NON_EMPTY,
        probatio.Optional(ATTR_TITLE): cv.string,
        probatio.Optional(ATTR_PROJECT): cv.string,
        probatio.Optional(ATTR_PRIORITY): probatio.In(ISSUE_PRIORITIES),
        probatio.Optional(ATTR_WAIT_FOR_REPLY, default=0): _WAIT,
    }
)
CREATE_ISSUE_SCHEMA = probatio.Schema(
    {
        **_ENTRY,
        probatio.Required(ATTR_TITLE): _NON_EMPTY,
        probatio.Optional(ATTR_DESCRIPTION): cv.string,
        probatio.Optional(ATTR_ASSIGNEE): cv.string,
        probatio.Optional(ATTR_PROJECT): cv.string,
        probatio.Optional(ATTR_PRIORITY): probatio.In(ISSUE_PRIORITIES),
        probatio.Optional(ATTR_ALLOW_DUPLICATE, default=False): cv.boolean,
    }
)
ADD_COMMENT_SCHEMA = probatio.Schema(
    {
        **_ENTRY,
        probatio.Required(ATTR_ISSUE): _NON_EMPTY,
        probatio.Required(ATTR_MESSAGE): _NON_EMPTY,
        probatio.Optional(ATTR_WAIT_FOR_REPLY, default=0): _WAIT,
    }
)
WAKE_AGENT_SCHEMA = probatio.Schema(
    {
        **_ENTRY_HINT,
        probatio.Required(ATTR_AGENT): cv.string,
        probatio.Optional(ATTR_REASON): cv.string,
    }
)
FIRE_WEBHOOK_SCHEMA = probatio.Schema(
    {
        **_ENTRY,
        probatio.Required(ATTR_PUBLIC_ID): _NON_EMPTY,
        probatio.Optional(ATTR_SECRET): cv.string,
        probatio.Optional(ATTR_PAYLOAD, default=dict): dict,
        probatio.Optional(ATTR_IDEMPOTENCY_KEY): cv.string,
    }
)
APPROVAL_SCHEMA = probatio.Schema(
    {
        **_ENTRY,
        probatio.Required(ATTR_APPROVAL_ID): _NON_EMPTY,
        probatio.Optional(ATTR_DECISION_NOTE): cv.string,
    }
)


def _entry(hass: HomeAssistant, entry_id: str) -> PaperclipConfigEntry:
    """Return the loaded Paperclip entry with this id."""
    return cast(
        "PaperclipConfigEntry", service.async_get_config_entry(hass, DOMAIN, entry_id)
    )


def _loaded_entries(hass: HomeAssistant) -> list[PaperclipConfigEntry]:
    return [
        cast("PaperclipConfigEntry", entry)
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


@callback
def _resolve_agent(
    hass: HomeAssistant, agent_ref: str, entry_id: str | None
) -> tuple[PaperclipConfigEntry, str]:
    """
    Resolve an agent device id, agent id or agent name to (entry, agent id).

    With ``entry_id``, the agent must belong to that entry: an agent device of
    another Paperclip entry (another company) is refused.
    """
    if dr.async_get(hass).async_get(agent_ref) is not None:
        # Agent devices belong to the entry directly. A foreign child device
        # resolved through its parent fails the identifier check below.
        device, entry = service.async_get_device_and_config_entry(
            hass, DOMAIN, agent_ref
        )
        paperclip_entry = cast("PaperclipConfigEntry", entry)
        if entry_id and paperclip_entry.entry_id != entry_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="agent_wrong_entry",
                translation_placeholders={
                    "agent": device.name or agent_ref,
                    "title": paperclip_entry.title,
                },
            )
        agents = paperclip_entry.runtime_data.coordinator.data.agents
        for domain, value in device.identifiers:
            if domain == DOMAIN and value in agents:
                return paperclip_entry, value
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_an_agent",
            translation_placeholders={"agent": device.name or agent_ref},
        )

    entries = [_entry(hass, entry_id)] if entry_id else _loaded_entries(hass)
    ref = agent_ref.strip().casefold()
    matches = [
        (entry, agent.id)
        for entry in entries
        for agent in entry.runtime_data.coordinator.data.agents.values()
        if agent.id == agent_ref or agent.name.casefold() == ref
    ]
    if len(matches) == 1:
        return matches[0]
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="agent_ambiguous" if matches else "agent_not_found",
        translation_placeholders={"agent": agent_ref},
    )


async def _call[T](entry: PaperclipConfigEntry, awaitable: Awaitable[T]) -> T:
    """Await a client call and turn client errors into Home Assistant errors."""
    try:
        return await awaitable
    except PaperclipForbiddenError as err:
        # A 403 here refuses this one operation (a read-only membership, a
        # missing grant). The key still works, so a reauth would not help.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="forbidden",
            translation_placeholders={"error": err.message},
        ) from err
    except PaperclipAuthError as err:
        entry.async_start_reauth(entry.runtime_data.coordinator.hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="invalid_auth",
        ) from err
    except PaperclipNotFoundError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_found",
            translation_placeholders={"error": err.message},
        ) from err
    except PaperclipError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"error": str(err)},
        ) from err


async def _project_id(
    entry: PaperclipConfigEntry, project_ref: str | None
) -> str | None:
    """Return the project id from the field, else the entry's default project."""
    coordinator = entry.runtime_data.coordinator
    project_ref = project_ref or coordinator.default_project
    if not project_ref:
        return None
    project_id = await _call(entry, coordinator.async_resolve_project(project_ref))
    if project_id is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="project_not_found",
            translation_placeholders={"project": project_ref},
        )
    return project_id


def _issue_response(
    coordinator: PaperclipCoordinator, tracked: TrackedIssue
) -> dict[str, Any]:
    return {
        "issue_id": tracked.id,
        "identifier": tracked.identifier,
        "url": coordinator.issue_url(tracked.identifier or tracked.id),
        "status": tracked.status,
    }


def _reply_response(reply: Comment | None) -> dict[str, Any]:
    # The reply is cut like the event body; the issue URL has the full text.
    return {
        "replied": reply is not None,
        "reply": truncate(reply.body, REPLY_MAX_LENGTH) if reply else None,
        "reply_comment_id": reply.id if reply else None,
    }


async def _async_ask_agent(call: ServiceCall) -> ServiceResponse:
    entry, agent_id = _resolve_agent(
        call.hass, call.data[ATTR_AGENT], call.data.get(ATTR_CONFIG_ENTRY_ID)
    )
    coordinator = entry.runtime_data.coordinator
    message: str = call.data[ATTR_MESSAGE]
    project_id = await _project_id(entry, call.data.get(ATTR_PROJECT))
    tracked, _ = await _call(
        entry,
        coordinator.async_create_tracked_issue(
            title=call.data.get(ATTR_TITLE) or shorten(message, TITLE_MAX_LENGTH),
            description=message,
            agent_id=agent_id,
            project_id=project_id,
            priority=call.data.get(ATTR_PRIORITY),
            allow_duplicate=True,
        ),
    )
    if not call.return_response:
        # Nobody receives the reply, so do not hold the caller for it; it
        # still arrives as a paperclip_agent_replied event.
        return None
    reply = await coordinator.async_wait_for_reply(
        tracked, call.data[ATTR_WAIT_FOR_REPLY]
    )
    return {
        **_issue_response(coordinator, tracked),
        "agent_id": agent_id,
        "agent_name": coordinator.agent_name(agent_id),
        **_reply_response(reply),
    }


async def _async_create_issue(call: ServiceCall) -> ServiceResponse:
    entry = _entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    coordinator = entry.runtime_data.coordinator
    agent_id: str | None = None
    if assignee := call.data.get(ATTR_ASSIGNEE):
        _, agent_id = _resolve_agent(call.hass, assignee, entry.entry_id)
    project_id = await _project_id(entry, call.data.get(ATTR_PROJECT))
    tracked, deduplicated = await _call(
        entry,
        coordinator.async_create_tracked_issue(
            title=call.data[ATTR_TITLE],
            description=call.data.get(ATTR_DESCRIPTION),
            agent_id=agent_id,
            project_id=project_id,
            priority=call.data.get(ATTR_PRIORITY),
            allow_duplicate=call.data[ATTR_ALLOW_DUPLICATE],
        ),
    )
    if not call.return_response:
        return None
    return {
        **_issue_response(coordinator, tracked),
        "assignee_agent_id": tracked.agent_id,
        "deduplicated": deduplicated,
    }


async def _async_add_comment(call: ServiceCall) -> ServiceResponse:
    entry = _entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    coordinator = entry.runtime_data.coordinator
    tracked, comment = await _call(
        entry, coordinator.async_comment(call.data[ATTR_ISSUE], call.data[ATTR_MESSAGE])
    )
    if not call.return_response:
        return None
    reply = await coordinator.async_wait_for_reply(
        tracked, call.data[ATTR_WAIT_FOR_REPLY], after=comment
    )
    return {
        "comment_id": comment.id,
        **_issue_response(coordinator, tracked),
        **_reply_response(reply),
    }


async def _async_wake_agent(call: ServiceCall) -> ServiceResponse:
    entry, agent_id = _resolve_agent(
        call.hass, call.data[ATTR_AGENT], call.data.get(ATTR_CONFIG_ENTRY_ID)
    )
    coordinator = entry.runtime_data.coordinator
    status = await _call(
        entry,
        coordinator.client.wake_agent(
            agent_id, reason=call.data.get(ATTR_REASON) or WAKE_REASON
        ),
    )
    await coordinator.async_request_refresh()
    if not call.return_response:
        return None
    return {"agent_id": agent_id, "status": status}


async def _async_fire_routine_webhook(call: ServiceCall) -> ServiceResponse:
    entry = _entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    raw_id: str = call.data[ATTR_PUBLIC_ID].strip()
    match = PUBLIC_URL_RE.search(raw_id)
    public_id = match.group(1) if match else raw_id
    if not PUBLIC_ID_RE.match(public_id):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_public_id"
        )
    # Service data can hold values plain JSON cannot (a YAML date, a set).
    # Home Assistant's encoder writes dates as ISO strings and sets as lists;
    # anything it cannot encode is the caller's mistake.
    try:
        payload = json_loads_object(json_bytes(call.data[ATTR_PAYLOAD]))
    except (TypeError, ValueError) as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_payload",
            translation_placeholders={"error": str(err)},
        ) from err
    try:
        result = await entry.runtime_data.client.fire_routine_webhook(
            public_id,
            payload,
            secret=call.data.get(ATTR_SECRET),
            idempotency_key=call.data.get(ATTR_IDEMPOTENCY_KEY),
        )
    except PaperclipResponseError as err:
        # 401 here means a bad trigger secret, never a bad board key.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="webhook_rejected",
            translation_placeholders={
                "status": str(err.status),
                "error": err.message,
            },
        ) from err
    except PaperclipError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"error": str(err)},
        ) from err
    if not call.return_response:
        return None
    return {
        "http_status": result.http_status,
        "status": result.status,
        "run_id": result.run_id,
        "linked_issue_id": result.linked_issue_id,
    }


async def _async_decide(call: ServiceCall, *, approve: bool) -> ServiceResponse:
    entry = _entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    if not entry.options.get(CONF_ALLOW_APPROVALS, False):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="approvals_disabled",
            translation_placeholders={"title": entry.title},
        )
    approval_id: str = call.data[ATTR_APPROVAL_ID].strip().lower()
    if not UUID_RE.match(approval_id):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_approval_id"
        )
    client = entry.runtime_data.client
    coordinator = entry.runtime_data.coordinator
    # A board key can decide approvals in every company its user belongs to,
    # but the option above only allows this entry's company.
    pending = coordinator.data.approvals or []
    if not any(item.id == approval_id for item in pending):
        found = await _call(entry, client.get_approval(approval_id))
        if found.company_id != coordinator.company_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="approval_wrong_company",
                translation_placeholders={"title": entry.title},
            )
    decide = client.approve if approve else client.reject
    approval = await _call(
        entry, decide(approval_id, call.data.get(ATTR_DECISION_NOTE))
    )
    await coordinator.async_request_refresh()
    if not call.return_response:
        return None
    return {"approval_id": approval.id, "status": approval.status}


async def _async_approve(call: ServiceCall) -> ServiceResponse:
    return await _async_decide(call, approve=True)


async def _async_reject(call: ServiceCall) -> ServiceResponse:
    return await _async_decide(call, approve=False)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register every Paperclip action (once, from async_setup)."""
    for name, handler, schema in (
        (SERVICE_ASK_AGENT, _async_ask_agent, ASK_AGENT_SCHEMA),
        (SERVICE_CREATE_ISSUE, _async_create_issue, CREATE_ISSUE_SCHEMA),
        (SERVICE_ADD_COMMENT, _async_add_comment, ADD_COMMENT_SCHEMA),
        (SERVICE_WAKE_AGENT, _async_wake_agent, WAKE_AGENT_SCHEMA),
        (
            SERVICE_FIRE_ROUTINE_WEBHOOK,
            _async_fire_routine_webhook,
            FIRE_WEBHOOK_SCHEMA,
        ),
        (SERVICE_APPROVE, _async_approve, APPROVAL_SCHEMA),
        (SERVICE_REJECT, _async_reject, APPROVAL_SCHEMA),
    ):
        hass.services.async_register(
            DOMAIN,
            name,
            handler,
            schema=schema,
            supports_response=SupportsResponse.OPTIONAL,
        )
