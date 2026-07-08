"""Tests for wrap-aware sequence handling and exactly-once ingestion.

Covers the sequence-wrap part of the XML_DATETIME_FIX_STRATEGY.md §7 test
plan: files _998, _999, _000, _001 ingest exactly once in readout-time
order; re-listed unchanged files are skipped without download; a wrapped
counter reusing a filename is detected as new content; gaps across the
wrap are reported modulo 1000.
"""

import datetime as dt

import pytest

from custom_components.energy_meter_izar.ingest import (
    IngestionTracker,
    SequenceWatcher,
    filename_sequence,
    is_wrap,
    sequence_gap,
)


def test_filename_sequence():
    assert filename_sequence("0080A3DB81A5_042.xml") == 42
    assert filename_sequence("0080A3DB81A5_999.XML") == 999
    assert filename_sequence("0080A3DB81A5_000.xml") == 0
    assert filename_sequence("0080A3DB81A5.xml") is None
    assert filename_sequence("readme.txt") is None


@pytest.mark.parametrize(
    ("previous", "current", "gap", "wrapped"),
    [
        (642, 643, 0, False),  # consecutive, no wrap
        (997, 999, 1, False),  # one file missed
        (999, 0, 0, True),  # clean wrap
        (997, 3, 5, True),  # wrap with 5 files lost — not 994!
    ],
)
def test_sequence_gap_and_wrap(previous, current, gap, wrapped):
    assert sequence_gap(previous, current) == gap
    assert is_wrap(previous, current) == wrapped


def test_sequence_watcher_across_wrap():
    watcher = SequenceWatcher()
    observations = [watcher.observe(seq) for seq in (998, 999, 0, 1)]

    assert [o.missing for o in observations] == [0, 0, 0, 0]
    assert [o.wrapped for o in observations] == [False, False, True, False]
    assert [o.epoch for o in observations] == [0, 0, 1, 1]

    duplicate = watcher.observe(1)
    assert duplicate.duplicate is True
    assert duplicate.missing == 0

    # gap across a wrap: 997 → 003 reports 5 missing files
    gap_watcher = SequenceWatcher()
    gap_watcher.observe(997)
    observation = gap_watcher.observe(3)
    assert observation.missing == 5
    assert observation.wrapped is True

    restored = SequenceWatcher.from_dict(watcher.to_dict())
    assert restored.epoch == watcher.epoch
    assert restored.last_sequence == watcher.last_sequence


def _mbtime(day: int) -> dt.datetime:
    return dt.datetime(2026, 6, day, 0, 0)


def test_ingestion_simulation_across_wrap():
    """_998, _999, _000, _001 with advancing gateway MBTIMEs ingest exactly
    once, in time order — even though all four share mtime and size
    (verified real-world behavior: transfers rewrite mtimes identically)."""
    tracker = IngestionTracker()
    mtime, size = 1751960100.0, 228081
    listing = {
        "0080A3DB81A5_998.xml": _mbtime(4),
        "0080A3DB81A5_999.xml": _mbtime(5),
        "0080A3DB81A5_000.xml": _mbtime(6),
        "0080A3DB81A5_001.xml": _mbtime(7),
    }

    ingested = []
    # candidates ordered by decoded gateway MBTIME, never by filename
    for name, gateway_time in sorted(listing.items(), key=lambda item: item[1]):
        assert tracker.needs_download(name, mtime=mtime, size=size)
        assert tracker.is_new_content(name, gateway_time)
        tracker.mark_ingested(name, gateway_time, mtime=mtime, size=size)
        ingested.append(name)

    assert [n[-7:-4] for n in ingested] == ["998", "999", "000", "001"]
    assert tracker.last_readout_time == _mbtime(7)

    # the same listing reappears on the next poll: nothing is re-downloaded
    for name in listing:
        assert not tracker.needs_download(name, mtime=mtime, size=size)

    # _000 reappears with new mtime/size (counter wrapped again a year later):
    # download it, and the different gateway MBTIME marks it as new content
    reused_name = "0080A3DB81A5_000.xml"
    assert tracker.needs_download(reused_name, mtime=mtime + 999.0, size=size + 4)
    next_wrap_time = dt.datetime(2027, 5, 30, 0, 0)
    assert tracker.is_new_content(reused_name, next_wrap_time)
    tracker.mark_ingested(reused_name, next_wrap_time, mtime=mtime + 999.0, size=size + 4)
    assert tracker.last_readout_time == next_wrap_time

    # …but re-downloading a file with the same decoded MBTIME is not new
    assert not tracker.is_new_content(reused_name, next_wrap_time)


def test_unknown_metadata_forces_download():
    tracker = IngestionTracker()
    tracker.mark_ingested("a.xml", _mbtime(1), mtime=1.0, size=10)
    assert not tracker.needs_download("a.xml", mtime=1.0, size=10)
    assert tracker.needs_download("a.xml", mtime=2.0, size=10)
    assert tracker.needs_download("a.xml", mtime=1.0, size=11)
    assert tracker.needs_download("a.xml")  # no metadata → must download


def test_recent_files_map_is_bounded():
    tracker = IngestionTracker(max_entries=3)
    for day in range(1, 5):
        tracker.mark_ingested(f"file_{day}.xml", _mbtime(day), mtime=float(day), size=day)

    # oldest entry evicted → treated as unseen again (harmless: reading
    # store dedupe prevents double-counting)
    assert tracker.needs_download("file_1.xml", mtime=1.0, size=1)
    assert not tracker.needs_download("file_4.xml", mtime=4.0, size=4)


def test_tracker_state_roundtrip():
    tracker = IngestionTracker()
    tracker.mark_ingested("a.xml", _mbtime(1), mtime=1.0, size=10)
    tracker.mark_ingested("b.xml", _mbtime(2), mtime=2.0, size=20)

    restored = IngestionTracker.from_dict(tracker.to_dict())
    assert restored.last_readout_time == _mbtime(2)
    assert not restored.needs_download("a.xml", mtime=1.0, size=10)
    assert not restored.is_new_content("b.xml", _mbtime(2))
    assert restored.is_new_content("b.xml", _mbtime(3))

    empty = IngestionTracker.from_dict({})
    assert empty.last_readout_time is None
    assert empty.needs_download("a.xml", mtime=1.0, size=10)
