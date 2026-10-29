"""Runtime data types for the Paperclip integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .api import PaperclipClient
    from .coordinator import PaperclipCoordinator

type PaperclipConfigEntry = ConfigEntry[PaperclipData]


@dataclass(slots=True)
class PaperclipData:
    """Objects shared by the platforms of one config entry."""

    client: PaperclipClient
    coordinator: PaperclipCoordinator
    company_device_id: str
