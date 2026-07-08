"""CSV bill renderer — one row per unit × line item, spreadsheet-friendly.

Column headers are fixed English identifiers so exported files import
consistently regardless of the profile language; the ``label`` column
carries the translated position name.
"""

from __future__ import annotations

import csv
import io

from .config import (
    SECTION_ELECTRICITY,
    SECTION_HEATING,
    SECTION_HOT_WATER,
    SECTION_SUMMARY,
    SECTION_WATER_VOLUME,
    Profile,
)
from .engine import BillResult
from .labels import label_for_line, labels_for

_HEADER = [
    "unit",
    "section",
    "item",
    "label",
    "quantity",
    "quantity_unit",
    "measured",
    "measured_unit",
    "price_per_unit",
    "cost",
    "currency",
]

_LINE_SECTIONS = (SECTION_ELECTRICITY, SECTION_HEATING, SECTION_HOT_WATER)


def _num(value: float | None, digits: int = 6) -> str:
    return "" if value is None else f"{round(value, digits):g}"


def render_csv(result: BillResult, profile: Profile) -> str:
    """Render the bill to CSV text."""
    text = labels_for(profile.language)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_HEADER)

    sections = tuple(s for s in profile.sections if s in _LINE_SECTIONS)
    for unit in result.units.values():
        included = [line for line in unit.lines if line.section in sections]
        for line in included:
            writer.writerow(
                [
                    unit.name,
                    line.section,
                    line.kind + (f"_{line.detail}" if line.detail else ""),
                    label_for_line(line, text),
                    _num(line.quantity),
                    line.quantity_unit,
                    _num(line.measured),
                    line.measured_unit or "",
                    _num(line.price),
                    _num(line.cost),
                    result.currency,
                ]
            )
        if included:
            subtotal = sum(line.cost for line in included)
            writer.writerow(
                [unit.name, "total", "total", text["total"], "", "", "", "", "",
                 _num(subtotal), result.currency]
            )

    if SECTION_WATER_VOLUME in profile.sections:
        for name, volume in result.water.items():
            writer.writerow(
                [name, SECTION_WATER_VOLUME, "hot_water_volume",
                 text["hot_water_m3"], _num(volume.hot_m3), "m^3", "", "", "", "",
                 result.currency]
            )
            writer.writerow(
                [name, SECTION_WATER_VOLUME, "cold_water_volume",
                 text["cold_water_m3"], _num(volume.cold_m3), "m^3", "", "", "", "",
                 result.currency]
            )

    if SECTION_SUMMARY in profile.sections and sections:
        writer.writerow(
            ["*", "summary", "grand_total", text["total"], "", "", "", "", "",
             _num(result.total), result.currency]
        )
    return buffer.getvalue()
