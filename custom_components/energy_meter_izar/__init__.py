"""Energy Meter IZAR — M-Bus gateway (HC2XML over FTP/SFTP) integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import IzarConfigEntry, IzarCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: IzarConfigEntry) -> bool:
    """Set up the gateway poller from a config entry."""
    coordinator = IzarCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IzarConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: IzarConfigEntry) -> None:
    """Reload the entry when options (poll interval, pattern, …) change."""
    await hass.config_entries.async_reload(entry.entry_id)
