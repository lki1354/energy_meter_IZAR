"""Pure-Python parser for HC2XML M-Bus gateway snapshot files.

Ported from ``meter_data_analyse/playground.py::xml_data_pars`` with the two
defects documented in ``XML_DATETIME_FIX_STRATEGY.md`` fixed:

* CP32 (EN 13757-3 type F) timestamps are decoded from the bit fields —
  including the year — instead of hardcoding 2025 (`decode_cp32`).
* Slot timestamps are cross-checked against the ``04 6D`` date/time record
  inside each telegram and against the gateway header time, so gross
  mis-datings surface as warnings instead of silently corrupting data.

No Home Assistant imports — this module is unit-testable standalone.
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import NamedTuple

import meterbus

DEFAULT_MBTIME_TOLERANCE = dt.timedelta(hours=2)
DEFAULT_MAX_FUTURE = dt.timedelta(hours=24)


def decode_cp32(hex_string: str) -> dt.datetime | None:
    """Decode an M-Bus CP32 (EN 13757-3 type F) timestamp, e.g. ``'00202637'``.

    Returns None when the sender flagged the timestamp invalid (IV bit).
    Raises ValueError on malformed input or out-of-range fields.
    """
    if not isinstance(hex_string, str) or len(hex_string) != 8:
        raise ValueError(f"CP32 must be 4 bytes / 8 hex chars, got {hex_string!r}")
    b1, b2, b3, b4 = (int(hex_string[i : i + 2], 16) for i in range(0, 8, 2))

    if b1 & 0x80:  # IV: sender marked the time invalid
        return None

    minute = b1 & 0x3F
    hour = b2 & 0x1F
    day = b3 & 0x1F
    month = b4 & 0x0F
    year = ((b3 & 0xE0) >> 5) | ((b4 & 0xF0) >> 1)
    hundred_year = (b2 & 0x60) >> 5

    if hundred_year:
        full_year = 1900 + 100 * hundred_year + year
    else:
        full_year = 2000 + year if year <= 80 else 1900 + year

    # datetime() rejects impossible dates (month 0, day 32, minute 60, …)
    return dt.datetime(full_year, month, day, hour, minute)


class TelegramLayout(Enum):
    """Record layout of a meter telegram (one per meter family)."""

    ELECTRICITY = "electricity"
    WATER = "water"
    HEAT = "heat"
    RESERVOIR = "reservoir"
    RESERVOIR_WW = "reservoir_ww"


class ValueKind(Enum):
    FLOAT = "float"
    INT = "int"
    DATETIME = "datetime"
    DATE = "date"
    TEXT = "text"


class QuantitySpec(NamedTuple):
    quantity: str
    unit: str
    kind: ValueKind


_F, _I, _DT, _D, _T = (
    ValueKind.FLOAT,
    ValueKind.INT,
    ValueKind.DATETIME,
    ValueKind.DATE,
    ValueKind.TEXT,
)

# Quantity names are kept identical to the polars column names of the
# original notebook pipeline so downstream billing logic ports 1:1.
_ELECTRICITY_SPECS = (
    QuantitySpec("Energie 1 (Wh)", "Wh", _F),
    QuantitySpec("Energie 2 (Wh)", "Wh", _F),
    QuantitySpec("Energie 3 (Wh)", "Wh", _F),
    QuantitySpec("Energie 4 (Wh)", "Wh", _F),
    QuantitySpec("Spannung 1 (V)", "V", _F),
    QuantitySpec("Strom 1 (A)", "A", _F),
    QuantitySpec("Leistung 1 (W)", "W", _I),
    QuantitySpec("Leistung 1.1 (W)", "W", _I),
    QuantitySpec("Spannung 2 (V)", "V", _F),
    QuantitySpec("Strom 2 (A)", "A", _F),
    QuantitySpec("Leistung 2 (W)", "W", _I),
    QuantitySpec("Leistung 2.1 (W)", "W", _I),
    QuantitySpec("Spannung 3 (V)", "V", _F),
    QuantitySpec("Strom 3 (A)", "A", _F),
    QuantitySpec("Leistung 3 (W)", "W", _I),
    QuantitySpec("Leistung 3.1 (W)", "W", _I),
    QuantitySpec("Herstellerspezifisch 1 (none)", "none", _F),
    QuantitySpec("Leistung 4 (W)", "W", _I),
    QuantitySpec("Leistung 5 (W)", "W", _I),
    QuantitySpec("Herstellerspezifisch 2 (none)", "none", _F),
)

_WATER_SPECS = (
    QuantitySpec("water_1 (m^3)", "m^3", _F),
    QuantitySpec("water_1 (date time)", "date time", _DT),
    QuantitySpec("water_1 (date)", "date", _D),
    QuantitySpec("water_2 (m^3)", "m^3", _F),
    QuantitySpec("water_2 (date)", "date", _D),
    QuantitySpec("water_customer_1 (none)", "none", _T),
    *(
        spec
        for n in range(3, 15)
        for spec in (
            QuantitySpec(f"water_{n} (date)", "date", _D),
            QuantitySpec(f"water_{n} (m^3)", "m^3", _F),
        )
    ),
)

_HEAT_SPECS = (
    QuantitySpec("heat_energy_1 (Wh)", "Wh", _I),
    QuantitySpec("heat_energy_2 (Wh)", "Wh", _I),
    QuantitySpec("heat_volume_1 (m^3)", "m^3", _F),
    QuantitySpec("heat_volume_2 (m^3)", "m^3", _F),
    QuantitySpec("SW version (none)", "none", _F),
    QuantitySpec("HW version (none)", "none", _F),
    QuantitySpec("Manufacturer 1 (none)", "none", _F),
    QuantitySpec("Manufacturer 2 (none)", "none", _F),
    QuantitySpec("manufacturer number (none)", "none", _F),
    QuantitySpec("moment 1 (date time)", "date time", _DT),
    QuantitySpec("moment 2 (date)", "date", _D),
    QuantitySpec("flow temperature (C)", "C", _F),
    QuantitySpec("return temperature (C)", "C", _F),
    QuantitySpec("flow rate (m^3/h)", "m^3/h", _F),
    QuantitySpec("power (W)", "W", _F),
    QuantitySpec("customem 1 (none)", "none", _F),
    QuantitySpec("on time (seconds)", "seconds", _I),
    QuantitySpec("customem 2 (none)", "none", _F),
)

_RESERVOIR_SPECS = (
    QuantitySpec("error (none)", "none", _I),
    QuantitySpec("duration (seconds)", "seconds", _I),
    QuantitySpec("moment (date time)", "date time", _DT),
    QuantitySpec("energy (Wh)", "Wh", _I),
    QuantitySpec("volume (m^3)", "m^3", _F),
    QuantitySpec("flow temperature (C)", "C", _F),
    QuantitySpec("return temperature (C)", "C", _F),
    QuantitySpec("flow rate (m^3/h)", "m^3/h", _F),
    QuantitySpec("power (W)", "W", _F),
    QuantitySpec("on time (seconds)", "seconds", _I),
    QuantitySpec("device number (none)", "none", _I),
)

_RESERVOIR_WW_SPECS = (
    QuantitySpec("error (none)", "none", _I),
    QuantitySpec("duration (seconds)", "seconds", _I),
    QuantitySpec("moment (date time)", "date time", _DT),
    QuantitySpec("energy (Wh)", "Wh", _I),
    QuantitySpec("volume (m^3)", "m^3", _F),
    QuantitySpec("info 1 (none)", "none", _I),
    QuantitySpec("volume 1 (m^3)", "m^3", _F),
    QuantitySpec("info 2 (none)", "none", _I),
    QuantitySpec("volume 2 (m^3)", "m^3", _F),
    QuantitySpec("flow temperature (C)", "C", _F),
    QuantitySpec("return temperature (C)", "C", _F),
    QuantitySpec("flow rate (m^3/h)", "m^3/h", _F),
    QuantitySpec("power (W)", "W", _F),
    QuantitySpec("on time (seconds)", "seconds", _I),
    QuantitySpec("device number (none)", "none", _I),
)

LAYOUT_SPECS: dict[TelegramLayout, tuple[QuantitySpec, ...]] = {
    TelegramLayout.ELECTRICITY: _ELECTRICITY_SPECS,
    TelegramLayout.WATER: _WATER_SPECS,
    TelegramLayout.HEAT: _HEAT_SPECS,
    TelegramLayout.RESERVOIR: _RESERVOIR_SPECS,
    TelegramLayout.RESERVOIR_WW: _RESERVOIR_WW_SPECS,
}


@dataclass(frozen=True)
class MeterDefinition:
    """A known physical meter: how to decode it and where it belongs."""

    number: int
    layout: TelegramLayout
    medium: str
    location: str


def _electricity(number: int, location: str) -> MeterDefinition:
    return MeterDefinition(number, TelegramLayout.ELECTRICITY, "electricity", location)


def _heat(number: int, location: str) -> MeterDefinition:
    return MeterDefinition(number, TelegramLayout.HEAT, "heat", location)


def _water(number: int, medium: str, location: str) -> MeterDefinition:
    return MeterDefinition(number, TelegramLayout.WATER, medium, location)


DEFAULT_DEVICE_MAP: dict[int, MeterDefinition] = {
    m.number: m
    for m in (
        _electricity(11601997, "EG"),
        _heat(2800001, "EG"),
        _water(2800002, "hot_water", "EG"),
        _water(2800003, "cold_water", "EG"),
        _electricity(11601989, "1.OG"),
        _heat(2801004, "1.OG"),
        _water(2801005, "hot_water", "1.OG"),
        _water(2801006, "cold_water", "1.OG"),
        _electricity(11601959, "DG"),
        _heat(2801007, "DG"),
        _water(2801008, "hot_water", "DG"),
        _water(2801009, "cold_water", "DG"),
        MeterDefinition(2891010, TelegramLayout.RESERVOIR, "reservoir_heating", "UG"),
        MeterDefinition(2891011, TelegramLayout.RESERVOIR_WW, "reservoir_hot_water", "UG"),
        _water(2891012, "hot_water", "UG"),
        _water(2891013, "cold_water", "UG"),
        _electricity(11601990, "Allgemein"),
        _electricity(11601992, "Wärmepumpe"),
        _electricity(11601893, "Photovoltaik"),
    )
}


@dataclass(frozen=True)
class MeterReading:
    """One decoded quantity of one meter at one readout instant."""

    device_number: int
    medium: str
    location: str
    timestamp: dt.datetime
    quantity: str
    value: float | int | str | dt.datetime | dt.date
    unit: str
    status: str


@dataclass(frozen=True)
class GatewayInfo:
    """Metadata from the ``<UNIT>`` header of a snapshot file."""

    device_id: str | None
    gateway_type: str | None
    mbtime: dt.datetime | None
    uptime_s: int | None
    bus_current_ma: int | None
    bus_voltage_v: float | None


@dataclass
class SnapshotParseResult:
    gateway: GatewayInfo
    readings: list[MeterReading] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    slots_total: int = 0
    slots_decoded: int = 0


def _hex_int(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _parse_gateway(unit_el: ET.Element | None, warnings: list[str]) -> GatewayInfo:
    if unit_el is None:
        warnings.append("snapshot has no <UNIT> header")
        return GatewayInfo(None, None, None, None, None, None)

    mbtime: dt.datetime | None = None
    mbtime_hex = unit_el.findtext("MBTIME")
    if mbtime_hex:
        try:
            mbtime = decode_cp32(mbtime_hex)
        except ValueError as err:
            warnings.append(f"gateway MBTIME {mbtime_hex!r} undecodable: {err}")
        else:
            if mbtime is None:
                warnings.append(f"gateway MBTIME {mbtime_hex!r} flagged invalid (IV bit)")

    voltage_raw = _hex_int(unit_el.findtext("VMBUS"))
    return GatewayInfo(
        device_id=unit_el.findtext("MBADDRESS/MBDEVICEID"),
        gateway_type=unit_el.findtext("TYPE"),
        mbtime=mbtime,
        uptime_s=_hex_int(unit_el.findtext("UPTIME")),
        bus_current_ma=_hex_int(unit_el.findtext("IMBUS")),
        bus_voltage_v=voltage_raw / 100 if voltage_raw is not None else None,  # 10 mV units
    )


def _telegram_address(telegram) -> int:
    id_nr = telegram.body.bodyHeader.id_nr
    return int("{:02X}{:02X}{:02X}{:02X}".format(*id_nr))


def _convert_value(raw, kind: ValueKind):
    if kind is ValueKind.FLOAT:
        return float(raw)
    if kind is ValueKind.INT:
        return int(raw)
    if kind is ValueKind.DATETIME:
        return dt.datetime.fromisoformat(raw)
    if kind is ValueKind.DATE:
        return dt.datetime.fromisoformat(raw).date()
    return str(raw)


def parse_snapshot(
    root: ET.Element,
    device_map: dict[int, MeterDefinition] | None = None,
    *,
    mbtime_tolerance: dt.timedelta = DEFAULT_MBTIME_TOLERANCE,
    max_future: dt.timedelta = DEFAULT_MAX_FUTURE,
) -> SnapshotParseResult:
    """Parse a full HC2XML snapshot into typed meter readings.

    Slots are skipped (with a warning on the result) when their MBTIME is
    IV-flagged/undecodable, lies more than ``max_future`` ahead of the gateway
    header time, the telegram fails to decode, or the meter is not in
    ``device_map``. A record whose M-Bus unit does not match the expected
    layout produces a warning and no reading. The telegram's own ``04 6D``
    date/time record is compared against the slot MBTIME and divergence
    beyond ``mbtime_tolerance`` is reported (meter clocks drift up to ~1h41
    ahead in the field, hence the generous default).
    """
    if device_map is None:
        device_map = DEFAULT_DEVICE_MAP

    gateway_warnings: list[str] = []
    result = SnapshotParseResult(gateway=_parse_gateway(root.find("UNIT"), gateway_warnings))
    result.warnings.extend(gateway_warnings)

    mem = root.find("MEM")
    if mem is None:
        result.warnings.append("snapshot has no <MEM> section")
        return result

    unknown_addresses: set[int] = set()
    for slot in mem:
        result.slots_total += 1
        tag = slot.tag
        mbtime_hex = slot.findtext("MBTIME")
        mbtel_hex = slot.findtext("MBTEL")
        if not mbtime_hex or not mbtel_hex:
            result.warnings.append(f"{tag}: missing MBTIME or MBTEL")
            continue

        try:
            slot_time = decode_cp32(mbtime_hex)
        except ValueError as err:
            result.warnings.append(f"{tag}: MBTIME {mbtime_hex!r} undecodable: {err}")
            continue
        if slot_time is None:
            result.warnings.append(f"{tag}: MBTIME {mbtime_hex!r} flagged invalid (IV bit)")
            continue
        if result.gateway.mbtime is not None and slot_time > result.gateway.mbtime + max_future:
            result.warnings.append(
                f"{tag}: slot time {slot_time} is more than {max_future} ahead of "
                f"gateway time {result.gateway.mbtime}; rejecting slot"
            )
            continue

        try:
            telegram = meterbus.load(bytes.fromhex(mbtel_hex))
        except Exception as err:  # noqa: BLE001 - pymeterbus raises various types
            result.warnings.append(f"{tag}: telegram decode failed: {err}")
            continue

        try:
            address = _telegram_address(telegram)
        except Exception as err:  # noqa: BLE001
            result.warnings.append(f"{tag}: telegram has no readable address: {err}")
            continue

        definition = device_map.get(address)
        if definition is None:
            if address not in unknown_addresses:
                unknown_addresses.add(address)
                result.warnings.append(f"unknown meter address {address} (first seen in {tag})")
            continue

        status = f"{telegram.body.bodyHeader.status_field}"
        specs = LAYOUT_SPECS[definition.layout]
        telegram_time: dt.datetime | None = None
        # telegrams may carry more records than the spec (trailing None
        # record) or fewer (older firmware) — decode the overlapping prefix
        for record, spec in zip(telegram.records, specs, strict=False):
            if record.unit != spec.unit:
                result.warnings.append(
                    f"{tag}: device {address} record unit {record.unit!r} does not match "
                    f"expected {spec.unit!r} for {spec.quantity!r}; skipping record"
                )
                continue
            try:
                value = _convert_value(record.value, spec.kind)
            except (TypeError, ValueError) as err:
                result.warnings.append(
                    f"{tag}: device {address} value {record.value!r} for "
                    f"{spec.quantity!r} unconvertible: {err}"
                )
                continue
            if telegram_time is None and spec.kind is ValueKind.DATETIME:
                telegram_time = value
            result.readings.append(
                MeterReading(
                    device_number=address,
                    medium=definition.medium,
                    location=definition.location,
                    timestamp=slot_time,
                    quantity=spec.quantity,
                    value=value,
                    unit=spec.unit,
                    status=status,
                )
            )

        # Cross-validation (defense in depth): the telegram's own 04 6D
        # date/time record must agree with the slot MBTIME — this catches
        # gross decoding errors like the original hardcoded-2025 bug.
        if telegram_time is not None and abs(telegram_time - slot_time) > mbtime_tolerance:
            result.warnings.append(
                f"{tag}: device {address} telegram time {telegram_time} diverges from "
                f"slot MBTIME {slot_time} by more than {mbtime_tolerance}"
            )
        result.slots_decoded += 1

    return result


def parse_snapshot_file(
    path: str | Path,
    device_map: dict[int, MeterDefinition] | None = None,
    *,
    mbtime_tolerance: dt.timedelta = DEFAULT_MBTIME_TOLERANCE,
    max_future: dt.timedelta = DEFAULT_MAX_FUTURE,
) -> SnapshotParseResult:
    """Parse an HC2XML snapshot file from disk."""
    root = ET.parse(path).getroot()
    return parse_snapshot(
        root, device_map, mbtime_tolerance=mbtime_tolerance, max_future=max_future
    )


def parse_snapshot_xml(
    xml_text: str | bytes,
    device_map: dict[int, MeterDefinition] | None = None,
    *,
    mbtime_tolerance: dt.timedelta = DEFAULT_MBTIME_TOLERANCE,
    max_future: dt.timedelta = DEFAULT_MAX_FUTURE,
) -> SnapshotParseResult:
    """Parse an HC2XML snapshot from an in-memory XML document."""
    root = ET.fromstring(xml_text)
    return parse_snapshot(
        root, device_map, mbtime_tolerance=mbtime_tolerance, max_future=max_future
    )


def read_gateway_mbtime(path: str | Path) -> dt.datetime | None:
    """Decode only the gateway header ``<MBTIME>`` of a snapshot file.

    This is the authoritative ordering/progress key for ingestion — the file
    name counter wraps after 999 and file mtimes are rewritten by transfers,
    so the readout time exists only inside the file content. Returns None
    when the header is missing or IV-flagged.
    """
    for _, element in ET.iterparse(path, events=("end",)):
        if element.tag == "UNIT":
            mbtime_hex = element.findtext("MBTIME")
            if not mbtime_hex:
                return None
            return decode_cp32(mbtime_hex)
    return None
