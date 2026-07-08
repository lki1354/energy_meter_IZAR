"""Tests for the generate_bill service."""

import datetime as dt
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import FakeRemoteClient
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_meter_izar.const import (
    CONF_DIRECTORY,
    CONF_PROTOCOL,
    DOMAIN,
    EVENT_BILL_GENERATED,
    SERVICE_GENERATE_BILL,
)
from custom_components.energy_meter_izar.mbus_parser import MeterReading

ENTRY_DATA = {
    CONF_PROTOCOL: "ftp",
    CONF_HOST: "gateway.local",
    CONF_PORT: 21,
    CONF_USERNAME: "mbus",
    CONF_PASSWORD: "secret",
    CONF_DIRECTORY: "/snapshots",
}

BILLING_YAML = """
currency: CHF
units:
  A: { electricity: 1 }
tariffs:
  - name: flat
    price_kwh: 0.5
    default: true
profiles:
  simple:
    sections: [electricity, summary]
    language: en
    formats: [markdown, csv]
"""


@pytest.fixture(autouse=True)
def _recorder(recorder_mock):
    return


@pytest.fixture(autouse=True)
def _custom_integrations(_recorder, enable_custom_integrations):
    return


@pytest.fixture(autouse=True)
def _isolated_config_dir(_recorder, hass, tmp_path):
    hass.config.config_dir = str(tmp_path)


async def _setup(hass, fixtures_dir) -> MockConfigEntry:
    name = "0080A3DB81A5_001.xml"
    client = FakeRemoteClient(
        {name: (fixtures_dir / name).read_bytes(), "0080A3DB81A5_001.rdy": b""}
    )
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id="ftp://gateway.local:21/snapshots"
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.energy_meter_izar.coordinator.create_client",
        return_value=client,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _electricity_reading(timestamp: dt.datetime, value: float) -> MeterReading:
    return MeterReading(
        device_number=1,
        medium="electricity",
        location="A",
        timestamp=timestamp,
        quantity="Energie 1 (Wh)",
        value=value,
        unit="Wh",
        status="00",
    )


async def _seed_readings(hass, entry) -> None:
    """Two consumption intervals of 1 kWh each on 2026-06-01."""
    store = entry.runtime_data.reading_store
    base = dt.datetime(2026, 6, 1, 10, 0)
    readings = [
        _electricity_reading(base + dt.timedelta(minutes=15 * i), 1000.0 * i)
        for i in range(3)
    ]
    await hass.async_add_executor_job(store.add_readings, readings)


def _write_billing_yaml(hass) -> None:
    path = Path(hass.config.path(DOMAIN, "billing.yaml"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BILLING_YAML, encoding="utf-8")


async def _generate(hass, **kwargs):
    data = {"start": "2026-06-01", "end": "2026-07-01", **kwargs}
    return await hass.services.async_call(
        DOMAIN,
        SERVICE_GENERATE_BILL,
        data,
        blocking=True,
        return_response=True,
    )


async def test_generate_bill_writes_files_and_fires_event(hass, fixtures_dir):
    entry = await _setup(hass, fixtures_dir)
    await _seed_readings(hass, entry)
    _write_billing_yaml(hass)

    events = []
    hass.bus.async_listen(EVENT_BILL_GENERATED, events.append)

    response = await _generate(hass, profile="simple")
    await hass.async_block_till_done()

    assert response["profile"] == "simple"
    # 2 kWh at 0.5 CHF/kWh
    assert response["total"] == pytest.approx(1.0)
    assert response["currency"] == "CHF"
    assert len(response["files"]) == 2

    markdown = Path(response["files"][0])
    assert markdown.name == "2026-06-01_2026-07-01_simple.md"
    text = markdown.read_text(encoding="utf-8")
    assert "## A" in text
    assert "Electricity flat (grid)" in text
    csv_file = Path(response["files"][1])
    assert csv_file.suffix == ".csv"
    assert "A,electricity,grid_flat" in csv_file.read_text(encoding="utf-8")

    assert len(events) == 1
    assert events[0].data["files"] == response["files"]


async def test_generate_bill_without_billing_yaml_uses_defaults(hass, fixtures_dir):
    """No billing.yaml → built-in building config; fixture readings feed it."""
    await _setup(hass, fixtures_dir)

    response = await _generate(hass, start="2026-06-01", end="2026-06-15")
    files = response["files"]
    assert response["profile"] == "quarterly_full"
    assert len(files) == 2
    text = Path(files[0]).read_text(encoding="utf-8")
    assert "EG" in text
    assert "hochtarif" in text


async def test_generate_bill_end_before_start_rejected(hass, fixtures_dir):
    await _setup(hass, fixtures_dir)
    with pytest.raises(ServiceValidationError):
        await _generate(hass, start="2026-07-01", end="2026-06-01")


async def test_generate_bill_unknown_profile_rejected(hass, fixtures_dir):
    await _setup(hass, fixtures_dir)
    _write_billing_yaml(hass)
    with pytest.raises(ServiceValidationError):
        await _generate(hass, profile="nope")


async def test_generate_bill_pdf_not_yet_supported(hass, fixtures_dir):
    await _setup(hass, fixtures_dir)
    with pytest.raises(ServiceValidationError):
        await _generate(hass, formats=["pdf"])


async def test_generate_bill_requires_loaded_entry(hass, fixtures_dir):
    entry = await _setup(hass, fixtures_dir)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError):
        await _generate(hass)
