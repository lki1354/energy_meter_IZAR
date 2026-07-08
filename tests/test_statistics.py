"""Tests for the external-statistics backfill (pure math + recorder e2e)."""

import datetime as dt
from unittest.mock import patch

import pytest
from conftest import FakeRemoteClient
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.energy_meter_izar.const import (
    CONF_DIRECTORY,
    CONF_PROTOCOL,
    DOMAIN,
)
from custom_components.energy_meter_izar.statistics import (
    cumulative_sums,
    hourly_last,
    statistic_id,
)

UTC = dt.UTC


def _hour(day: int, hour: int) -> dt.datetime:
    return dt.datetime(2026, 6, day, hour, tzinfo=UTC)


class TestHourlyLast:
    def test_last_value_per_hour_wins(self):
        points = [
            (_hour(1, 12).replace(minute=0), 10.0),
            (_hour(1, 12).replace(minute=30), 12.0),
            (_hour(1, 12).replace(minute=45), 13.0),
        ]
        assert hourly_last(points) == [(_hour(1, 12), 13.0)]

    def test_buckets_sorted_across_hours(self):
        points = [
            (_hour(1, 13).replace(minute=15), 20.0),
            (_hour(1, 12).replace(minute=15), 10.0),
        ]
        assert hourly_last(points) == [(_hour(1, 12), 10.0), (_hour(1, 13), 20.0)]

    def test_empty(self):
        assert hourly_last([]) == []


class TestCumulativeSums:
    def test_monotonic_counter_sum_equals_state(self):
        rows = cumulative_sums([(_hour(1, 0), 100.0), (_hour(1, 1), 110.0)])
        assert [(r["state"], r["sum"]) for r in rows] == [(100.0, 100.0), (110.0, 110.0)]

    def test_meter_reset_keeps_sum_increasing(self):
        rows = cumulative_sums(
            [(_hour(1, 0), 100.0), (_hour(1, 1), 2.0), (_hour(1, 2), 5.0)]
        )
        assert [r["sum"] for r in rows] == [100.0, 102.0, 105.0]

    def test_seeding_continues_previous_series(self):
        # last imported stat had state 90, sum 240 → offset 150
        rows = cumulative_sums([(_hour(1, 0), 95.0)], offset=150.0)
        assert rows[0]["sum"] == 245.0

    def test_seeded_previous_state_detects_reset(self):
        rows = cumulative_sums([(_hour(1, 0), 1.0)], offset=150.0, previous_state=90.0)
        assert rows[0]["sum"] == 241.0


# --- end-to-end: files → store → recorder statistics -------------------------

ENTRY_DATA = {
    CONF_PROTOCOL: "ftp",
    CONF_HOST: "gateway.local",
    CONF_PORT: 21,
    CONF_USERNAME: "mbus",
    CONF_PASSWORD: "secret",
    CONF_DIRECTORY: "/snapshots",
}

EG_ENERGY = statistic_id(11601997, "energy")
EG_HOT_WATER = statistic_id(2800002, "volume")


def _fixture_client(fixtures_dir, *names: str) -> FakeRemoteClient:
    files = {}
    for name in names:
        files[name] = (fixtures_dir / name).read_bytes()
        files[name.rsplit(".", 1)[0] + ".rdy"] = b""
    return FakeRemoteClient(files)


def _add_fixture(client, fixtures_dir, name: str) -> None:
    client.files[name] = (fixtures_dir / name).read_bytes()
    client.files[name.rsplit(".", 1)[0] + ".rdy"] = b""


def _patch_client(client):
    return patch(
        "custom_components.energy_meter_izar.coordinator.create_client",
        return_value=client,
    )


async def _setup(hass, tmp_path, client) -> MockConfigEntry:
    # the shared testing config dir must not accumulate readings.db files
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id="ftp://gateway.local:21/snapshots"
    )
    entry.add_to_hass(hass)
    with _patch_client(client):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    return entry


async def _get_stats(hass, *statistic_ids: str) -> dict:
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt.datetime(2026, 1, 1, tzinfo=UTC),
        None,
        set(statistic_ids),
        "hour",
        None,
        {"state", "sum"},
    )


class TestBackfill:
    """End-to-end backfill tests against a real (in-memory) recorder."""

    @pytest.fixture(autouse=True)
    def _recorder(self, recorder_mock):
        """The integration declares recorder as a manifest dependency."""
        return

    @pytest.fixture(autouse=True)
    def _custom_integrations(self, _recorder, enable_custom_integrations):
        # depends on _recorder so the recorder database is initialized
        # before anything touches the hass fixture
        return

    async def test_setup_backfills_statistics(self, hass, tmp_path, fixtures_dir):
        """Snapshot _001 carries 24h of 15-min electricity history — all of
        it must land as hourly external statistics, not just the latest
        state."""
        await _setup(hass, tmp_path, _fixture_client(fixtures_dir, "0080A3DB81A5_001.xml"))

        stats = await _get_stats(hass, EG_ENERGY, EG_HOT_WATER)

        energy = stats[EG_ENERGY]
        assert len(energy) == 24  # local hours 2026-06-06 00:00 … 23:00
        # readings are Wh, statistics kWh; last reading of the first local
        # hour is 00:45 → 18 484 350 Wh
        assert energy[0]["state"] == pytest.approx(18484.35)
        assert energy[0]["sum"] == pytest.approx(18484.35)
        assert energy[-1]["state"] == pytest.approx(18491.29)
        assert energy[-1]["sum"] == pytest.approx(18491.29)
        # local midnight in the default test TZ (US/Pacific, UTC-7)
        assert energy[0]["start"] == dt.datetime(2026, 6, 6, 7, tzinfo=UTC).timestamp()

        water = stats[EG_HOT_WATER]
        assert len(water) == 1
        assert water[0]["state"] == pytest.approx(241.772)

    async def test_refresh_without_new_files_changes_nothing(
        self, hass, tmp_path, fixtures_dir
    ):
        client = _fixture_client(fixtures_dir, "0080A3DB81A5_001.xml")
        entry = await _setup(hass, tmp_path, client)
        before = await _get_stats(hass, EG_ENERGY)

        with _patch_client(client):
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
        await async_wait_recording_done(hass)

        assert await _get_stats(hass, EG_ENERGY) == before

    async def test_newer_file_continues_series(self, hass, tmp_path, fixtures_dir):
        """A later snapshot appends to the existing statistics without
        touching the already-imported history."""
        client = _fixture_client(fixtures_dir, "0080A3DB81A5_000.xml")
        entry = await _setup(hass, tmp_path, client)
        assert len((await _get_stats(hass, EG_ENERGY))[EG_ENERGY]) == 24

        with _patch_client(client):
            _add_fixture(client, fixtures_dir, "0080A3DB81A5_001.xml")
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
        await async_wait_recording_done(hass)

        energy = (await _get_stats(hass, EG_ENERGY))[EG_ENERGY]
        assert len(energy) == 48  # 2026-06-05 plus 2026-06-06, hourly
        sums = [row["sum"] for row in energy]
        assert sums == sorted(sums)
        assert energy[-1]["sum"] == pytest.approx(18491.29)

    async def test_late_older_file_rewrites_history_consistently(
        self, hass, tmp_path, fixtures_dir
    ):
        """A snapshot older than the statistics tip (delayed upload) triggers
        a full recompute so the earlier hours appear and sums stay
        monotonic."""
        client = _fixture_client(fixtures_dir, "0080A3DB81A5_000.xml")
        entry = await _setup(hass, tmp_path, client)

        with _patch_client(client):
            _add_fixture(client, fixtures_dir, "0080A3DB81A5_999.xml")  # 2026-06-04 data
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
        await async_wait_recording_done(hass)

        energy = (await _get_stats(hass, EG_ENERGY))[EG_ENERGY]
        assert len(energy) == 48  # 2026-06-04 plus 2026-06-05
        assert energy[0]["start"] == dt.datetime(2026, 6, 4, 7, tzinfo=UTC).timestamp()
        assert energy[0]["state"] == pytest.approx(18465.51)  # 06-04 00:45 local
        sums = [row["sum"] for row in energy]
        assert sums == sorted(sums)
        assert energy[-1]["sum"] == pytest.approx(18484.21)  # 06-05 23:45 local

    async def test_readings_survive_reload_and_feed_statistics(
        self, hass, tmp_path, fixtures_dir
    ):
        """readings.db persists across entry reloads; a reload with no new
        files must not duplicate or shift any statistics."""
        client = _fixture_client(fixtures_dir, "0080A3DB81A5_001.xml")
        entry = await _setup(hass, tmp_path, client)
        before = await _get_stats(hass, EG_ENERGY)

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        with _patch_client(client):
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        await async_wait_recording_done(hass)

        assert await _get_stats(hass, EG_ENERGY) == before
        assert (tmp_path / DOMAIN / "readings.db").exists()
