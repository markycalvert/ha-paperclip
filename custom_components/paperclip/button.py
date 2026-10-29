"""Wake buttons for Paperclip agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from .api import Agent, PaperclipError
from .const import DOMAIN
from .entity import PaperclipAgentEntity, add_agent_entities

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import PaperclipCoordinator
    from .data import PaperclipConfigEntry

PARALLEL_UPDATES = 1
WAKE_REASON = "Requested from Home Assistant"


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: PaperclipConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one wake button per agent."""

    def _factory(
        coordinator: PaperclipCoordinator, company_device_id: str, agent: Agent
    ) -> list[Entity]:
        return [PaperclipWakeButton(coordinator, company_device_id, agent)]

    add_agent_entities(entry, async_add_entities, _factory)


class PaperclipWakeButton(PaperclipAgentEntity, ButtonEntity):
    """Start an on-demand heartbeat for the agent."""

    def __init__(
        self, coordinator: PaperclipCoordinator, company_device_id: str, agent: Agent
    ) -> None:
        """Create the button."""
        super().__init__(coordinator, company_device_id, agent, "wake")

    @override
    async def async_press(self) -> None:
        try:
            await self.coordinator.client.wake_agent(self.agent_id, reason=WAKE_REASON)
        except PaperclipError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="wake_failed",
                translation_placeholders={
                    "agent": self.coordinator.agent_name(self.agent_id),
                    "error": str(err),
                },
            ) from err
        await self.coordinator.async_request_refresh()
