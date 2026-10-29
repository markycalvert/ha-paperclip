"""Binary sensors for the Paperclip integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.const import EntityCategory

from .entity import PaperclipCompanyEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import PaperclipCoordinator
    from .data import PaperclipConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: PaperclipConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the server connectivity sensor."""
    async_add_entities([PaperclipConnectivitySensor(entry.runtime_data.coordinator)])


class PaperclipConnectivitySensor(PaperclipCompanyEntity, BinarySensorEntity):
    """On while the last poll reached a healthy Paperclip server."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PaperclipCoordinator) -> None:
        """Create the sensor."""
        super().__init__(coordinator, "server")

    @property
    @override
    def available(self) -> bool:
        # A connectivity sensor reports "off" rather than "unavailable" when
        # the server cannot be reached.
        return True

    @property
    @override
    def is_on(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self.coordinator.data.health.status == "ok"
        )
