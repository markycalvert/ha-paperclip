"""Pause/resume switches for Paperclip agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from .api import Agent, PaperclipError
from .const import DOMAIN
from .entity import PaperclipAgentEntity, add_agent_entities

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import PaperclipCoordinator
    from .data import PaperclipConfigEntry

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: PaperclipConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one switch per agent."""

    def _factory(
        coordinator: PaperclipCoordinator, company_device_id: str, agent: Agent
    ) -> list[Entity]:
        return [PaperclipAgentSwitch(coordinator, company_device_id, agent)]

    add_agent_entities(entry, async_add_entities, _factory)


class PaperclipAgentSwitch(PaperclipAgentEntity, SwitchEntity):
    """On when the agent may run; off pauses it (and cancels active runs)."""

    def __init__(
        self, coordinator: PaperclipCoordinator, company_device_id: str, agent: Agent
    ) -> None:
        """Create the switch."""
        super().__init__(coordinator, company_device_id, agent, "enabled")

    @property
    @override
    def is_on(self) -> bool | None:
        agent = self.agent
        return None if agent is None else agent.status != "paused"

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_call("resume", self.coordinator.client.resume_agent)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_call("pause", self.coordinator.client.pause_agent)

    async def _async_call(
        self, action: str, call: Callable[[str], Awaitable[Agent]]
    ) -> None:
        try:
            await call(self.agent_id)
        except PaperclipError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=f"{action}_failed",
                translation_placeholders={"agent": self.agent_name, "error": str(err)},
            ) from err
        await self.coordinator.async_request_refresh()

    @property
    def agent_name(self) -> str:
        """Return the agent's name for messages."""
        return self.coordinator.agent_name(self.agent_id)
