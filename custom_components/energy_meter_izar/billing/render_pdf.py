"""PDF bill renderer via ``fpdf2`` (pure Python, no system dependencies).

The layout mirrors the Markdown renderer: per-unit cost tables, summary,
water volumes, and the calculation-basis footer. Uses the built-in
Helvetica core font, so text is limited to Latin-1 — plenty for the de/en
bill languages; anything outside degrades to a replacement character
instead of failing the export.
"""

from __future__ import annotations

from fpdf import FPDF
from fpdf.fonts import FontFace

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

_FONT = "Helvetica"
_BOLD = FontFace(emphasis="BOLD")
_TABLE_STYLE = {
    "borders_layout": "HORIZONTAL_LINES",
    "line_height": 6,
    "padding": 1,
}

#: Characters common in the labels/notes that Latin-1 cannot represent.
_SUBSTITUTIONS = str.maketrans({"–": "-", "—": "-", "⚠": "(!)", "️": ""})


def _latin1(text: str) -> str:
    return text.translate(_SUBSTITUTIONS).encode("latin-1", "replace").decode("latin-1")


def _money(value: float) -> str:
    return f"{value:.2f}"


def _qty(value: float) -> str:
    return f"{value:.2f}"


def _price(value: float | None, mixed_label: str) -> str:
    return mixed_label if value is None else f"{value:.4f}"


class _BillPDF(FPDF):
    def footer(self) -> None:
        self.set_y(-12)
        self.set_font(_FONT, size=8)
        self.set_text_color(120)
        self.cell(0, 6, f"{self.page_no()}/{{nb}}", align="C")
        self.set_text_color(0)


def _heading(pdf: FPDF, text: str, size: int) -> None:
    pdf.set_font(_FONT, style="B", size=size)
    pdf.cell(0, 8, _latin1(text), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_font(_FONT, size=9)


def _table(pdf: FPDF, header: tuple[str, ...], rows: list[tuple[bool, tuple[str, ...]]],
           col_widths: tuple[int, ...], text_align: tuple[str, ...]) -> None:
    """One table: ``rows`` are ``(bold, cells)``; the header row is bold."""
    with pdf.table(col_widths=col_widths, text_align=text_align, **_TABLE_STYLE) as table:
        head = table.row()
        for cell in header:
            head.cell(_latin1(cell))
        for bold, cells in rows:
            row = table.row()
            for cell in cells:
                row.cell(_latin1(cell), style=_BOLD if bold else None)
    pdf.ln(4)


def _line_cells(line: LineItem, text: dict[str, str], currency: str) -> tuple[str, ...]:
    label = label_for_line(line, text)
    if line.measured is not None:
        label += f" ({_qty(line.measured)} {line.measured_unit} {text['measured']})"
    return (
        label,
        f"{_qty(line.quantity)} {line.quantity_unit}",
        _price(line.price, text["mixed"]),
        f"{_money(line.cost)} {currency}",
    )


def _unit_details(
    pdf: FPDF, unit: UnitBill, sections: tuple[str, ...],
    text: dict[str, str], currency: str,
) -> None:
    included = [line for line in unit.lines if line.section in sections]
    _heading(pdf, unit.name, 13)
    rows: list[tuple[bool, tuple[str, ...]]] = [
        (False, _line_cells(line, text, currency)) for line in included
    ]
    subtotal = sum(line.cost for line in included)
    rows.append(
        (True, (f"{text['total']} {unit.name}", "", "", f"{_money(subtotal)} {currency}"))
    )
    _table(
        pdf,
        (text["position"], text["quantity"], text["price"], text["cost"]),
        rows,
        col_widths=(52, 18, 12, 18),
        text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT"),
    )


def _summary_table(pdf: FPDF, result: BillResult, text: dict[str, str]) -> None:
    currency = result.currency
    _heading(pdf, text["summary"], 13)
    totals = [0.0, 0.0, 0.0, 0.0]
    rows: list[tuple[bool, tuple[str, ...]]] = []
    for unit in result.units.values():
        electricity = unit.section_total(SECTION_ELECTRICITY)
        heating = unit.section_total(SECTION_HEATING)
        hot_water = unit.section_total(SECTION_HOT_WATER)
        totals[0] += electricity
        totals[1] += heating
        totals[2] += hot_water
        totals[3] += unit.total
        rows.append(
            (False, (unit.name, _money(electricity), _money(heating),
                     _money(hot_water), f"{_money(unit.total)} {currency}"))
        )
    rows.append(
        (True, (text["total"], _money(totals[0]), _money(totals[1]),
                _money(totals[2]), f"{_money(totals[3])} {currency}"))
    )
    _table(
        pdf,
        (text["unit"], text["electricity"], text["heating"], text["hot_water"],
         text["total"]),
        rows,
        col_widths=(28, 18, 18, 18, 18),
        text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT", "RIGHT"),
    )


def _water_table(pdf: FPDF, result: BillResult, text: dict[str, str]) -> None:
    _heading(pdf, text["water_volume"], 13)
    hot_total = cold_total = 0.0
    rows: list[tuple[bool, tuple[str, ...]]] = []
    for name, volume in result.water.items():
        hot_total += volume.hot_m3
        cold_total += volume.cold_m3
        rows.append((False, (name, f"{volume.hot_m3:.3f}", f"{volume.cold_m3:.3f}")))
    rows.append((True, (text["total"], f"{hot_total:.3f}", f"{cold_total:.3f}")))
    _table(
        pdf,
        (text["unit"], text["hot_water_m3"], text["cold_water_m3"]),
        rows,
        col_widths=(40, 30, 30),
        text_align=("LEFT", "RIGHT", "RIGHT"),
    )


def _footer_section(pdf: FPDF, result: BillResult, text: dict[str, str]) -> None:
    _heading(pdf, text["basis"], 11)
    lines = [
        f"- {text['tariff']} {name}: {price:.4f} {result.currency}/kWh"
        for name, price in result.tariff_prices.items()
    ]
    if result.pv_price_kwh is not None:
        lines.append(f"- {text['pv_price']}: {result.pv_price_kwh:.4f} {result.currency}/kWh")
    if result.meta.pv_production_kwh:
        lines.append(f"- {text['pv_production']}: {result.meta.pv_production_kwh:.2f} kWh")
        lines.append(
            f"- {text['pv_self_consumed']}: {result.meta.pv_self_consumed_kwh:.2f} kWh"
        )
    if result.meta.heating_share is not None:
        source = text[f"share_{result.meta.heating_share_source}"]
        lines.append(f"- {text['heating_share']}: {result.meta.heating_share:.1%} ({source})")
    lines.extend(f"- (!) {note}" for note in result.meta.notes)
    for line in lines:
        pdf.multi_cell(0, 5, _latin1(line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font(_FONT, style="I", size=8)
    pdf.cell(0, 5, _latin1(f"{text['generated']}: {result.generated_at:%Y-%m-%d %H:%M}"))


def render_pdf(result: BillResult, profile: Profile) -> bytes:
    """Render the bill to a PDF document."""
    text = labels_for(profile.language)
    pdf = _BillPDF(format="A4")
    pdf.set_title(_latin1(text["title"]))
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    pdf.set_font(_FONT, style="B", size=16)
    pdf.cell(0, 10, _latin1(text["title"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(_FONT, size=10)
    end_inclusive = result.end.date()
    pdf.cell(
        0, 6,
        _latin1(
            f"{text['period']}: {result.start.date()} - {end_inclusive} "
            f"({text['end_exclusive']})"
        ),
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(4)
    pdf.set_font(_FONT, size=9)

    detail_sections = tuple(s for s in profile.sections if s in _DETAIL_SECTIONS)
    if detail_sections:
        for unit in result.units.values():
            _unit_details(pdf, unit, detail_sections, text, result.currency)
    if SECTION_SUMMARY in profile.sections:
        _summary_table(pdf, result, text)
    if SECTION_WATER_VOLUME in profile.sections:
        _water_table(pdf, result, text)
    _footer_section(pdf, result, text)
    return bytes(pdf.output())
