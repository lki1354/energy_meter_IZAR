"""Unit tests for the billing engine (tariffs, PV allocation, splits)."""

import datetime as dt

import pytest

from custom_components.energy_meter_izar.billing.config import parse_billing_config
from custom_components.energy_meter_izar.billing.engine import (
    KIND_COMMON_GRID,
    KIND_COMMON_PV,
    KIND_GRID,
    KIND_HEATING,
    KIND_HOT_WATER,
    KIND_PV,
    QUANTITY_ELECTRICITY_ENERGY,
    QUANTITY_HEAT_ENERGY,
    QUANTITY_RESERVOIR_ENERGY,
    QUANTITY_WATER_VOLUME,
    assign_tariff,
    generate_bill,
)


class FakeStore:
    """In-memory SeriesProvider: {(device, quantity): [(timestamp, value)]}."""

    def __init__(self, data):
        self.data = data

    def series(self, device_number, quantity, *, start=None, end=None):
        points = self.data.get((device_number, quantity), [])
        return [
            (ts, value)
            for ts, value in points
            if (start is None or ts >= start) and (end is None or ts < end)
        ]


CONFIG = parse_billing_config(
    {
        "currency": "CHF",
        "units": {
            "A": {"electricity": 1, "heat": 11, "hot_water": 21, "cold_water": 31},
            "B": {"electricity": 2, "heat": 12, "hot_water": 22},
        },
        "shared": {
            "common_electricity": {"device": 3},
            "heat_pump": {
                "device": 4,
                "reservoir_heating": 41,
                "reservoir_hot_water": 42,
                "fallback_heating_share": 0.70,
            },
            "photovoltaic": {"device": 5},
        },
        "tariffs": [
            {
                "name": "hochtarif",
                "price_kwh": 0.2,
                "schedule": [
                    {
                        "days": ["mon", "tue", "wed", "thu", "fri"],
                        "from": "07:00",
                        "to": "20:00",
                    },
                    {"days": ["sat"], "from": "07:00", "to": "13:00"},
                ],
            },
            {"name": "niedertarif", "price_kwh": 0.1, "default": True},
            {"name": "pv", "price_kwh": 0.05},
        ],
    }
)

# 2026-01-06 is a Tuesday; t1 falls into hochtarif, t2 into niedertarif.
T0 = dt.datetime(2026, 1, 6, 10, 0)
T1 = dt.datetime(2026, 1, 6, 10, 15)
T2 = dt.datetime(2026, 1, 6, 20, 15)
START = dt.datetime(2026, 1, 1)
END = dt.datetime(2026, 2, 1)

ELEC = QUANTITY_ELECTRICITY_ENERGY

SCENARIO = {
    # electricity counters (Wh): diffs at T1 / T2
    (1, ELEC): [(T0, 10_000.0), (T1, 11_000.0), (T2, 12_000.0)],  # A: +1000 / +1000
    (2, ELEC): [(T0, 5_000.0), (T1, 8_000.0), (T2, 8_000.0)],  # B: +3000 / 0
    (3, ELEC): [(T0, 100.0), (T1, 700.0), (T2, 700.0)],  # common: +600 / 0
    (4, ELEC): [(T0, 0.0), (T1, 2_000.0), (T2, 3_000.0)],  # heat pump: +2000 / +1000
    (5, ELEC): [(T0, 0.0), (T1, 3_300.0), (T2, 3_300.0)],  # PV: +3300 / 0
    # reservoirs (Wh): heating 600, hot water 400 → heating share 0.6
    (41, QUANTITY_RESERVOIR_ENERGY): [(T0, 1_000.0), (T1, 1_600.0)],
    (42, QUANTITY_RESERVOIR_ENERGY): [(T0, 2_000.0), (T1, 2_400.0)],
    # unit heat meters (Wh): A 300, B 700
    (11, QUANTITY_HEAT_ENERGY): [(T0, 0.0), (T1, 300.0)],
    (12, QUANTITY_HEAT_ENERGY): [(T0, 0.0), (T1, 700.0)],
    # hot water (m³): A 0.2, B 0.3; cold water A 0.5
    (21, QUANTITY_WATER_VOLUME): [(T0, 10.0), (T1, 10.2)],
    (22, QUANTITY_WATER_VOLUME): [(T0, 5.0), (T1, 5.3)],
    (31, QUANTITY_WATER_VOLUME): [(T0, 1.0), (T1, 1.5)],
}


def _line(bill, kind, detail=None):
    return next(
        line for line in bill.lines if line.kind == kind and line.detail == detail
    )


@pytest.fixture(scope="module")
def result():
    return generate_bill(FakeStore(SCENARIO), CONFIG, START, END)


def test_pv_factor_splits_grid_and_pv(result):
    # T1: house = 6600 Wh, PV = 3300 Wh → pv_factor 0.5 (hochtarif)
    # T2: house = 2000 Wh, PV = 0 → pv_factor 0 (niedertarif)
    a = result.units["A"]
    assert _line(a, KIND_GRID, "hochtarif").quantity == pytest.approx(0.5)
    assert _line(a, KIND_GRID, "hochtarif").cost == pytest.approx(0.1)
    assert _line(a, KIND_GRID, "niedertarif").quantity == pytest.approx(1.0)
    assert _line(a, KIND_GRID, "niedertarif").cost == pytest.approx(0.1)
    assert _line(a, KIND_PV).quantity == pytest.approx(0.5)
    assert _line(a, KIND_PV).cost == pytest.approx(0.025)

    b = result.units["B"]
    assert _line(b, KIND_GRID, "hochtarif").quantity == pytest.approx(1.5)
    assert _line(b, KIND_GRID, "niedertarif").quantity == pytest.approx(0.0)
    assert _line(b, KIND_PV).quantity == pytest.approx(1.5)


def test_common_electricity_split_equally(result):
    # common: 600 Wh at pv_factor 0.5 → 0.3 kWh grid (hochtarif) + 0.3 kWh PV
    for unit in ("A", "B"):
        bill = result.units[unit]
        common_grid = _line(bill, KIND_COMMON_GRID)
        assert common_grid.quantity == pytest.approx(0.15)
        assert common_grid.cost == pytest.approx(0.3 * 0.2 / 2)
        assert common_grid.price is None  # mixed tariffs
        common_pv = _line(bill, KIND_COMMON_PV)
        assert common_pv.quantity == pytest.approx(0.15)
        assert common_pv.cost == pytest.approx(0.3 * 0.05 / 2)


def test_heat_pump_reservoir_split_and_allocation(result):
    # heat pump: 1 kWh HT grid (0.2) + 1 kWh NT grid (0.1) + 1 kWh PV (0.05)
    assert result.meta.heat_pump_kwh == pytest.approx(3.0)
    assert result.meta.heat_pump_cost == pytest.approx(0.35)
    # reservoirs 600/400 Wh → heating share 0.6
    assert result.meta.heating_share == pytest.approx(0.6)
    assert result.meta.heating_share_source == "reservoir_ratio"

    a_heat = _line(result.units["A"], KIND_HEATING)
    assert a_heat.cost == pytest.approx(0.3 * 0.35 * 0.6)  # portion 300/1000
    assert a_heat.quantity == pytest.approx(0.3 * 3.0 * 0.6)
    assert a_heat.measured == pytest.approx(0.3)  # own meter, kWh
    assert a_heat.measured_unit == "kWh"
    b_heat = _line(result.units["B"], KIND_HEATING)
    assert b_heat.cost == pytest.approx(0.7 * 0.35 * 0.6)

    a_ww = _line(result.units["A"], KIND_HOT_WATER)
    assert a_ww.cost == pytest.approx(0.4 * 0.35 * 0.4)  # portion 0.2/0.5
    assert a_ww.measured == pytest.approx(0.2)
    assert a_ww.measured_unit == "m³"
    b_ww = _line(result.units["B"], KIND_HOT_WATER)
    assert b_ww.cost == pytest.approx(0.6 * 0.35 * 0.4)


def test_totals(result):
    assert result.units["A"].total == pytest.approx(0.3815)
    assert result.units["B"].total == pytest.approx(0.6435)
    assert result.total == pytest.approx(1.025)


def test_meta_energy_accounting(result):
    assert result.meta.intervals == 2
    assert result.meta.house_consumption_kwh == pytest.approx(8.6)
    assert result.meta.pv_production_kwh == pytest.approx(3.3)
    assert result.meta.pv_self_consumed_kwh == pytest.approx(3.3)
    assert result.meta.pv_exported_kwh == pytest.approx(0.0)


def test_water_volumes(result):
    assert result.water["A"].hot_m3 == pytest.approx(0.2)
    assert result.water["A"].cold_m3 == pytest.approx(0.5)
    assert result.water["B"].hot_m3 == pytest.approx(0.3)
    assert result.water["B"].cold_m3 == pytest.approx(0.0)


def test_tariff_assignment_boundaries():
    def name(timestamp):
        return assign_tariff(CONFIG, timestamp).name

    monday = dt.date(2026, 1, 5)
    saturday = dt.date(2026, 1, 10)
    sunday = dt.date(2026, 1, 11)
    assert name(dt.datetime.combine(monday, dt.time(6, 59))) == "niedertarif"
    assert name(dt.datetime.combine(monday, dt.time(7, 0))) == "hochtarif"
    assert name(dt.datetime.combine(monday, dt.time(19, 59))) == "hochtarif"
    assert name(dt.datetime.combine(monday, dt.time(20, 0))) == "niedertarif"
    assert name(dt.datetime.combine(saturday, dt.time(12, 59))) == "hochtarif"
    assert name(dt.datetime.combine(saturday, dt.time(13, 0))) == "niedertarif"
    assert name(dt.datetime.combine(sunday, dt.time(12, 0))) == "niedertarif"


def test_meter_reset_negative_diff_dropped():
    data = {
        (1, ELEC): [
            (T0, 10_000.0),
            (T1, 11_000.0),  # +1000
            (T2, 500.0),  # reset: negative diff dropped
            (T2 + dt.timedelta(minutes=15), 700.0),  # +200 from new baseline
        ]
    }
    config = parse_billing_config(
        {
            "units": {"A": {"electricity": 1}},
            "tariffs": [{"name": "flat", "price_kwh": 1.0, "default": True}],
        }
    )
    result = generate_bill(FakeStore(data), config, START, END)
    grid = _line(result.units["A"], KIND_GRID, "flat")
    assert grid.quantity == pytest.approx(1.2)


def test_fallback_heating_share_without_reservoir_data():
    data = dict(SCENARIO)
    data.pop((41, QUANTITY_RESERVOIR_ENERGY))
    data.pop((42, QUANTITY_RESERVOIR_ENERGY))
    result = generate_bill(FakeStore(data), CONFIG, START, END)
    assert result.meta.heating_share == pytest.approx(0.70)
    assert result.meta.heating_share_source == "fallback"
    assert any("fallback" in note for note in result.meta.notes)


def test_pv_surplus_is_exported():
    data = {
        (1, ELEC): [(T0, 0.0), (T1, 2_000.0)],
        (5, ELEC): [(T0, 0.0), (T1, 5_000.0), (T2, 6_000.0)],
    }
    config = parse_billing_config(
        {
            "units": {"A": {"electricity": 1}},
            "shared": {"photovoltaic": {"device": 5}},
            "tariffs": [
                {"name": "flat", "price_kwh": 1.0, "default": True},
                {"name": "pv", "price_kwh": 0.05},
            ],
        }
    )
    result = generate_bill(FakeStore(data), config, START, END)
    # T1: PV 5 kWh > house 2 kWh → everything PV-covered, 3 kWh exported;
    # T2: PV 1 kWh with no consumption → fully exported.
    bill = result.units["A"]
    assert _line(bill, KIND_GRID, "flat").quantity == pytest.approx(0.0)
    assert _line(bill, KIND_PV).quantity == pytest.approx(2.0)
    assert result.meta.pv_exported_kwh == pytest.approx(4.0)


def test_without_pv_everything_is_grid():
    config = parse_billing_config(
        {
            "units": {"A": {"electricity": 1}},
            "tariffs": [{"name": "flat", "price_kwh": 0.5, "default": True}],
        }
    )
    result = generate_bill(FakeStore(SCENARIO), config, START, END)
    bill = result.units["A"]
    assert _line(bill, KIND_GRID, "flat").quantity == pytest.approx(2.0)
    assert all(line.kind != KIND_PV for line in bill.lines)
    assert result.meta.pv_production_kwh == 0.0


def test_empty_period_produces_note():
    result = generate_bill(FakeStore(SCENARIO), CONFIG, T2, T2 + dt.timedelta(days=1))
    assert result.total == pytest.approx(0.0)
    assert any("no readings" in note for note in result.meta.notes)


def test_invalid_period_rejected():
    with pytest.raises(ValueError, match="after start"):
        generate_bill(FakeStore(SCENARIO), CONFIG, END, START)
