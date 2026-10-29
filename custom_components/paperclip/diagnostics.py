"""Diagnostics for the Paperclip integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_URL

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import PaperclipConfigEntry

TO_REDACT = {
    CONF_API_KEY,
    CONF_URL,
    "name",
    "title",
    "body",
    "identifier",
    "commit",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001
    entry: PaperclipConfigEntry,
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    tracker = coordinator.tracker
    return async_redact_data(
        {
            "entry": {
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "health": asdict(data.health),
            "company": {
                "id": data.company.id,
                "name": data.company.name,
                "status": data.company.status,
            },
            "agents": [
                {
                    "id": agent.id,
                    "name": agent.name,
                    "role": agent.role,
                    "status": agent.status,
                    "pause_reason": agent.pause_reason,
                }
                for agent in data.agents.values()
            ],
            "dashboard": asdict(data.dashboard) if data.dashboard else None,
            "pending_approvals": len(data.approvals)
            if data.approvals is not None
            else None,
            "live_runs": [
                {"id": run.id, "status": run.status, "agent_id": run.agent_id}
                for run in data.live_runs or []
            ],
            "unsupported_routes": sorted(coordinator.unsupported),
            "tracked_issues": [
                {
                    "id": item.id,
                    "identifier": item.identifier,
                    "title": item.title,
                    "status": item.status,
                    "agent_id": item.agent_id,
                }
                for item in tracker.issues.values()
            ],
            "conversations": len(tracker.conversations),
            "last_update_success": coordinator.last_update_success,
        },
        TO_REDACT,
    )
