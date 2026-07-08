"""Poll pipeline: remote file listing → download → parse → latest meter state.

Pure Python, no Home Assistant imports — the coordinator wraps one
:class:`SnapshotPipeline` instance and persists its tracker/watcher state.

Ordering and exactly-once rules follow ``XML_DATETIME_FIX_STRATEGY.md`` §5:
files are ordered and gated by their decoded gateway ``MBTIME``; the 3-digit
filename counter is only used (modulo 1000) to report gaps.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import logging
from dataclasses import dataclass, field

from .ftp_client import RemoteClient, RemoteFileInfo
from .ingest import IngestionTracker, SequenceWatcher, filename_sequence
from .mbus_parser import (
    DEFAULT_DEVICE_MAP,
    GatewayInfo,
    MeterDefinition,
    MeterReading,
    parse_snapshot_xml,
)

_LOGGER = logging.getLogger(__name__)

#: Upper bound of files ingested in one poll, so a first run against a large
#: backlog cannot stall the event loop indefinitely; the rest is picked up on
#: subsequent polls (ordering by gateway MBTIME makes this safe).
MAX_FILES_PER_POLL = 200


@dataclass
class MeterState:
    """Latest decoded reading per quantity of one physical meter."""

    definition: MeterDefinition
    readings: dict[str, MeterReading] = field(default_factory=dict)

    @property
    def last_seen(self) -> dt.datetime | None:
        if not self.readings:
            return None
        return max(reading.timestamp for reading in self.readings.values())


@dataclass
class PollResult:
    """Outcome of one poll cycle."""

    meters: dict[int, MeterState]
    gateway: GatewayInfo | None
    last_readout_time: dt.datetime | None
    files_ingested: list[str] = field(default_factory=list)
    files_missing: int = 0
    warnings: list[str] = field(default_factory=list)
    #: Every reading decoded from the files ingested in this cycle (not just
    #: the latest per quantity) — the coordinator archives these in the
    #: reading store and derives statistics from them.
    new_readings: list[MeterReading] = field(default_factory=list)


class SnapshotPipeline:
    """Stateful ingestion pipeline shared across poll cycles.

    ``meters`` accumulates the latest reading per (device, quantity) over the
    lifetime of the pipeline; ``tracker``/``watcher`` carry the exactly-once
    bookkeeping and are meant to be persisted between restarts by the caller.
    """

    def __init__(
        self,
        *,
        tracker: IngestionTracker | None = None,
        watcher: SequenceWatcher | None = None,
        device_map: dict[int, MeterDefinition] | None = None,
        file_pattern: str = "*.xml",
        require_rdy: bool = True,
        delete_after: bool = False,
    ) -> None:
        self.tracker = tracker or IngestionTracker()
        self.watcher = watcher or SequenceWatcher()
        self.device_map = device_map if device_map is not None else DEFAULT_DEVICE_MAP
        self.file_pattern = file_pattern
        self.require_rdy = require_rdy
        self.delete_after = delete_after
        self.meters: dict[int, MeterState] = {}
        self.gateway: GatewayInfo | None = None
        #: Files whose content could not be used (unparsable, no gateway
        #: MBTIME). Remembered per runtime so each poll does not re-download
        #: and re-warn about the same broken file.
        self._rejected: dict[str, tuple[float | None, int | None]] = {}

    async def poll(self, client: RemoteClient) -> PollResult:
        """Run one poll cycle against an already-connected client."""
        listing = await client.list_files()
        names = {info.name for info in listing}
        result = PollResult(
            meters=self.meters,
            gateway=self.gateway,
            last_readout_time=self.tracker.last_readout_time,
        )

        candidates = self._select_candidates(listing, names, result)
        pending = await self._download_new(client, candidates, result)

        # Authoritative order: decoded gateway MBTIME, never filename counter.
        pending.sort(key=lambda item: item[0])
        for gateway_time, info, parse in pending:
            self._observe_sequence(info.name, result)
            self._apply_readings(parse.readings)
            result.new_readings.extend(parse.readings)
            for warning in parse.warnings:
                result.warnings.append(f"{info.name}: {warning}")
            self.tracker.mark_ingested(
                info.name, gateway_time, mtime=info.mtime, size=info.size
            )
            self.gateway = parse.gateway
            result.files_ingested.append(info.name)
            if self.delete_after:
                await client.delete(info.name)
                rdy_name = f"{info.name.rsplit('.', 1)[0]}.rdy"
                if rdy_name in names:
                    await client.delete(rdy_name)

        result.gateway = self.gateway
        result.last_readout_time = self.tracker.last_readout_time
        return result

    def _select_candidates(
        self,
        listing: list[RemoteFileInfo],
        names: set[str],
        result: PollResult,
    ) -> list[RemoteFileInfo]:
        candidates: list[RemoteFileInfo] = []
        for info in listing:
            if not fnmatch.fnmatch(info.name, self.file_pattern):
                continue
            if self.require_rdy:
                stem = info.name.rsplit(".", 1)[0]
                if f"{stem}.rdy" not in names:
                    _LOGGER.debug("skipping %s: no .rdy marker yet", info.name)
                    continue
            candidates.append(info)
        return candidates

    async def _download_new(
        self,
        client: RemoteClient,
        candidates: list[RemoteFileInfo],
        result: PollResult,
    ) -> list[tuple[dt.datetime, RemoteFileInfo, object]]:
        pending = []
        for info in candidates:
            if len(pending) >= MAX_FILES_PER_POLL:
                result.warnings.append(
                    f"more than {MAX_FILES_PER_POLL} new files; "
                    "deferring the rest to the next poll"
                )
                break
            if self._rejected.get(info.name) == (info.mtime, info.size):
                continue
            if not self.tracker.needs_download(info.name, mtime=info.mtime, size=info.size):
                continue

            raw = await client.download(info.name)
            try:
                parse = parse_snapshot_xml(raw, self.device_map)
            except Exception as err:  # noqa: BLE001 - malformed remote data
                self._rejected[info.name] = (info.mtime, info.size)
                result.warnings.append(f"{info.name}: unparsable snapshot: {err}")
                continue

            gateway_time = parse.gateway.mbtime
            if gateway_time is None:
                self._rejected[info.name] = (info.mtime, info.size)
                result.warnings.append(
                    f"{info.name}: no decodable gateway MBTIME; cannot order file, skipping"
                )
                continue

            if not self.tracker.is_new_content(info.name, gateway_time):
                # Same snapshot as already ingested — refresh the metadata so
                # the cheap pre-filter skips it next time without downloading.
                self.tracker.mark_ingested(
                    info.name, gateway_time, mtime=info.mtime, size=info.size
                )
                continue

            pending.append((gateway_time, info, parse))
        return pending

    def _observe_sequence(self, filename: str, result: PollResult) -> None:
        sequence = filename_sequence(filename)
        if sequence is None:
            return
        observation = self.watcher.observe(sequence)
        if observation.missing:
            result.files_missing += observation.missing
            result.warnings.append(
                f"{observation.missing} file(s) missed before {filename}"
                + (" (counter wrapped)" if observation.wrapped else "")
            )

    def _apply_readings(self, readings: list[MeterReading]) -> None:
        for reading in readings:
            state = self.meters.get(reading.device_number)
            if state is None:
                state = MeterState(definition=self.device_map[reading.device_number])
                self.meters[reading.device_number] = state
            current = state.readings.get(reading.quantity)
            if current is None or reading.timestamp >= current.timestamp:
                state.readings[reading.quantity] = reading
