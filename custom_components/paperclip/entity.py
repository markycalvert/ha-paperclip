"""Base entities for the Paperclip integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import AGENT_ROLE_LABELS, DOMAIN, MANUFACTURER
from .coordinator import PaperclipCoordinator

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.helpers.entity import Entity

    from .api import Agent
    from .data import PaperclipConfigEntry


def company_device_info(coordinator: PaperclipCoordinator) -> DeviceInfo:
    """Return the service device that represents the company."""
    data = coordinator.data
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.company_id)},
        name=data.company.name if data else coordinator.config_entry.title,
        manufacturer=MANUFACTURER,
        model="Company",
        sw_version=data.health.version if data else None,
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=coordinator.dashboard_url(),
    )


def agent_model(agent: Agent) -> str:
    """Return a human model string from the agent's title or role."""
    if agent.title:
        return agent.title
    if agent.role:
        return AGENT_ROLE_LABELS.get(agent.role, agent.role.replace("_", " ").title())
    return "Agent"


class PaperclipCompanyEntity(CoordinatorEntity[PaperclipCoordinator]):
    """An entity on the company device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PaperclipCoordinator, key: str) -> None:
        """Set up the unique id and device."""
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.company_id}_{key}"
        self._attr_device_info = company_device_info(coordinator)


class PaperclipAgentEntity(CoordinatorEntity[PaperclipCoordinator]):
    """An entity on one agent's device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PaperclipCoordinator,
        company_device_id: str,
        agent: Agent,
        key: str | None,
    ) -> None:
        """Set up the unique id and the agent device (via the company)."""
        super().__init__(coordinator)
        self.agent_id = agent.id
        if key is not None:
            self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.company_id}_{agent.id}_{key or 'agent'}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, agent.id)},
            name=agent.name,
            manufacturer=MANUFACTURER,
            model=agent_model(agent),
            configuration_url=coordinator.agent_url(agent),
            via_device_id=company_device_id,
        )

    @property
    def agent(self) -> Agent | None:
        """Return the agent from the latest poll, if it still exists."""
        return self.coordinator.data.agents.get(self.agent_id)

    @property
    @override
    def available(self) -> bool:
        return super().available and self.agent is not None


def add_agent_entities(
    entry: PaperclipConfigEntry,
    add_entities: Callable[[list[Entity]], None],
    factory: Callable[[PaperclipCoordinator, str, Agent], list[Entity]],
) -> None:
    """Add entities for every agent now, and for agents that appear later."""
    coordinator = entry.runtime_data.coordinator
    company_device_id = entry.runtime_data.company_device_id
    known: set[str] = set()

    @callback
    def _add_new_agents() -> None:
        # Forget agents that vanished, so they get entities again if they return.
        known.intersection_update(coordinator.data.agents)
        new = [
            agent
            for agent_id, agent in coordinator.data.agents.items()
            if agent_id not in known
        ]
        if not new:
            return
        entities: list[Entity] = []
        for agent in new:
            known.add(agent.id)
            entities.extend(factory(coordinator, company_device_id, agent))
        add_entities(entities)

    _add_new_agents()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_agents))
