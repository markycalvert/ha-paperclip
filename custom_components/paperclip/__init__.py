"""The Paperclip integration: AI-agent orchestration in Home Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    issue_registry as ir,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PaperclipClient
from .const import DOMAIN
from .coordinator import PaperclipCoordinator
from .data import PaperclipConfigEntry, PaperclipData
from .entity import agent_model, company_device_info
from .services import async_setup_services
from .tracker import IssueTracker

if TYPE_CHECKING:
    from homeassistant.helpers.typing import ConfigType

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CONVERSATION,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:  # noqa: ARG001
    """Register the integration's actions once, for every config entry."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PaperclipConfigEntry) -> bool:
    """Set up one Paperclip company from a config entry."""
    session = async_get_clientsession(
        hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, True)
    )
    client = PaperclipClient(
        session, entry.data[CONF_URL], entry.data.get(CONF_API_KEY)
    )
    tracker = IssueTracker(hass, entry.entry_id)
    coordinator = PaperclipCoordinator(hass, entry, client, tracker)
    await coordinator.async_config_entry_first_refresh()

    # The company device must exist before agent devices can point at it.
    company_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **company_device_info(coordinator)
    )
    entry.runtime_data = PaperclipData(
        client=client, coordinator=coordinator, company_device_id=company_device.id
    )

    @callback
    def _sync_devices() -> None:
        _async_sync_devices(hass, entry)

    entry.async_on_unload(coordinator.async_add_listener(_sync_devices))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _async_sync_devices(hass: HomeAssistant, entry: PaperclipConfigEntry) -> None:
    """Keep device details current and remove devices of deleted agents."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        ids = {value for domain, value in device.identifiers if domain == DOMAIN}
        if coordinator.company_id in ids:
            if (
                device.sw_version != data.health.version
                or device.name != data.company.name
            ):
                registry.async_update_device(
                    device.id, sw_version=data.health.version, name=data.company.name
                )
            continue
        agent = next((data.agents[i] for i in ids if i in data.agents), None)
        if agent is None:
            registry.async_remove_device(device.id)
        elif device.name != agent.name or device.model != agent_model(agent):
            registry.async_update_device(
                device.id, name=agent.name, model=agent_model(agent)
            )


async def async_unload_entry(hass: HomeAssistant, entry: PaperclipConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = entry.runtime_data.coordinator
        # Reply waits still running in an action or an Assist turn belong to
        # this coordinator: end them, so that after a reload they cannot fire
        # replies a second time or save over the new entry's tracking data.
        coordinator.async_stop_waits()
        await coordinator.tracker.async_close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: PaperclipConfigEntry) -> None:
    """Delete stored tracking data and repair issues of a removed entry."""
    await IssueTracker(hass, entry.entry_id).async_remove()
    ir.async_delete_issue(hass, DOMAIN, f"outdated_paperclip_{entry.entry_id}")


async def async_remove_config_entry_device(
    hass: HomeAssistant,  # noqa: ARG001
    entry: PaperclipConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removing a device only when Paperclip no longer has it."""
    coordinator = entry.runtime_data.coordinator
    live = {coordinator.company_id, *coordinator.data.agents}
    return not any(
        domain == DOMAIN and value in live for domain, value in device_entry.identifiers
    )
