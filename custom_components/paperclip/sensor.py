"""Sensors for the Paperclip integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.util import dt as dt_util

from .const import AGENT_STATUSES
from .coordinator import PaperclipCoordinator, PaperclipState, shorten
from .entity import PaperclipAgentEntity, PaperclipCompanyEntity, add_agent_entities

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .api import Agent
    from .data import PaperclipConfigEntry

PARALLEL_UPDATES = 0
CURRENCY = "USD"  # Paperclip records costs in US cents.
NO_TASK = "none"
STATE_MAX_LENGTH = 255

type StateValue = str | int | float | datetime | None


def utc_month_start() -> datetime:
    """
    Return the start of the current UTC month.

    Paperclip's month-to-date spend restarts from zero at this moment, so it
    is the last_reset of the spend totals.
    """
    return dt_util.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True, kw_only=True)
class PaperclipSensorDescription(SensorEntityDescription):
    """Fields shared by every Paperclip sensor description."""

    last_reset_fn: Callable[[], datetime] | None = None


@dataclass(frozen=True, kw_only=True)
class CompanySensorDescription(PaperclipSensorDescription):
    """Describes a company-level sensor."""

    value_fn: Callable[[PaperclipState], StateValue]
    available_fn: Callable[[PaperclipState], bool] = lambda _: True


@dataclass(frozen=True, kw_only=True)
class AgentSensorDescription(PaperclipSensorDescription):
    """Describes a per-agent sensor."""

    value_fn: Callable[[Agent], StateValue]


def _pending_approvals(state: PaperclipState) -> int | None:
    if state.approvals is not None:
        return len(state.approvals)
    return state.dashboard.pending_approvals if state.dashboard else None


def _month_spend(state: PaperclipState) -> float:
    if state.dashboard is not None:
        return state.dashboard.month_spend_cents / 100
    return state.company.spent_monthly_cents / 100


COMPANY_SENSORS: tuple[CompanySensorDescription, ...] = (
    CompanySensorDescription(
        key="open_issues",
        translation_key="open_issues",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: state.dashboard.open_issues if state.dashboard else None,
        available_fn=lambda state: state.dashboard is not None,
    ),
    CompanySensorDescription(
        key="pending_approvals",
        translation_key="pending_approvals",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_pending_approvals,
        available_fn=lambda state: (
            state.approvals is not None or state.dashboard is not None
        ),
    ),
    CompanySensorDescription(
        key="active_runs",
        translation_key="active_runs",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: (
            len(state.live_runs) if state.live_runs is not None else None
        ),
        available_fn=lambda state: state.live_runs is not None,
    ),
    CompanySensorDescription(
        key="spend_this_month",
        translation_key="spend_this_month",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        last_reset_fn=utc_month_start,
        value_fn=_month_spend,
    ),
)

AGENT_SENSORS: tuple[AgentSensorDescription, ...] = (
    AgentSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=AGENT_STATUSES,
        value_fn=lambda agent: agent.status if agent.status in AGENT_STATUSES else None,
    ),
    AgentSensorDescription(
        key="agent_spend_this_month",
        translation_key="agent_spend_this_month",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        last_reset_fn=utc_month_start,
        value_fn=lambda agent: agent.spent_monthly_cents / 100,
    ),
    AgentSensorDescription(
        key="last_heartbeat",
        translation_key="last_heartbeat",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda agent: agent.last_heartbeat_at,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: PaperclipConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up company sensors and per-agent sensors."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        PaperclipCompanySensor(coordinator, description)
        for description in COMPANY_SENSORS
    )

    def _agent_entities(
        coordinator: PaperclipCoordinator, company_device_id: str, agent: Agent
    ) -> list[Entity]:
        entities: list[Entity] = [
            PaperclipAgentSensor(coordinator, company_device_id, agent, description)
            for description in AGENT_SENSORS
        ]
        entities.append(
            PaperclipCurrentTaskSensor(coordinator, company_device_id, agent)
        )
        return entities

    add_agent_entities(entry, async_add_entities, _agent_entities)


class PaperclipCompanySensor(PaperclipCompanyEntity, SensorEntity):
    """A company-level count or total."""

    entity_description: CompanySensorDescription

    def __init__(
        self, coordinator: PaperclipCoordinator, description: CompanySensorDescription
    ) -> None:
        """Create the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    @override
    def available(self) -> bool:
        return super().available and self.entity_description.available_fn(
            self.coordinator.data
        )

    @property
    @override
    def native_value(self) -> StateValue:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    @override
    def last_reset(self) -> datetime | None:
        last_reset_fn = self.entity_description.last_reset_fn
        return last_reset_fn() if last_reset_fn else None


class PaperclipAgentSensor(PaperclipAgentEntity, SensorEntity):
    """A per-agent value."""

    entity_description: AgentSensorDescription

    def __init__(
        self,
        coordinator: PaperclipCoordinator,
        company_device_id: str,
        agent: Agent,
        description: AgentSensorDescription,
    ) -> None:
        """Create the sensor."""
        super().__init__(coordinator, company_device_id, agent, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> StateValue:
        agent = self.agent
        return self.entity_description.value_fn(agent) if agent else None

    @property
    @override
    def last_reset(self) -> datetime | None:
        last_reset_fn = self.entity_description.last_reset_fn
        return last_reset_fn() if last_reset_fn else None


class PaperclipCurrentTaskSensor(PaperclipAgentEntity, SensorEntity):
    """The issue an agent's live run is working on, or "none"."""

    def __init__(
        self, coordinator: PaperclipCoordinator, company_device_id: str, agent: Agent
    ) -> None:
        """Create the sensor."""
        super().__init__(coordinator, company_device_id, agent, "current_task")

    @property
    @override
    def native_value(self) -> str:
        task = self.coordinator.data.current_tasks.get(self.agent_id)
        if task is None:
            return NO_TASK
        if task.identifier or task.title:
            text = " ".join(part for part in (task.identifier, task.title) if part)
            return shorten(text, STATE_MAX_LENGTH)
        return task.issue_id or task.run_status

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        task = self.coordinator.data.current_tasks.get(self.agent_id)
        if task is None:
            return {}
        ref = task.identifier or task.issue_id
        return {
            "run_id": task.run_id,
            "run_status": task.run_status,
            "issue_id": task.issue_id,
            "identifier": task.identifier,
            "url": self.coordinator.issue_url(ref) if ref else None,
        }
