"""Backfill long-term external statistics from the reading store.

Snapshot files arrive in batches carrying past timestamps and HA sensor
states cannot be backdated, so the cumulative meter counters are additionally
imported as external statistics (``energy_meter_izar:<device>_<key>``, the
``opower`` pattern). The Energy Dashboard then shows historically correct
hourly data even when files arrive hours or days late.

The bucketing/sum math is pure (unit-testable without HA); only
:class:`StatisticsImporter` talks to the recorder.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .const import DOMAIN
from .mbus_parser import MeterDefinition, MeterReading, TelegramLayout
from .store import ReadingStore

type HourlyPoint = tuple[dt.datetime, float]


@dataclass(frozen=True)
class StatisticSpec:
    """Maps one cumulative decoded quantity onto an external statistic."""

    key: str  # statistic id suffix, matches the sensor unique_id suffix
    quantity: str  # decoded quantity name in the reading store
    unit: str
    unit_class: str
    scale: float = 1.0  # store value → statistic unit


def _energy(quantity: str) -> StatisticSpec:
    # readings are Wh; statistics use kWh like the sensors' display unit
    return StatisticSpec(
        "energy", quantity, UnitOfEnergy.KILO_WATT_HOUR, EnergyConverter.UNIT_CLASS, 0.001
    )


def _volume(quantity: str) -> StatisticSpec:
    return StatisticSpec(
        "volume", quantity, UnitOfVolume.CUBIC_METERS, VolumeConverter.UNIT_CLASS
    )


_RESERVOIR_STATISTICS = (_energy("energy (Wh)"), _volume("volume (m^3)"))

LAYOUT_STATISTICS: dict[TelegramLayout, tuple[StatisticSpec, ...]] = {
    TelegramLayout.ELECTRICITY: (_energy("Energie 1 (Wh)"),),
    TelegramLayout.HEAT: (_energy("heat_energy_1 (Wh)"), _volume("heat_volume_1 (m^3)")),
    TelegramLayout.WATER: (_volume("water_1 (m^3)"),),
    TelegramLayout.RESERVOIR: _RESERVOIR_STATISTICS,
    TelegramLayout.RESERVOIR_WW: _RESERVOIR_STATISTICS,
}


def statistic_id(device_number: int, spec_key: str) -> str:
    return f"{DOMAIN}:{device_number}_{spec_key}"


def hourly_last(points: Iterable[HourlyPoint]) -> list[HourlyPoint]:
    """Reduce a time-ordered series to the last value of each UTC hour.

    Long-term statistics have hourly resolution; for a cumulative counter the
    value at the end of the hour is the correct hourly state.
    """
    buckets: dict[dt.datetime, float] = {}
    for timestamp, value in points:
        buckets[timestamp.replace(minute=0, second=0, microsecond=0)] = value
    return [(hour, buckets[hour]) for hour in sorted(buckets)]


def cumulative_sums(
    buckets: Iterable[HourlyPoint],
    *,
    offset: float = 0.0,
    previous_state: float | None = None,
) -> list[StatisticData]:
    """Turn hourly counter states into statistics rows with reset-safe sums.

    ``sum`` must never decrease; when the counter drops (meter reset /
    replacement) the last value before the drop is folded into ``offset`` so
    consumption keeps accumulating. Seed ``offset``/``previous_state`` from
    the last imported statistic to continue an existing series.
    """
    rows: list[StatisticData] = []
    for start, state in buckets:
        if previous_state is not None and state < previous_state:
            offset += previous_state
        rows.append(StatisticData(start=start, state=state, sum=state + offset))
        previous_state = state
    return rows


def _supports_unit_class() -> bool:
    # unit_class was added to StatisticMetaData after 2025.6 (our minimum
    # supported HA) and becomes mandatory in 2026.11.
    return "unit_class" in StatisticMetaData.__annotations__


class StatisticsImporter:
    """Imports reading-store series as external statistics after each poll."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: ReadingStore,
        device_map: dict[int, MeterDefinition],
    ) -> None:
        self._hass = hass
        self._store = store
        self._device_map = device_map

    async def async_import_new(self, new_readings: list[MeterReading]) -> None:
        """Refresh every statistic that the given new readings touch."""
        earliest: dict[tuple[int, StatisticSpec], dt.datetime] = {}
        for reading in new_readings:
            definition = self._device_map.get(reading.device_number)
            if definition is None:
                continue
            for spec in LAYOUT_STATISTICS.get(definition.layout, ()):
                if spec.quantity != reading.quantity:
                    continue
                key = (reading.device_number, spec)
                if key not in earliest or reading.timestamp < earliest[key]:
                    earliest[key] = reading.timestamp

        for (device_number, spec), first_new in earliest.items():
            await self._async_import_one(device_number, spec, first_new)

    async def _async_import_one(
        self, device_number: int, spec: StatisticSpec, first_new: dt.datetime
    ) -> None:
        stat_id = statistic_id(device_number, spec.key)
        local_tz = dt_util.get_default_time_zone()
        first_new_hour = (
            first_new.replace(tzinfo=local_tz)
            .astimezone(dt.UTC)
            .replace(minute=0, second=0, microsecond=0)
        )

        last_rows = await get_instance(self._hass).async_add_executor_job(
            get_last_statistics, self._hass, 1, stat_id, False, {"state", "sum"}
        )
        rows_for_id = last_rows.get(stat_id)
        last = rows_for_id[0] if rows_for_id else None

        # Continue the imported series when the new data is at or past its
        # tip (re-importing the tip hour updates it in place). New data
        # *older* than the tip means a late/out-of-order file: recompute the
        # whole series from the store so every following sum stays
        # consistent — async_add_external_statistics overwrites row-by-row.
        offset = 0.0
        query_start: dt.datetime | None = None
        if (
            last is not None
            and last.get("state") is not None
            and last.get("sum") is not None
        ):
            last_start = dt_util.utc_from_timestamp(last["start"])
            if first_new_hour >= last_start:
                offset = last["sum"] - last["state"]
                query_start = last_start.astimezone(local_tz).replace(tzinfo=None)

        series = await self._hass.async_add_executor_job(
            lambda: self._store.series(device_number, spec.quantity, start=query_start)
        )
        points = [
            (timestamp.replace(tzinfo=local_tz).astimezone(dt.UTC), value * spec.scale)
            for timestamp, value in series
        ]
        rows = cumulative_sums(hourly_last(points), offset=offset)
        if not rows:
            return

        definition = self._device_map[device_number]
        meter_name = f"{definition.medium.replace('_', ' ').title()} {definition.location}"
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{meter_name} {spec.key}",
            source=DOMAIN,
            statistic_id=stat_id,
            unit_of_measurement=spec.unit,
        )
        if _supports_unit_class():
            metadata["unit_class"] = spec.unit_class
        async_add_external_statistics(self._hass, metadata, rows)
