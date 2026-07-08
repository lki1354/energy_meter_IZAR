"""DataUpdateCoordinator polling the gateway's file server."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DELETE_AFTER,
    CONF_DIRECTORY,
    CONF_FILE_PATTERN,
    CONF_POLL_INTERVAL,
    CONF_PROTOCOL,
    CONF_REQUIRE_RDY,
    DEFAULT_DELETE_AFTER,
    DEFAULT_FILE_PATTERN,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DEFAULT_REQUIRE_RDY,
    DOMAIN,
    STORAGE_KEY_TEMPLATE,
    STORAGE_VERSION,
)
from .ftp_client import ConnectionConfig, FetchAuthError, FetchError, create_client
from .ingest import IngestionTracker, SequenceWatcher
from .pipeline import PollResult, SnapshotPipeline

_LOGGER = logging.getLogger(__name__)

type IzarConfigEntry = ConfigEntry[IzarCoordinator]


class IzarCoordinator(DataUpdateCoordinator[PollResult]):
    """Poll the FTP/SFTP server and maintain the latest meter state."""

    config_entry: IzarConfigEntry

    def __init__(self, hass: HomeAssistant, entry: IzarConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_HOST]}",
            update_interval=timedelta(
                minutes=entry.options.get(
                    CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_MINUTES
                )
            ),
        )
        self._store: Store = Store(
            hass, STORAGE_VERSION, STORAGE_KEY_TEMPLATE.format(entry_id=entry.entry_id)
        )
        self._pipeline: SnapshotPipeline | None = None

    @property
    def connection_config(self) -> ConnectionConfig:
        entry = self.config_entry
        return ConnectionConfig(
            protocol=entry.data[CONF_PROTOCOL],
            host=entry.data[CONF_HOST],
            port=entry.data[CONF_PORT],
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            directory=entry.data[CONF_DIRECTORY],
        )

    async def _async_setup(self) -> None:
        """Restore ingestion bookkeeping from disk before the first poll."""
        options = self.config_entry.options
        stored = await self._store.async_load() or {}
        self._pipeline = SnapshotPipeline(
            tracker=IngestionTracker.from_dict(stored.get("tracker", {})),
            watcher=SequenceWatcher.from_dict(stored.get("watcher", {})),
            file_pattern=options.get(CONF_FILE_PATTERN, DEFAULT_FILE_PATTERN),
            require_rdy=options.get(CONF_REQUIRE_RDY, DEFAULT_REQUIRE_RDY),
            delete_after=options.get(CONF_DELETE_AFTER, DEFAULT_DELETE_AFTER),
        )

    async def _async_update_data(self) -> PollResult:
        assert self._pipeline is not None
        client = create_client(self.connection_config)
        try:
            await client.connect()
            result = await self._pipeline.poll(client)
        except FetchAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except FetchError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            await client.close()

        for warning in result.warnings:
            _LOGGER.warning("%s", warning)
        if result.files_ingested:
            _LOGGER.debug(
                "ingested %d file(s): %s",
                len(result.files_ingested),
                ", ".join(result.files_ingested),
            )
            await self._store.async_save(
                {
                    "tracker": self._pipeline.tracker.to_dict(),
                    "watcher": self._pipeline.watcher.to_dict(),
                }
            )
        return result
