"""Tests for the SQLite reading store (pure Python, no HA)."""

import datetime as dt

from custom_components.energy_meter_izar.mbus_parser import MeterReading
from custom_components.energy_meter_izar.store import ReadingStore


def _reading(
    timestamp: dt.datetime,
    value,
    *,
    device: int = 11601997,
    quantity: str = "Energie 1 (Wh)",
) -> MeterReading:
    return MeterReading(
        device_number=device,
        medium="electricity",
        location="EG",
        timestamp=timestamp,
        quantity=quantity,
        value=value,
        unit="Wh",
        status="0",
    )


def test_add_readings_dedupes_on_device_quantity_timestamp(tmp_path):
    store = ReadingStore(tmp_path / "readings.db")
    reading = _reading(dt.datetime(2026, 6, 1, 12, 0), 1000.0)

    assert store.add_readings([reading]) == 1
    # identical key again — e.g. a re-ingested wrapped-counter file
    assert store.add_readings([reading, _reading(dt.datetime(2026, 6, 1, 12, 15), 1010.0)]) == 1
    assert store.reading_count() == 2


def test_series_is_time_ordered_and_range_filtered(tmp_path):
    store = ReadingStore(tmp_path / "readings.db")
    times = [dt.datetime(2026, 6, 1, 12, 0) + dt.timedelta(minutes=15 * i) for i in range(4)]
    # insert deliberately out of order
    store.add_readings([_reading(t, float(i)) for i, t in reversed(list(enumerate(times)))])

    series = store.series(11601997, "Energie 1 (Wh)")
    assert [v.value for v in series] == [0.0, 1.0, 2.0, 3.0]
    assert [v.timestamp for v in series] == times

    # start inclusive, end exclusive
    window = store.series(11601997, "Energie 1 (Wh)", start=times[1], end=times[3])
    assert [v.value for v in window] == [1.0, 2.0]


def test_series_separates_devices_and_quantities(tmp_path):
    store = ReadingStore(tmp_path / "readings.db")
    when = dt.datetime(2026, 6, 1, 12, 0)
    store.add_readings(
        [
            _reading(when, 1.0),
            _reading(when, 2.0, device=11601989),
            _reading(when, 3.0, quantity="Leistung 1 (W)"),
        ]
    )
    assert [v.value for v in store.series(11601997, "Energie 1 (Wh)")] == [1.0]
    assert [v.value for v in store.series(11601989, "Energie 1 (Wh)")] == [2.0]
    assert [v.value for v in store.series(11601997, "Leistung 1 (W)")] == [3.0]


def test_non_numeric_values_stored_but_not_in_series(tmp_path):
    """Dates/texts (historical water reference readings) must survive for
    billing but never leak into numeric statistics series."""
    store = ReadingStore(tmp_path / "readings.db")
    when = dt.datetime(2026, 6, 1, 12, 0)
    store.add_readings(
        [
            _reading(when, dt.date(2026, 6, 1), quantity="water_2 (date)"),
            _reading(when, dt.datetime(2026, 6, 1, 11, 59), quantity="water_1 (date time)"),
            _reading(when, "customer", quantity="water_customer_1 (none)"),
            _reading(when, True, quantity="error (none)"),
        ]
    )
    assert store.reading_count() == 4
    assert store.series(11601997, "water_2 (date)") == []
    assert store.series(11601997, "water_customer_1 (none)") == []
    assert store.series(11601997, "error (none)") == []


def test_store_persists_across_reopen(tmp_path):
    path = tmp_path / "readings.db"
    store = ReadingStore(path)
    store.add_readings([_reading(dt.datetime(2026, 6, 1, 12, 0), 42.0)])
    store.close()

    reopened = ReadingStore(path)
    assert [v.value for v in reopened.series(11601997, "Energie 1 (Wh)")] == [42.0]
    # dedupe also works against previously persisted rows
    assert reopened.add_readings([_reading(dt.datetime(2026, 6, 1, 12, 0), 42.0)]) == 0
    reopened.close()


def test_store_creates_parent_directory(tmp_path):
    store = ReadingStore(tmp_path / "energy_meter_izar" / "readings.db")
    assert store.reading_count() == 0
    store.close()
