"""Energy Meter IZAR — M-Bus gateway (HC2XML over FTP/SFTP) integration."""

from __future__ import annotations

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import IzarConfigEntry, IzarCoordinator
from .services import async_setup_services

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the generate_bill service (available without a loaded entry)."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: IzarConfigEntry) -> bool:
    """Set up the gateway poller from a config entry."""
    coordinator = IzarCoordinator(hass, entry)
    await coordinator.async_initialize()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    # The first poll may chew through a large file backlog and take minutes;
    # run it outside the setup so a restart or reload cannot cancel the entry
    # mid-download and Home Assistant startup is not delayed.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN} first poll"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IzarConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_close()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Purge this integration's external statistics when the entry is removed.

    The readings.db archive is deliberately left on disk — it is the user's
    billing history and can be deleted manually.
    """
    if "recorder" not in hass.config.components:
        return
    instance = get_instance(hass)
    known = await instance.async_add_executor_job(list_statistic_ids, hass)
    our_ids = [
        meta["statistic_id"]
        for meta in known
        if meta["statistic_id"].startswith(f"{DOMAIN}:")
    ]
    if our_ids:
        instance.async_clear_statistics(our_ids)


async def _async_options_updated(hass: HomeAssistant, entry: IzarConfigEntry) -> None:
    """Reload the entry when options (poll interval, pattern, …) change."""
    await hass.config_entries.async_reload(entry.entry_id)
