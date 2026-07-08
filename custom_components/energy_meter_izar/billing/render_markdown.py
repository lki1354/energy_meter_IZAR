"""Markdown bill renderer — tables like the manual notebook bills."""

from __future__ import annotations

from .config import (
    SECTION_ELECTRICITY,
    SECTION_HEATING,
    SECTION_HOT_WATER,
    SECTION_SUMMARY,
    SECTION_WATER_VOLUME,
    Profile,
)
from .engine import BillResult, LineItem, UnitBill
from .labels import label_for_line, labels_for

_DETAIL_SECTIONS = (SECTION_ELECTRICITY, SECTION_HEATING, SECTION_HOT_WATER)


def _money(value: float) -> str:
    return f"{value:.2f}"


def _qty(value: float) -> str:
    return f"{value:.2f}"


def _price(value: float | None, mixed_label: str) -> str:
    return mixed_label if value is None else f"{value:.4f}"


def _line_row(line: LineItem, text: dict[str, str], currency: str) -> str:
    label = label_for_line(line, text)
    if line.measured is not None:
        label += f" ({_qty(line.measured)} {line.measured_unit} {text['measured']})"
    return (
        f"| {label} | {_qty(line.quantity)} {line.quantity_unit} "
        f"| {_price(line.price, text['mixed'])} | {_money(line.cost)} {currency} |"
    )


def _unit_details(
    unit: UnitBill, sections: tuple[str, ...], text: dict[str, str], currency: str
) -> list[str]:
    included = [line for line in unit.lines if line.section in sections]
    out = [
        f"## {unit.name}",
        "",
        f"| {text['position']} | {text['quantity']} | {text['price']} | {text['cost']} |",
        "|---|---|---|---|",
    ]
    out.extend(_line_row(line, text, currency) for line in included)
    subtotal = sum(line.cost for line in included)
    out.append(f"| **{text['total']} {unit.name}** | | | **{_money(subtotal)} {currency}** |")
    out.append("")
    return out


def _summary_table(result: BillResult, text: dict[str, str]) -> list[str]:
    currency = result.currency
    out = [
        f"## {text['summary']}",
        "",
        f"| {text['unit']} | {text['electricity']} | {text['heating']} "
        f"| {text['hot_water']} | **{text['total']}** |",
        "|---|---|---|---|---|",
    ]
    totals = [0.0, 0.0, 0.0, 0.0]
    for unit in result.units.values():
        electricity = unit.section_total(SECTION_ELECTRICITY)
        heating = unit.section_total(SECTION_HEATING)
        hot_water = unit.section_total(SECTION_HOT_WATER)
        totals[0] += electricity
        totals[1] += heating
        totals[2] += hot_water
        totals[3] += unit.total
        out.append(
            f"| {unit.name} | {_money(electricity)} | {_money(heating)} "
            f"| {_money(hot_water)} | **{_money(unit.total)} {currency}** |"
        )
    out.append(
        f"| **{text['total']}** | **{_money(totals[0])}** | **{_money(totals[1])}** "
        f"| **{_money(totals[2])}** | **{_money(totals[3])} {currency}** |"
    )
    out.append("")
    return out


def _water_table(result: BillResult, text: dict[str, str]) -> list[str]:
    out = [
        f"## {text['water_volume']}",
        "",
        f"| {text['unit']} | {text['hot_water_m3']} | {text['cold_water_m3']} |",
        "|---|---|---|",
    ]
    hot_total = cold_total = 0.0
    for name, volume in result.water.items():
        hot_total += volume.hot_m3
        cold_total += volume.cold_m3
        out.append(f"| {name} | {volume.hot_m3:.3f} | {volume.cold_m3:.3f} |")
    out.append(f"| **{text['total']}** | **{hot_total:.3f}** | **{cold_total:.3f}** |")
    out.append("")
    return out


def _footer(result: BillResult, text: dict[str, str]) -> list[str]:
    out = ["---", "", f"### {text['basis']}", ""]
    for name, price in result.tariff_prices.items():
        out.append(f"- {text['tariff']} {name}: {price:.4f} {result.currency}/kWh")
    if result.pv_price_kwh is not None:
        out.append(f"- {text['pv_price']}: {result.pv_price_kwh:.4f} {result.currency}/kWh")
    if result.meta.pv_production_kwh:
        out.append(f"- {text['pv_production']}: {result.meta.pv_production_kwh:.2f} kWh")
        out.append(
            f"- {text['pv_self_consumed']}: {result.meta.pv_self_consumed_kwh:.2f} kWh"
        )
    if result.meta.heating_share is not None:
        source = text[f"share_{result.meta.heating_share_source}"]
        out.append(
            f"- {text['heating_share']}: {result.meta.heating_share:.1%} ({source})"
        )
    for note in result.meta.notes:
        out.append(f"- ⚠️ {note}")
    out.append("")
    out.append(f"*{text['generated']}: {result.generated_at:%Y-%m-%d %H:%M}*")
    out.append("")
    return out


def render_markdown(result: BillResult, profile: Profile) -> str:
    """Render the bill to a Markdown document."""
    text = labels_for(profile.language)
    end_inclusive = result.end.date()
    lines = [
        f"# {text['title']}",
        "",
        f"**{text['period']}**: {result.start.date()} – {end_inclusive} "
        f"({text['end_exclusive']})",
        "",
    ]
    detail_sections = tuple(s for s in profile.sections if s in _DETAIL_SECTIONS)
    if detail_sections:
        for unit in result.units.values():
            lines.extend(_unit_details(unit, detail_sections, text, result.currency))
    if SECTION_SUMMARY in profile.sections:
        lines.extend(_summary_table(result, text))
    if SECTION_WATER_VOLUME in profile.sections:
        lines.extend(_water_table(result, text))
    lines.extend(_footer(result, text))
    return "\n".join(lines)
