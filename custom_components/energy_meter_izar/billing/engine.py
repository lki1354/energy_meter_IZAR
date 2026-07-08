"""Billing engine: tariff assignment, PV allocation, and cost splits.

Port of ``meter_data_analyse/energy_bill_analysis.py`` with the notebook's
semantics kept intact so results are comparable to the manual bills:

* Consumption is the diff of the cumulative counter per device; negative
  diffs (meter resets) are dropped, and the first reading inside the period
  only serves as the baseline for the next one.
* Per interval timestamp, ``pv_factor = min(PV, house) / house`` splits every
  consumer into a grid share and a PV share.
* Grid energy is priced by the tariff matching the interval timestamp,
  PV energy by the flat PV price.
* Common electricity is split equally across units; heat-pump cost is split
  heating vs. hot water by the reservoir energy ratio (fallback share when
  no reservoir data), then allocated proportionally to each unit's heat
  meter (Wh) respectively hot-water meter (m³).

Pure Python — no Home Assistant imports. Readings come from any object with
a ``ReadingStore.series``-compatible method.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

from .config import (
    SECTION_ELECTRICITY,
    SECTION_HEATING,
    SECTION_HOT_WATER,
    BillingConfig,
    Tariff,
)

# Quantity names as stored by the parser (identical to the notebook columns).
QUANTITY_ELECTRICITY_ENERGY = "Energie 1 (Wh)"
QUANTITY_HEAT_ENERGY = "heat_energy_1 (Wh)"
QUANTITY_WATER_VOLUME = "water_1 (m^3)"
QUANTITY_RESERVOIR_ENERGY = "energy (Wh)"

# Line-item kinds (renderers map these to translated labels).
KIND_GRID = "grid"
KIND_PV = "pv"
KIND_COMMON_GRID = "common_grid"
KIND_COMMON_PV = "common_pv"
KIND_HEATING = "heating"
KIND_HOT_WATER = "hot_water"


class SeriesProvider(Protocol):
    """Anything that can return one meter quantity's time series.

    ``ReadingStore.series`` satisfies this; timestamps are naive local,
    ``start`` inclusive, ``end`` exclusive, ordered by time.
    """

    def series(
        self,
        device_number: int,
        quantity: str,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> list:  # list of (timestamp, value) tuples / NamedTuples
        ...


@dataclass(frozen=True)
class LineItem:
    """One billed position of one unit."""

    section: str  # electricity | heating | hot_water
    kind: str  # KIND_* constant
    detail: str | None  # tariff name for grid lines, else None
    quantity: float
    quantity_unit: str
    price: float | None  # per-unit price; None = mixed tariffs
    cost: float
    #: The unit's own meter reading behind an allocated position (heat meter
    #: kWh, hot-water m³); None where the quantity itself is the measurement.
    measured: float | None = None
    measured_unit: str | None = None


@dataclass
class UnitBill:
    name: str
    lines: list[LineItem] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(line.cost for line in self.lines)

    def section_total(self, section: str) -> float:
        return sum(line.cost for line in self.lines if line.section == section)


@dataclass(frozen=True)
class WaterVolume:
    """Plain consumption volumes for the water_volume section."""

    hot_m3: float
    cold_m3: float


@dataclass
class BillMeta:
    """Auditing context embedded into every rendered bill."""

    intervals: int = 0
    house_consumption_kwh: float = 0.0
    pv_production_kwh: float = 0.0
    pv_self_consumed_kwh: float = 0.0
    pv_exported_kwh: float = 0.0
    heating_share: float | None = None
    heating_share_source: str | None = None  # "reservoir_ratio" | "fallback"
    heat_pump_kwh: float = 0.0
    heat_pump_cost: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class BillResult:
    """Structured bill output consumed by the renderers."""

    start: dt.datetime
    end: dt.datetime
    currency: str
    tariff_prices: dict[str, float]  # grid tariff name → price/kWh
    pv_price_kwh: float | None
    units: dict[str, UnitBill]
    water: dict[str, WaterVolume]
    meta: BillMeta
    generated_at: dt.datetime = field(default_factory=dt.datetime.now)

    @property
    def total(self) -> float:
        return sum(unit.total for unit in self.units.values())


def assign_tariff(config: BillingConfig, timestamp: dt.datetime) -> Tariff:
    """First tariff whose schedule matches; otherwise the default tariff."""
    for tariff in config.tariffs:
        if any(window.matches(timestamp) for window in tariff.windows):
            return tariff
    return config.default_tariff


def _consumption(points: list) -> dict[dt.datetime, float]:
    """Positive diffs of a cumulative counter, keyed by interval-end time.

    Negative diffs (meter resets) are dropped; counting continues from the
    new, lower baseline — exactly like the notebook's ``diff() > 0`` filter.
    """
    diffs: dict[dt.datetime, float] = {}
    previous: float | None = None
    for timestamp, value in points:
        if previous is not None:
            diff = value - previous
            if diff > 0:
                diffs[timestamp] = diffs.get(timestamp, 0.0) + diff
        previous = value
    return diffs


class _ElectricityAccount:
    """Grid Wh per tariff plus PV Wh accumulated for one consumer."""

    def __init__(self) -> None:
        self.grid_wh: defaultdict[str, float] = defaultdict(float)
        self.pv_wh = 0.0

    def grid_kwh(self, tariff: str) -> float:
        return self.grid_wh[tariff] / 1000

    @property
    def total_grid_kwh(self) -> float:
        return sum(self.grid_wh.values()) / 1000

    @property
    def pv_kwh(self) -> float:
        return self.pv_wh / 1000

    def grid_cost(self, prices: dict[str, float]) -> float:
        return sum(wh / 1000 * prices[name] for name, wh in self.grid_wh.items())

    def pv_cost(self, pv_price: float | None) -> float:
        return self.pv_kwh * pv_price if pv_price is not None else 0.0

    def total_cost(self, prices: dict[str, float], pv_price: float | None) -> float:
        return self.grid_cost(prices) + self.pv_cost(pv_price)

    @property
    def total_kwh(self) -> float:
        return self.total_grid_kwh + self.pv_kwh


_COMMON = "__common__"
_HEAT_PUMP = "__heat_pump__"


def generate_bill(
    store: SeriesProvider,
    config: BillingConfig,
    start: dt.datetime,
    end: dt.datetime,
) -> BillResult:
    """Compute the bill for ``[start, end)`` from stored readings."""
    if end <= start:
        raise ValueError("billing period end must be after start")

    meta = BillMeta()
    prices = {tariff.name: tariff.price_kwh for tariff in config.tariffs}

    accounts = _allocate_electricity(store, config, start, end, meta)
    units = {name: UnitBill(name=name) for name in config.units}

    _add_electricity_lines(config, accounts, units, prices, meta)
    _add_heat_pump_lines(store, config, accounts, units, prices, start, end, meta)
    water = _water_volumes(store, config, start, end)

    if meta.intervals == 0:
        meta.notes.append("no readings found in the billing period")

    return BillResult(
        start=start,
        end=end,
        currency=config.currency,
        tariff_prices=prices,
        pv_price_kwh=config.pv_price_kwh,
        units=units,
        water=water,
        meta=meta,
    )


def _allocate_electricity(
    store: SeriesProvider,
    config: BillingConfig,
    start: dt.datetime,
    end: dt.datetime,
    meta: BillMeta,
) -> dict[str, _ElectricityAccount]:
    """Per-interval PV split and tariff assignment for every consumer."""
    consumers: dict[str, dict[dt.datetime, float]] = {}
    for name, unit in config.units.items():
        if unit.electricity is not None:
            consumers[name] = _consumption(
                store.series(
                    unit.electricity, QUANTITY_ELECTRICITY_ENERGY, start=start, end=end
                )
            )
    if config.common_electricity is not None:
        consumers[_COMMON] = _consumption(
            store.series(
                config.common_electricity.device,
                QUANTITY_ELECTRICITY_ENERGY,
                start=start,
                end=end,
            )
        )
    if config.heat_pump is not None:
        consumers[_HEAT_PUMP] = _consumption(
            store.series(
                config.heat_pump.device, QUANTITY_ELECTRICITY_ENERGY, start=start, end=end
            )
        )
    pv_production: dict[dt.datetime, float] = {}
    if config.photovoltaic is not None:
        pv_production = _consumption(
            store.series(
                config.photovoltaic.device,
                QUANTITY_ELECTRICITY_ENERGY,
                start=start,
                end=end,
            )
        )

    accounts = {name: _ElectricityAccount() for name in consumers}
    timestamps = set().union(*consumers.values(), pv_production) if consumers else set()

    for timestamp in sorted(timestamps):
        interval = {name: series.get(timestamp, 0.0) for name, series in consumers.items()}
        house = sum(interval.values())
        pv = pv_production.get(timestamp, 0.0)
        # pv_factor = min(PV, house) / house — share of this interval's
        # consumption covered by own production (notebook semantics).
        pv_factor = pv / house if house > pv else 1.0
        tariff = assign_tariff(config, timestamp).name

        for name, consumed_wh in interval.items():
            accounts[name].grid_wh[tariff] += consumed_wh * (1 - pv_factor)
            accounts[name].pv_wh += consumed_wh * pv_factor

        meta.intervals += 1
        meta.house_consumption_kwh += house / 1000
        meta.pv_production_kwh += pv / 1000
        meta.pv_self_consumed_kwh += min(pv, house) / 1000
        meta.pv_exported_kwh += max(pv - house, 0.0) / 1000

    return accounts


def _add_electricity_lines(
    config: BillingConfig,
    accounts: dict[str, _ElectricityAccount],
    units: dict[str, UnitBill],
    prices: dict[str, float],
    meta: BillMeta,
) -> None:
    common = accounts.get(_COMMON)
    unit_count = len(config.units)

    for name, bill in units.items():
        account = accounts.get(name)
        if account is None:
            continue
        for tariff in config.tariffs:
            kwh = account.grid_kwh(tariff.name)
            bill.lines.append(
                LineItem(
                    section=SECTION_ELECTRICITY,
                    kind=KIND_GRID,
                    detail=tariff.name,
                    quantity=kwh,
                    quantity_unit="kWh",
                    price=tariff.price_kwh,
                    cost=kwh * tariff.price_kwh,
                )
            )
        if config.photovoltaic is not None:
            bill.lines.append(
                LineItem(
                    section=SECTION_ELECTRICITY,
                    kind=KIND_PV,
                    detail=None,
                    quantity=account.pv_kwh,
                    quantity_unit="kWh",
                    price=config.pv_price_kwh,
                    cost=account.pv_cost(config.pv_price_kwh),
                )
            )
        if common is not None:
            # Equal split: each unit carries 1/N of the common meter's grid
            # cost (mixed tariffs → no single price) and PV cost.
            bill.lines.append(
                LineItem(
                    section=SECTION_ELECTRICITY,
                    kind=KIND_COMMON_GRID,
                    detail=None,
                    quantity=common.total_grid_kwh / unit_count,
                    quantity_unit="kWh",
                    price=None,
                    cost=common.grid_cost(prices) / unit_count,
                )
            )
            if config.photovoltaic is not None:
                bill.lines.append(
                    LineItem(
                        section=SECTION_ELECTRICITY,
                        kind=KIND_COMMON_PV,
                        detail=None,
                        quantity=common.pv_kwh / unit_count,
                        quantity_unit="kWh",
                        price=config.pv_price_kwh,
                        cost=common.pv_cost(config.pv_price_kwh) / unit_count,
                    )
                )


def _heating_share(
    store: SeriesProvider,
    config: BillingConfig,
    start: dt.datetime,
    end: dt.datetime,
    meta: BillMeta,
) -> float:
    """Heating vs. hot-water share of the heat-pump cost (VEWA split)."""
    heat_pump = config.heat_pump
    assert heat_pump is not None
    reservoir_heating_wh = reservoir_ww_wh = 0.0
    if heat_pump.reservoir_heating is not None:
        reservoir_heating_wh = sum(
            _consumption(
                store.series(
                    heat_pump.reservoir_heating,
                    QUANTITY_RESERVOIR_ENERGY,
                    start=start,
                    end=end,
                )
            ).values()
        )
    if heat_pump.reservoir_hot_water is not None:
        reservoir_ww_wh = sum(
            _consumption(
                store.series(
                    heat_pump.reservoir_hot_water,
                    QUANTITY_RESERVOIR_ENERGY,
                    start=start,
                    end=end,
                )
            ).values()
        )
    total = reservoir_heating_wh + reservoir_ww_wh
    if total > 0:
        meta.heating_share_source = "reservoir_ratio"
        return reservoir_heating_wh / total
    meta.heating_share_source = "fallback"
    meta.notes.append(
        "no reservoir data in period; heat pump split uses the fallback "
        f"heating share of {heat_pump.fallback_heating_share:.0%}"
    )
    return heat_pump.fallback_heating_share


def _add_heat_pump_lines(
    store: SeriesProvider,
    config: BillingConfig,
    accounts: dict[str, _ElectricityAccount],
    units: dict[str, UnitBill],
    prices: dict[str, float],
    start: dt.datetime,
    end: dt.datetime,
    meta: BillMeta,
) -> None:
    if config.heat_pump is None:
        return
    account = accounts[_HEAT_PUMP]
    hp_cost = account.total_cost(prices, config.pv_price_kwh)
    hp_kwh = account.total_kwh
    meta.heat_pump_kwh = hp_kwh
    meta.heat_pump_cost = hp_cost

    heating_share = _heating_share(store, config, start, end, meta)
    meta.heating_share = heating_share

    heating_cost = hp_cost * heating_share
    heating_kwh = hp_kwh * heating_share
    ww_cost = hp_cost * (1 - heating_share)
    ww_kwh = hp_kwh * (1 - heating_share)

    # Allocation keys: each unit's own meter over the period.
    heat_measured = {
        name: sum(
            _consumption(
                store.series(unit.heat, QUANTITY_HEAT_ENERGY, start=start, end=end)
            ).values()
        )
        for name, unit in config.units.items()
        if unit.heat is not None
    }
    ww_measured = {
        name: sum(
            _consumption(
                store.series(unit.hot_water, QUANTITY_WATER_VOLUME, start=start, end=end)
            ).values()
        )
        for name, unit in config.units.items()
        if unit.hot_water is not None
    }

    _allocate_proportionally(
        units,
        heat_measured,
        section=SECTION_HEATING,
        kind=KIND_HEATING,
        pool_kwh=heating_kwh,
        pool_cost=heating_cost,
        measured_unit="kWh",
        measured_scale=1 / 1000,  # Wh → kWh
        meta=meta,
        pool_label="heating",
    )
    _allocate_proportionally(
        units,
        ww_measured,
        section=SECTION_HOT_WATER,
        kind=KIND_HOT_WATER,
        pool_kwh=ww_kwh,
        pool_cost=ww_cost,
        measured_unit="m³",
        measured_scale=1.0,
        meta=meta,
        pool_label="hot water",
    )


def _allocate_proportionally(
    units: dict[str, UnitBill],
    measured: dict[str, float],
    *,
    section: str,
    kind: str,
    pool_kwh: float,
    pool_cost: float,
    measured_unit: str,
    measured_scale: float,
    meta: BillMeta,
    pool_label: str,
) -> None:
    """Split a cost pool across units in proportion to their own meters."""
    total = sum(measured.values())
    if total <= 0:
        if pool_cost > 0:
            meta.notes.append(
                f"heat pump {pool_label} cost of {pool_cost:.2f} could not be "
                "allocated: no unit meter consumption in the period"
            )
        return
    price = pool_cost / pool_kwh if pool_kwh > 0 else None
    for name, value in measured.items():
        portion = value / total
        units[name].lines.append(
            LineItem(
                section=section,
                kind=kind,
                detail=None,
                quantity=portion * pool_kwh,
                quantity_unit="kWh",
                price=price,
                cost=portion * pool_cost,
                measured=value * measured_scale,
                measured_unit=measured_unit,
            )
        )


def _water_volumes(
    store: SeriesProvider,
    config: BillingConfig,
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, WaterVolume]:
    volumes: dict[str, WaterVolume] = {}
    for name, unit in config.units.items():
        hot = cold = 0.0
        if unit.hot_water is not None:
            hot = sum(
                _consumption(
                    store.series(
                        unit.hot_water, QUANTITY_WATER_VOLUME, start=start, end=end
                    )
                ).values()
            )
        if unit.cold_water is not None:
            cold = sum(
                _consumption(
                    store.series(
                        unit.cold_water, QUANTITY_WATER_VOLUME, start=start, end=end
                    )
                ).values()
            )
        volumes[name] = WaterVolume(hot_m3=hot, cold_m3=cold)
    return volumes
