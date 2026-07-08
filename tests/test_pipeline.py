"""Tests for the HA-free poll pipeline (listing → download → parse → state)."""

import datetime as dt

import pytest
from conftest import FakeRemoteClient, snapshot_bytes

from custom_components.energy_meter_izar.pipeline import SnapshotPipeline

PATTERN = "0080A3DB81A5_*.xml"


def _name(seq: int) -> str:
    return f"0080A3DB81A5_{seq:03d}.xml"


def _rdy(seq: int) -> str:
    return f"0080A3DB81A5_{seq:03d}.rdy"


def _with_markers(files: dict[str, bytes]) -> dict[str, bytes]:
    return files | {name.rsplit(".", 1)[0] + ".rdy": b"" for name in files}


async def test_poll_ingests_fixture_files_across_wrap(fixtures_dir):
    """Real snapshots _999/_000/_001 ingest in gateway-MBTIME order across
    the counter wrap and populate all 19 meters with their latest readings."""
    files = _with_markers(
        {
            name: (fixtures_dir / name).read_bytes()
            # listed deliberately out of order — MBTIME must decide
            for name in (_name(1), _name(999), _name(0))
        }
    )
    client = FakeRemoteClient(files)
    pipeline = SnapshotPipeline(file_pattern=PATTERN)

    result = await pipeline.poll(client)

    assert result.files_ingested == [_name(999), _name(0), _name(1)]
    assert result.last_readout_time == dt.datetime(2026, 6, 7, 0, 0)
    assert result.files_missing == 0
    assert not result.warnings
    assert len(result.meters) == 19

    eg_electricity = result.meters[11601997]
    energy = eg_electricity.readings["Energie 1 (Wh)"]
    assert energy.value == 18491290.0
    assert energy.timestamp == dt.datetime(2026, 6, 6, 23, 45)
    assert eg_electricity.definition.location == "EG"

    assert result.meters[2800002].readings["water_1 (m^3)"].value == 241.772
    assert result.gateway is not None
    assert result.gateway.device_id == "44610100"


async def test_rdy_marker_gates_ingestion():
    files = {_name(10): snapshot_bytes(dt.datetime(2026, 6, 1))}
    client = FakeRemoteClient(files)
    pipeline = SnapshotPipeline(file_pattern=PATTERN)

    result = await pipeline.poll(client)
    assert result.files_ingested == []
    assert client.downloads == []

    client.files[_rdy(10)] = b""
    result = await pipeline.poll(client)
    assert result.files_ingested == [_name(10)]


async def test_rdy_marker_optional():
    files = {_name(10): snapshot_bytes(dt.datetime(2026, 6, 1))}
    pipeline = SnapshotPipeline(file_pattern=PATTERN, require_rdy=False)
    result = await pipeline.poll(FakeRemoteClient(files))
    assert result.files_ingested == [_name(10)]


async def test_second_poll_downloads_nothing():
    files = _with_markers(
        {
            _name(1): snapshot_bytes(dt.datetime(2026, 6, 1)),
            _name(2): snapshot_bytes(dt.datetime(2026, 6, 2)),
        }
    )
    client = FakeRemoteClient(files)
    pipeline = SnapshotPipeline(file_pattern=PATTERN)

    await pipeline.poll(client)
    assert sorted(client.downloads) == [_name(1), _name(2)]

    result = await pipeline.poll(client)
    assert len(client.downloads) == 2  # unchanged — nothing fetched again
    assert result.files_ingested == []


async def test_wrapped_filename_reuse_is_reingested():
    """The same filename with a different gateway MBTIME (counter wrapped
    and reused the name) is new content and must be ingested again."""
    client = FakeRemoteClient(_with_markers({_name(5): snapshot_bytes(dt.datetime(2026, 1, 1))}))
    pipeline = SnapshotPipeline(file_pattern=PATTERN)
    result = await pipeline.poll(client)
    assert result.files_ingested == [_name(5)]

    client.files[_name(5)] = snapshot_bytes(dt.datetime(2026, 12, 24))
    client.mtimes[_name(5)] = 2000.0
    result = await pipeline.poll(client)
    assert result.files_ingested == [_name(5)]
    assert result.last_readout_time == dt.datetime(2026, 12, 24)


async def test_changed_metadata_same_content_not_reingested():
    """A re-uploaded file with identical content is downloaded again (mtime
    changed) but recognized as already ingested; the refreshed metadata then
    suppresses further downloads."""
    content = snapshot_bytes(dt.datetime(2026, 3, 1))
    client = FakeRemoteClient(_with_markers({_name(7): content}))
    pipeline = SnapshotPipeline(file_pattern=PATTERN)
    await pipeline.poll(client)

    client.mtimes[_name(7)] = 2000.0
    result = await pipeline.poll(client)
    assert result.files_ingested == []
    assert client.downloads.count(_name(7)) == 2

    await pipeline.poll(client)
    assert client.downloads.count(_name(7)) == 2


async def test_unparsable_file_warns_once_and_is_not_retried():
    client = FakeRemoteClient(_with_markers({_name(3): b"this is not xml"}))
    pipeline = SnapshotPipeline(file_pattern=PATTERN)

    result = await pipeline.poll(client)
    assert result.files_ingested == []
    assert any("unparsable" in warning for warning in result.warnings)

    result = await pipeline.poll(client)
    assert client.downloads.count(_name(3)) == 1
    assert not result.warnings


async def test_missing_gateway_mbtime_rejected():
    bad = b"<HC2XML><UNIT><TYPE>60M</TYPE></UNIT><MEM></MEM></HC2XML>"
    client = FakeRemoteClient(_with_markers({_name(4): bad}))
    pipeline = SnapshotPipeline(file_pattern=PATTERN)

    result = await pipeline.poll(client)
    assert result.files_ingested == []
    assert any("MBTIME" in warning for warning in result.warnings)


async def test_gap_across_wrap_reported():
    files = _with_markers(
        {
            _name(997): snapshot_bytes(dt.datetime(2026, 6, 1)),
            _name(1): snapshot_bytes(dt.datetime(2026, 6, 5)),
        }
    )
    pipeline = SnapshotPipeline(file_pattern=PATTERN)
    result = await pipeline.poll(FakeRemoteClient(files))

    assert result.files_ingested == [_name(997), _name(1)]
    assert result.files_missing == 3  # 998, 999, 000
    assert any("counter wrapped" in warning for warning in result.warnings)


async def test_delete_after_removes_snapshot_and_marker():
    files = _with_markers({_name(8): snapshot_bytes(dt.datetime(2026, 6, 1))})
    client = FakeRemoteClient(files)
    pipeline = SnapshotPipeline(file_pattern=PATTERN, delete_after=True)

    result = await pipeline.poll(client)
    assert result.files_ingested == [_name(8)]
    assert client.deleted == [_name(8), _rdy(8)]
    assert client.files == {}


async def test_non_matching_files_ignored():
    files = {
        "readme.txt": b"hi",
        "OTHER_001.xml": snapshot_bytes(dt.datetime(2026, 6, 1)),
    }
    pipeline = SnapshotPipeline(file_pattern=PATTERN, require_rdy=False)
    client = FakeRemoteClient(files)
    result = await pipeline.poll(client)
    assert result.files_ingested == []
    assert client.downloads == []


async def test_state_survives_tracker_roundtrip():
    """Persisted tracker/watcher state prevents re-ingestion after restart."""
    from custom_components.energy_meter_izar.ingest import IngestionTracker, SequenceWatcher

    files = _with_markers({_name(1): snapshot_bytes(dt.datetime(2026, 6, 1))})
    client = FakeRemoteClient(files)
    pipeline = SnapshotPipeline(file_pattern=PATTERN)
    await pipeline.poll(client)

    restarted = SnapshotPipeline(
        tracker=IngestionTracker.from_dict(pipeline.tracker.to_dict()),
        watcher=SequenceWatcher.from_dict(pipeline.watcher.to_dict()),
        file_pattern=PATTERN,
    )
    result = await restarted.poll(client)
    assert result.files_ingested == []
    assert result.last_readout_time == dt.datetime(2026, 6, 1)


async def test_backlog_capped_per_poll(monkeypatch):
    import custom_components.energy_meter_izar.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "MAX_FILES_PER_POLL", 2)
    files = _with_markers(
        {_name(i): snapshot_bytes(dt.datetime(2026, 6, i)) for i in range(1, 5)}
    )
    client = FakeRemoteClient(files)
    pipeline = SnapshotPipeline(file_pattern=PATTERN)

    first = await pipeline.poll(client)
    assert len(first.files_ingested) == 2
    assert any("deferring" in warning for warning in first.warnings)

    second = await pipeline.poll(client)
    assert len(second.files_ingested) == 2
    assert sorted(first.files_ingested + second.files_ingested) == [
        _name(1), _name(2), _name(3), _name(4),
    ]


@pytest.mark.parametrize("bad_protocol", ["http", "", "ftpes"])
def test_create_client_rejects_unknown_protocol(bad_protocol):
    from custom_components.energy_meter_izar.ftp_client import ConnectionConfig, create_client

    with pytest.raises(ValueError):
        create_client(
            ConnectionConfig(
                protocol=bad_protocol, host="h", port=21, username="u", password="p"
            )
        )
