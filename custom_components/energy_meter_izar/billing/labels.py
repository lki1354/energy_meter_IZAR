"""Renderer label translations (de/en) shared by all output formats."""

from __future__ import annotations

from .engine import (
    KIND_COMMON_GRID,
    KIND_COMMON_PV,
    KIND_GRID,
    KIND_HEATING,
    KIND_HOT_WATER,
    KIND_PV,
    LineItem,
)

LABELS: dict[str, dict[str, str]] = {
    "de": {
        "title": "Energieabrechnung",
        "period": "Zeitraum",
        "end_exclusive": "Ende exklusiv",
        "position": "Position",
        "quantity": "Menge",
        "price": "Preis",
        "cost": "Kosten",
        "total": "Gesamt",
        "unit": "Wohnung",
        "mixed": "gemischt",
        "measured": "gemessen",
        "electricity": "Strom",
        "heating": "Heizung",
        "hot_water": "Warmwasser",
        "summary": "Zusammenfassung",
        "water_volume": "Wasserverbrauch",
        "hot_water_m3": "Warmwasser (m³)",
        "cold_water_m3": "Kaltwasser (m³)",
        "basis": "Berechnungsgrundlage",
        "tariff": "Tarif",
        "pv_price": "PV-Eigenverbrauch",
        "pv_production": "PV-Produktion",
        "pv_self_consumed": "PV-Eigenverbrauch (Energie)",
        "heating_share": "Heizungsanteil Wärmepumpe",
        "share_reservoir_ratio": "aus Speicher-Energieabgabe",
        "share_fallback": "Fallback-Anteil, keine Speicherdaten",
        "generated": "Erstellt",
        KIND_GRID: "Strom {detail} (Netz)",
        KIND_PV: "Strom Photovoltaik",
        KIND_COMMON_GRID: "Allgemeinstrom (Anteil, Netz)",
        KIND_COMMON_PV: "Allgemeinstrom (Anteil, PV)",
        KIND_HEATING: "Heizung",
        KIND_HOT_WATER: "Warmwasser",
    },
    "en": {
        "title": "Energy bill",
        "period": "Period",
        "end_exclusive": "end exclusive",
        "position": "Item",
        "quantity": "Quantity",
        "price": "Price",
        "cost": "Cost",
        "total": "Total",
        "unit": "Unit",
        "mixed": "mixed",
        "measured": "measured",
        "electricity": "Electricity",
        "heating": "Heating",
        "hot_water": "Hot water",
        "summary": "Summary",
        "water_volume": "Water consumption",
        "hot_water_m3": "Hot water (m³)",
        "cold_water_m3": "Cold water (m³)",
        "basis": "Calculation basis",
        "tariff": "Tariff",
        "pv_price": "PV self-consumption price",
        "pv_production": "PV production",
        "pv_self_consumed": "PV self-consumed (energy)",
        "heating_share": "Heat pump heating share",
        "share_reservoir_ratio": "from reservoir energy output",
        "share_fallback": "fallback share, no reservoir data",
        "generated": "Generated",
        KIND_GRID: "Electricity {detail} (grid)",
        KIND_PV: "Electricity photovoltaic",
        KIND_COMMON_GRID: "Common electricity (share, grid)",
        KIND_COMMON_PV: "Common electricity (share, PV)",
        KIND_HEATING: "Heating",
        KIND_HOT_WATER: "Hot water",
    },
}


def labels_for(language: str) -> dict[str, str]:
    return LABELS.get(language, LABELS["en"])


def label_for_line(line: LineItem, text: dict[str, str]) -> str:
    return text[line.kind].format(detail=line.detail or "")
