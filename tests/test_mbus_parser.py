"""Tests for the HC2XML / CP32 parser (phase 1 merge gate).

Covers the test plan of XML_DATETIME_FIX_STRATEGY.md §7: the §3.3 CP32
vectors (both century paths), the 2025→2026 year rollover that the original
decoder got wrong, leap days, IV/malformed input, and the slot-vs-telegram
timestamp cross-check on real gateway snapshot files.
"""

import datetime as dt
import xml.etree.ElementTree as ET

import pytest

from custom_components.energy_meter_izar import mbus_parser
from custom_components.energy_meter_izar.mbus_parser import (
    DEFAULT_DEVICE_MAP,
    decode_cp32,
    parse_snapshot_file,
    parse_snapshot_xml,
    read_gateway_mbtime,
)


@pytest.mark.parametrize(
    ("hex_string", "expected"),
    [
        # gateway MBTIME, hundred-year bits set
        ("00202637", dt.datetime(2025, 7, 6, 0, 0)),
        ("2D282537", dt.datetime(2025, 7, 5, 8, 45)),
        ("1E2F2537", dt.datetime(2025, 7, 5, 15, 30)),
        # 04 6D record inside telegram, hundred-year = 0 (heuristic path)
        ("0A002637", dt.datetime(2025, 7, 6, 0, 10)),
        # year rollover, minute before / after
        ("3B173F3C", dt.datetime(2025, 12, 31, 23, 59)),
        ("00004131", dt.datetime(2026, 1, 1, 0, 0)),
        # real gateway MBTIME of file _844 (hundred-year bits set)
        ("00204131", dt.datetime(2026, 1, 1, 0, 0)),
        # real gateway MBTIME of post-wrap file _001
        ("00204736", dt.datetime(2026, 6, 7, 0, 0)),
        ("1E084F31", dt.datetime(2026, 1, 15, 8, 30)),
        # leap day 2028-02-29 decodes
        ("00009D32", dt.datetime(2028, 2, 29, 0, 0)),
    ],
)
def test_decode_cp32_vectors(hex_string, expected):
    assert decode_cp32(hex_string) == expected


def test_year_rollover_is_monotonic():
    """Consecutive readings across New Year decode with increasing timestamps."""
    before = decode_cp32("3B173F3C")  # 2025-12-31 23:59
    after = decode_cp32("00004131")  # 2026-01-01 00:00
    assert before < after
    assert (before.year, after.year) == (2025, 2026)


def test_iv_bit_returns_none():
    assert decode_cp32("80202637") is None


@pytest.mark.parametrize(
    "bad",
    [
        "123",  # wrong length
        "00202637FF",  # wrong length
        "ZZ202637",  # not hex
        "",
        None,
    ],
)
def test_malformed_input_raises(bad):
    with pytest.raises(ValueError):
        decode_cp32(bad)


def test_invalid_date_raises():
    # 2027-02-29 does not exist
    with pytest.raises(ValueError):
        decode_cp32("00007D32")


# --- Snapshot parsing against the committed real gateway files -------------


def test_parse_fixture_665(fixtures_dir):
    """The meter_data_analyse sample decodes fully, with consistent times."""
    result = parse_snapshot_file(fixtures_dir / "0080A3DB81A5_665.xml")

    assert result.warnings == []
    assert result.slots_total > 0
    assert result.slots_decoded == result.slots_total
    assert result.gateway.device_id == "44610100"
    assert result.gateway.gateway_type == "60M"
    assert result.gateway.mbtime is not None
    assert result.gateway.mbtime.year == 2025

    # all 19 meters of the building inventory are present
    devices = {r.device_number for r in result.readings}
    assert devices == set(DEFAULT_DEVICE_MAP)

    # every reading carries a plausible 2025 timestamp
    assert all(r.timestamp.year == 2025 for r in result.readings)

    # cumulative electricity counter of the EG meter is a positive float
    eg_energy = [
        r
        for r in result.readings
        if r.device_number == 11601997 and r.quantity == "Energie 1 (Wh)"
    ]
    assert eg_energy
    assert all(isinstance(r.value, float) and r.value > 0 for r in eg_energy)
    assert all(r.medium == "electricity" and r.location == "EG" for r in eg_energy)


def test_parse_fixture_844_decodes_2026(fixtures_dir):
    """Regression for the hardcoded-2025 bug: file _844 is dated 2026."""
    result = parse_snapshot_file(fixtures_dir / "0080A3DB81A5_844.xml")

    assert result.warnings == []
    assert result.slots_decoded == result.slots_total
    assert result.gateway.mbtime == dt.datetime(2026, 1, 1, 0, 0)
    assert max(r.timestamp for r in result.readings).year == 2026


def test_gateway_mbtime_orders_files_across_wrap_and_year(fixtures_dir):
    """Decoded gateway MBTIME sequences the files; filenames must not."""
    expected = {
        "0080A3DB81A5_837.xml": dt.datetime(2025, 12, 25, 0, 0),
        "0080A3DB81A5_843.xml": dt.datetime(2025, 12, 31, 0, 0),
        "0080A3DB81A5_844.xml": dt.datetime(2026, 1, 1, 0, 0),
        "0080A3DB81A5_856.xml": dt.datetime(2026, 1, 13, 0, 0),
        "0080A3DB81A5_999.xml": dt.datetime(2026, 6, 5, 0, 0),
        "0080A3DB81A5_000.xml": dt.datetime(2026, 6, 6, 0, 0),
        "0080A3DB81A5_001.xml": dt.datetime(2026, 6, 7, 0, 0),
    }
    decoded = {name: read_gateway_mbtime(fixtures_dir / name) for name in expected}
    assert decoded == expected

    by_mbtime = sorted(expected, key=expected.get)
    assert [n[-7:-4] for n in by_mbtime] == ["837", "843", "844", "856", "999", "000", "001"]
    # lexicographic filename order would misorder the batch across the wrap
    assert sorted(expected) != by_mbtime


def test_cross_check_slot_vs_telegram_time(fixtures_dir):
    """The 04 6D record inside each telegram agrees with the slot MBTIME.

    This check would have caught the hardcoded-2025 bug immediately in
    January 2026: with the default ~2 h tolerance the real files are clean,
    while a near-zero tolerance flags the normal meter clock drift.
    """
    path = fixtures_dir / "0080A3DB81A5_844.xml"

    clean = parse_snapshot_file(path)
    assert not [w for w in clean.warnings if "diverges" in w]

    strict = parse_snapshot_file(path, mbtime_tolerance=dt.timedelta(0))
    assert [w for w in strict.warnings if "diverges" in w]


# --- Guard rails on synthetic snapshots -------------------------------------


def _snapshot_xml(slot_mbtime: str, mbtel: str, gateway_mbtime: str = "00204131") -> str:
    return f"""<?xml version="1.0"?>
    <HC2XML>
      <UNIT>
        <TYPE FORMAT="STRING">60M</TYPE>
        <MBADDRESS><MBDEVICEID FORMAT="MBUSHEX">44610100</MBDEVICEID></MBADDRESS>
        <MBTIME FORMAT="MBUSHEX">{gateway_mbtime}</MBTIME>
      </UNIT>
      <MEM>
        <T0000>
          <MBTIME>{slot_mbtime}</MBTIME>
          <ALMSTAT>00</ALMSTAT>
          <MBTEL>{mbtel}</MBTEL>
        </T0000>
      </MEM>
    </HC2XML>"""


def _first_real_mbtel(fixtures_dir) -> str:
    root = ET.parse(fixtures_dir / "0080A3DB81A5_844.xml").getroot()
    return root.find("MEM")[0].findtext("MBTEL")


def test_iv_flagged_slot_is_rejected(fixtures_dir):
    result = parse_snapshot_xml(_snapshot_xml("80204131", _first_real_mbtel(fixtures_dir)))
    assert result.readings == []
    assert result.slots_decoded == 0
    assert any("IV bit" in w for w in result.warnings)


def test_far_future_slot_is_rejected(fixtures_dir):
    # slot claims 2026-06-07 while the gateway header says 2026-01-01
    result = parse_snapshot_xml(_snapshot_xml("00204736", _first_real_mbtel(fixtures_dir)))
    assert result.readings == []
    assert any("ahead of" in w for w in result.warnings)


def test_unknown_meter_is_reported_once(fixtures_dir):
    xml = _snapshot_xml("00204131", _first_real_mbtel(fixtures_dir))
    result = parse_snapshot_xml(xml, device_map={})
    assert result.readings == []
    assert len([w for w in result.warnings if "unknown meter address" in w]) == 1


def test_undecodable_telegram_is_reported():
    result = parse_snapshot_xml(_snapshot_xml("00204131", "68C4C468FFFF16"))
    assert result.readings == []
    assert any("telegram" in w for w in result.warnings)


def test_module_has_no_homeassistant_dependency():
    """The parser must stay unit-testable standalone (STRATEGY.md §2.3)."""
    import inspect

    source = inspect.getsource(mbus_parser)
    assert "homeassistant" not in source
