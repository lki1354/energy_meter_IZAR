"""End-to-end setup tests: config entry → coordinator → sensor entities."""

from unittest.mock import patch

import pytest
from conftest import FakeRemoteClient
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_meter_izar.const import (
    CONF_DIRECTORY,
    CONF_PROTOCOL,
    DOMAIN,
)
from custom_components.energy_meter_izar.ftp_client import FetchAuthError, FetchError

ENTRY_DATA = {
    CONF_PROTOCOL: "ftp",
    CONF_HOST: "gateway.local",
    CONF_PORT: 21,
    CONF_USERNAME: "mbus",
    CONF_PASSWORD: "secret",
    CONF_DIRECTORY: "/snapshots",
}


@pytest.fixture(autouse=True)
def _recorder(recorder_mock):
    """The integration declares recorder as a manifest dependency."""
    return


@pytest.fixture(autouse=True)
def _custom_integrations(_recorder, enable_custom_integrations):
    # depends on _recorder so the recorder database is initialized before
    # anything touches the hass fixture
    return


@pytest.fixture(autouse=True)
def _isolated_config_dir(_recorder, hass, tmp_path):
    """Keep readings.db out of the shared testing config dir."""
    hass.config.config_dir = str(tmp_path)


def _fixture_client(fixtures_dir, *names: str) -> FakeRemoteClient:
    files = {}
    for name in names:
        files[name] = (fixtures_dir / name).read_bytes()
        files[name.rsplit(".", 1)[0] + ".rdy"] = b""
    return FakeRemoteClient(files)


def _patch_client(client):
    return patch(
        "custom_components.energy_meter_izar.coordinator.create_client",
        return_value=client,
    )


async def _setup(hass, client) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id="ftp://gateway.local:21/snapshots"
    )
    entry.add_to_hass(hass)
    with _patch_client(client):
        await hass.config_entries.async_setup(entry.entry_id)
        # the first poll runs as a background task right after setup
        await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def test_setup_creates_meter_and_gateway_entities(hass, fixtures_dir):
    entry = await _setup(hass, _fixture_client(fixtures_dir, "0080A3DB81A5_001.xml"))
    assert entry.state is ConfigEntryState.LOADED

    entity_registry = er.async_get(hass)

    # EG electricity meter: 18 491 290 Wh shown as kWh via suggested unit
    entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, "11601997_energy")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert float(state.state) == pytest.approx(18491.29)
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert state.attributes["device_class"] == "energy"
    assert state.attributes["state_class"] == "total_increasing"

    # EG hot-water meter volume
    entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, "2800002_volume")
    state = hass.states.get(entity_id)
    assert float(state.state) == pytest.approx(241.772)
    assert state.attributes["device_class"] == "water"

    # gateway diagnostics
    entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_last_readout"
    )
    state = hass.states.get(entity_id)
    # gateway MBTIME 2026-06-07 00:00 is local time; test TZ is US/Pacific (UTC-7)
    assert state.state == "2026-06-07T07:00:00+00:00"

    # one HA device per physical meter + the gateway itself
    device_registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert len(devices) == 20

    meter_device = device_registry.async_get_device({(DOMAIN, "11601997")})
    assert meter_device is not None
    assert meter_device.name == "Electricity EG"
    assert meter_device.serial_number == "11601997"


async def test_new_meters_appear_on_later_polls(hass, fixtures_dir):
    """Meters missing from the first snapshot are added when they show up."""
    client = _fixture_client(fixtures_dir, "0080A3DB81A5_000.xml")
    entry = await _setup(hass, client)
    coordinator = entry.runtime_data

    entity_registry = er.async_get(hass)
    assert entity_registry.async_get_entity_id("sensor", DOMAIN, "11601997_energy")

    with _patch_client(client):
        # nothing new listed → refresh keeps everything and adds nothing
        before = len(entity_registry.entities)
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert len(entity_registry.entities) == before

        # next snapshot appears → newer values are picked up
        name = "0080A3DB81A5_001.xml"
        client.files[name] = (fixtures_dir / name).read_bytes()
        client.files["0080A3DB81A5_001.rdy"] = b""
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, "11601997_energy")
    assert float(hass.states.get(entity_id).state) == pytest.approx(18491.29)


async def test_unload_entry(hass, fixtures_dir):
    entry = await _setup(hass, _fixture_client(fixtures_dir, "0080A3DB81A5_001.xml"))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_connection_failure_retries_setup(hass):
    entry = await _setup(hass, FakeRemoteClient(connect_error=FetchError("no route")))
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_auth_failure_starts_reauth(hass):
    entry = await _setup(hass, FakeRemoteClient(connect_error=FetchAuthError("530")))
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == "reauth" for flow in flows)
