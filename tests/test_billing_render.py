"""Tests for the Markdown, CSV, and PDF bill renderers."""

import csv
import datetime as dt
import io

import pytest
from pypdf import PdfReader

from custom_components.energy_meter_izar.billing import render_bill
from custom_components.energy_meter_izar.billing.config import Profile
from custom_components.energy_meter_izar.billing.engine import (
    BillMeta,
    BillResult,
    LineItem,
    UnitBill,
    WaterVolume,
)
from custom_components.energy_meter_izar.billing.render_csv import render_csv
from custom_components.energy_meter_izar.billing.render_markdown import render_markdown
from custom_components.energy_meter_izar.billing.render_pdf import render_pdf


def _result() -> BillResult:
    a = UnitBill(name="EG")
    a.lines = [
        LineItem("electricity", "grid", "hochtarif", 100.0, "kWh", 0.2218, 22.18),
        LineItem("electricity", "grid", "niedertarif", 50.0, "kWh", 0.2028, 10.14),
        LineItem("electricity", "pv", None, 30.0, "kWh", 0.088, 2.64),
        LineItem("electricity", "common_grid", None, 10.0, "kWh", None, 2.10),
        LineItem("electricity", "common_pv", None, 5.0, "kWh", 0.088, 0.44),
        LineItem(
            "heating", "heating", None, 200.0, "kWh", 0.11, 22.0,
            measured=1500.0, measured_unit="kWh",
        ),
        LineItem(
            "hot_water", "hot_water", None, 80.0, "kWh", 0.12, 9.6,
            measured=12.5, measured_unit="m³",
        ),
    ]
    return BillResult(
        start=dt.datetime(2026, 1, 1),
        end=dt.datetime(2026, 4, 1),
        currency="CHF",
        tariff_prices={"hochtarif": 0.2218, "niedertarif": 0.2028},
        pv_price_kwh=0.088,
        units={"EG": a},
        water={"EG": WaterVolume(hot_m3=12.5, cold_m3=30.25)},
        meta=BillMeta(
            intervals=100,
            house_consumption_kwh=500.0,
            pv_production_kwh=120.0,
            pv_self_consumed_kwh=100.0,
            pv_exported_kwh=20.0,
            heating_share=0.6,
            heating_share_source="reservoir_ratio",
            heat_pump_kwh=280.0,
            heat_pump_cost=44.0,
        ),
        generated_at=dt.datetime(2026, 7, 8, 12, 0),
    )


FULL_PROFILE = Profile(
    name="quarterly_full",
    sections=("electricity", "heating", "hot_water", "summary"),
    language="de",
)


def test_markdown_full_bill_german():
    text = render_markdown(_result(), FULL_PROFILE)
    assert "# Energieabrechnung" in text
    assert "**Zeitraum**: 2026-01-01 – 2026-04-01" in text
    assert "## EG" in text
    assert "| Strom hochtarif (Netz) | 100.00 kWh | 0.2218 | 22.18 CHF |" in text
    assert "| Strom Photovoltaik | 30.00 kWh | 0.0880 | 2.64 CHF |" in text
    assert "| Allgemeinstrom (Anteil, Netz) | 10.00 kWh | gemischt | 2.10 CHF |" in text
    assert "Heizung (1500.00 kWh gemessen)" in text
    assert "Warmwasser (12.50 m³ gemessen)" in text
    # unit total = 22.18+10.14+2.64+2.10+0.44+22.0+9.6 = 69.10
    assert "**69.10 CHF**" in text
    # summary table + audit footer
    assert "## Zusammenfassung" in text
    assert "Heizungsanteil Wärmepumpe: 60.0%" in text
    assert "PV-Produktion: 120.00 kWh" in text


def test_markdown_english_labels():
    profile = Profile(name="p", sections=("electricity", "summary"), language="en")
    text = render_markdown(_result(), profile)
    assert "# Energy bill" in text
    assert "Electricity hochtarif (grid)" in text
    assert "## Summary" in text
    # heating/hot-water detail lines (with measured values) are excluded
    assert "measured" not in text


def test_markdown_water_only_has_no_cost_tables():
    profile = Profile(name="water_only", sections=("water_volume",), language="de")
    text = render_markdown(_result(), profile)
    assert "## Wasserverbrauch" in text
    assert "| EG | 12.500 | 30.250 |" in text
    assert "Strom" not in text.split("Berechnungsgrundlage")[0]


def test_csv_rows_and_totals():
    text = render_csv(_result(), FULL_PROFILE)
    rows = list(csv.reader(io.StringIO(text)))
    header, body = rows[0], rows[1:]
    assert header[0] == "unit"
    # 7 line items + unit total + grand total
    assert len(body) == 9
    grid = next(row for row in body if row[2] == "grid_hochtarif")
    assert grid[0] == "EG"
    assert float(grid[4]) == pytest.approx(100.0)
    assert float(grid[8]) == pytest.approx(0.2218)
    assert float(grid[9]) == pytest.approx(22.18)
    common = next(row for row in body if row[2] == "common_grid")
    assert common[8] == ""  # mixed price
    heating = next(row for row in body if row[2] == "heating")
    assert float(heating[6]) == pytest.approx(1500.0)
    total = next(row for row in body if row[2] == "total")
    assert float(total[9]) == pytest.approx(69.10)
    grand = next(row for row in body if row[2] == "grand_total")
    assert float(grand[9]) == pytest.approx(69.10)


def test_csv_water_volume_section():
    profile = Profile(name="water_only", sections=("water_volume",), language="de")
    rows = list(csv.reader(io.StringIO(render_csv(_result(), profile))))
    body = rows[1:]
    assert len(body) == 2
    hot = next(row for row in body if row[2] == "hot_water_volume")
    assert float(hot[4]) == pytest.approx(12.5)


def _pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() for page in reader.pages)


def test_pdf_full_bill_german():
    data = render_pdf(_result(), FULL_PROFILE)
    assert data.startswith(b"%PDF-")
    text = _pdf_text(data)
    assert "Energieabrechnung" in text
    assert "2026-01-01 - 2026-04-01" in text
    assert "Strom hochtarif (Netz)" in text
    assert "22.18 CHF" in text
    # unit total = 22.18+10.14+2.64+2.10+0.44+22.0+9.6 = 69.10
    assert "69.10 CHF" in text
    assert "Zusammenfassung" in text
    assert "Heizungsanteil Wärmepumpe: 60.0%" in text
    assert "PV-Produktion: 120.00 kWh" in text


def test_pdf_water_only_english():
    profile = Profile(name="water_only", sections=("water_volume",), language="en")
    text = _pdf_text(render_pdf(_result(), profile))
    assert "Water consumption" in text
    assert "12.500" in text
    assert "30.250" in text
    assert "hochtarif (grid)" not in text


def test_pdf_survives_non_latin1_notes():
    result = _result()
    result.meta.notes.append("Zähler 2800001 → reset erkannt — Werte geprüft ⚠️")
    text = _pdf_text(render_pdf(result, FULL_PROFILE))
    # en dash / warning sign are substituted, the rest survives
    assert "Werte geprüft" in text


def test_render_bill_dispatch():
    assert render_bill(_result(), FULL_PROFILE, "markdown").startswith("# ")
    assert render_bill(_result(), FULL_PROFILE, "csv").startswith("unit,")
    assert render_bill(_result(), FULL_PROFILE, "pdf").startswith(b"%PDF-")
    with pytest.raises(ValueError, match="docx"):
        render_bill(_result(), FULL_PROFILE, "docx")
