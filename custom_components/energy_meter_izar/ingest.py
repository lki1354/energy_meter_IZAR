"""Wrap-aware ingestion helpers for the gateway snapshot files.

The 3-digit counter in ``0080A3DB81A5_XXX.xml`` wraps after 999, so it is an
identifier — never a global ordering or progress key (see
``XML_DATETIME_FIX_STRATEGY.md`` §5). The authoritative order of files is the
decoded gateway ``MBTIME`` inside each file; sequence numbers are only used
modulo 1000 to report *gaps* (missed files) within one counter period.

Pure Python, no Home Assistant imports. Phase 2 wires `IngestionTracker`
state into a HA ``Store`` via ``to_dict``/``from_dict``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import OrderedDict
from dataclasses import dataclass

SEQUENCE_MODULUS = 1000
#: Keep roughly two counter periods so a wrapped counter reusing a filename
#: is still recognized as new content.
DEFAULT_MAX_TRACKED_FILES = 2000

_FILENAME_RE = re.compile(r"_(\d{3})\.xml$", re.IGNORECASE)


def filename_sequence(filename: str) -> int | None:
    """Extract the 3-digit sequence counter from a snapshot filename."""
    match = _FILENAME_RE.search(filename)
    return int(match.group(1)) if match else None


def sequence_gap(previous: int, current: int) -> int:
    """Number of files missed between two consecutive observed counters.

    Treats the counter as modulo 1000, so a wrap is not a huge gap:
    ``997 → 003`` reports 5 missing files (998, 999, 000, 001, 002).
    """
    return (current - previous - 1) % SEQUENCE_MODULUS


def is_wrap(previous: int, current: int) -> bool:
    """True when the counter passed 999 between the two observations."""
    return current < previous


@dataclass(frozen=True)
class SequenceObservation:
    sequence: int
    epoch: int
    missing: int
    wrapped: bool
    duplicate: bool


class SequenceWatcher:
    """Track (epoch, sequence) pairs across counter wraps for gap reporting.

    Feed it sequence numbers of files in readout-time order (i.e. ordered by
    decoded gateway MBTIME); it reports how many files were missed and when
    the counter wrapped. It must never be used to *skip or order* data.
    """

    def __init__(self, epoch: int = 0, last_sequence: int | None = None) -> None:
        self.epoch = epoch
        self.last_sequence = last_sequence

    def observe(self, sequence: int) -> SequenceObservation:
        if not 0 <= sequence < SEQUENCE_MODULUS:
            raise ValueError(f"sequence must be 0..{SEQUENCE_MODULUS - 1}, got {sequence}")
        last = self.last_sequence
        if last is None:
            self.last_sequence = sequence
            return SequenceObservation(sequence, self.epoch, 0, False, False)
        if sequence == last:
            return SequenceObservation(sequence, self.epoch, 0, False, True)
        missing = sequence_gap(last, sequence)
        wrapped = is_wrap(last, sequence)
        if wrapped:
            self.epoch += 1
        self.last_sequence = sequence
        return SequenceObservation(sequence, self.epoch, missing, wrapped, False)

    def to_dict(self) -> dict:
        return {"epoch": self.epoch, "last_sequence": self.last_sequence}

    @classmethod
    def from_dict(cls, data: dict) -> SequenceWatcher:
        return cls(epoch=data.get("epoch", 0), last_sequence=data.get("last_sequence"))


class IngestionTracker:
    """Exactly-once bookkeeping for snapshot files, safe across counter wraps.

    Two-stage decision per listed file:

    1. `needs_download` — cheap pre-filter: a filename already ingested with
       identical mtime *and* size is assumed unchanged and skipped without
       downloading. Any difference (or unknown metadata) forces a download,
       because transfers rewrite mtimes and sizes are near-identical.
    2. `is_new_content` — authoritative: after downloading, the decoded
       gateway MBTIME decides. Same filename with a different gateway MBTIME
       is *new* content (counter wrapped and reused the name) and must be
       re-ingested.

    ``recent_files`` is bounded (oldest ingested entries evicted) and the
    reading store dedupes on (device, quantity, timestamp), so eviction can
    cause a redundant re-parse but never double-counting.
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_TRACKED_FILES) -> None:
        self.max_entries = max_entries
        self.last_readout_time: dt.datetime | None = None
        self._recent_files: OrderedDict[str, dict] = OrderedDict()

    def needs_download(
        self, filename: str, *, mtime: float | None = None, size: int | None = None
    ) -> bool:
        entry = self._recent_files.get(filename)
        if entry is None:
            return True
        if mtime is None or size is None:
            return True
        return not (entry.get("mtime") == mtime and entry.get("size") == size)

    def is_new_content(self, filename: str, gateway_mbtime: dt.datetime) -> bool:
        entry = self._recent_files.get(filename)
        if entry is None:
            return True
        return entry["gateway_mbtime"] != gateway_mbtime.isoformat()

    def mark_ingested(
        self,
        filename: str,
        gateway_mbtime: dt.datetime,
        *,
        mtime: float | None = None,
        size: int | None = None,
    ) -> None:
        self._recent_files.pop(filename, None)
        self._recent_files[filename] = {
            "gateway_mbtime": gateway_mbtime.isoformat(),
            "mtime": mtime,
            "size": size,
        }
        while len(self._recent_files) > self.max_entries:
            self._recent_files.popitem(last=False)
        if self.last_readout_time is None or gateway_mbtime > self.last_readout_time:
            self.last_readout_time = gateway_mbtime

    def to_dict(self) -> dict:
        return {
            "last_readout_time": (
                self.last_readout_time.isoformat() if self.last_readout_time else None
            ),
            "recent_files": dict(self._recent_files),
        }

    @classmethod
    def from_dict(
        cls, data: dict, max_entries: int = DEFAULT_MAX_TRACKED_FILES
    ) -> IngestionTracker:
        tracker = cls(max_entries=max_entries)
        raw_time = data.get("last_readout_time")
        if raw_time:
            tracker.last_readout_time = dt.datetime.fromisoformat(raw_time)
        tracker._recent_files = OrderedDict(data.get("recent_files", {}))
        return tracker
