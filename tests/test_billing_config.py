"""Tests for the billing.yaml schema and configuration parsing."""

import pytest

from custom_components.energy_meter_izar.billing.config import (
    BillingConfigError,
    load_billing_config,
    parse_billing_config,
)

STRATEGY_YAML = """
currency: CHF
units:
  EG:   { electricity: 11601997, heat: 2800001, hot_water: 2800002, cold_water: 2800003 }
  1.OG: { electricity: 11601989, heat: 2801004, hot_water: 2801005, cold_water: 2801006 }
  DG:   { electricity: 11601959, heat: 2801007, hot_water: 2801008, cold_water: 2801009 }

shared:
  common_electricity: { device: 11601990, split: equal }
  heat_pump:
    device: 11601992
    split_method: reservoir_ratio
    reservoir_heating: 2891010
    reservoir_hot_water: 2891011
    fallback_heating_share: 0.70
  photovoltaic: { device: 11601893, allocation: proportional }

tariffs:
  - name: hochtarif
    price_kwh: 0.2218
    schedule:
      - { days: [mon, tue, wed, thu, fri], from: "07:00", to: "20:00" }
      - { days: [sat], from: "07:00", to: "13:00" }
  - name: niedertarif
    price_kwh: 0.2028
    default: true
  - name: pv
    price_kwh: 0.088

profiles:
  quarterly_full:
    sections: [electricity, heating, hot_water, summary]
    language: de
    formats: [markdown, csv, pdf]
  water_only:
    sections: [water_volume]
    formats: [csv]
"""


def _minimal(**overrides):
    raw = {
        "units": {"EG": {"electricity": 1}},
        "tariffs": [{"name": "flat", "price_kwh": 0.2, "default": True}],
    }
    raw.update(overrides)
    return raw


def test_strategy_example_parses(tmp_path):
    path = tmp_path / "billing.yaml"
    path.write_text(STRATEGY_YAML, encoding="utf-8")
    config = load_billing_config(path)

    assert config.currency == "CHF"
    assert list(config.units) == ["EG", "1.OG", "DG"]
    assert config.units["1.OG"].hot_water == 2801005
    assert config.common_electricity.device == 11601990
    assert config.heat_pump.reservoir_heating == 2891010
    assert config.heat_pump.fallback_heating_share == pytest.approx(0.70)
    assert config.photovoltaic.device == 11601893

    # pv is split off the grid tariffs
    assert [t.name for t in config.tariffs] == ["hochtarif", "niedertarif"]
    assert config.pv_price_kwh == pytest.approx(0.088)
    assert config.default_tariff.name == "niedertarif"
    hochtarif = config.tariffs[0]
    assert len(hochtarif.windows) == 2
    assert hochtarif.windows[1].days == frozenset({5})
    assert hochtarif.windows[1].start_minute == 7 * 60
    assert hochtarif.windows[1].end_minute == 13 * 60

    # profiles, including the not-yet-renderable pdf format
    assert config.profiles["quarterly_full"].formats == ("markdown", "csv", "pdf")
    assert config.profiles["water_only"].sections == ("water_volume",)
    assert config.default_profile.name == "quarterly_full"


def test_missing_file_falls_back_to_default(tmp_path):
    config = load_billing_config(tmp_path / "does-not-exist.yaml")
    assert config.units["EG"].electricity == 11601997
    assert config.pv_price_kwh == pytest.approx(0.088)
    assert config.profiles["quarterly_full"].language == "de"


def test_invalid_yaml_raises(tmp_path):
    path = tmp_path / "billing.yaml"
    path.write_text("units: [unbalanced", encoding="utf-8")
    with pytest.raises(BillingConfigError, match="not valid YAML"):
        load_billing_config(path)


def test_non_mapping_yaml_raises(tmp_path):
    path = tmp_path / "billing.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(BillingConfigError, match="YAML mapping"):
        load_billing_config(path)


def test_no_units_rejected():
    with pytest.raises(BillingConfigError):
        parse_billing_config(_minimal(units={}))


def test_single_tariff_becomes_default():
    config = parse_billing_config(
        _minimal(tariffs=[{"name": "flat", "price_kwh": 0.25}])
    )
    assert config.default_tariff.name == "flat"


def test_two_defaults_rejected():
    with pytest.raises(BillingConfigError, match="exactly one"):
        parse_billing_config(
            _minimal(
                tariffs=[
                    {"name": "a", "price_kwh": 0.1, "default": True},
                    {"name": "b", "price_kwh": 0.2, "default": True},
                ]
            )
        )


def test_unreachable_tariff_rejected():
    with pytest.raises(BillingConfigError, match="never match"):
        parse_billing_config(
            _minimal(
                tariffs=[
                    {"name": "a", "price_kwh": 0.1, "default": True},
                    {"name": "unreachable", "price_kwh": 0.2},
                ]
            )
        )


def test_pv_tariff_cannot_have_schedule():
    with pytest.raises(BillingConfigError, match="pv"):
        parse_billing_config(
            _minimal(
                tariffs=[
                    {"name": "flat", "price_kwh": 0.2, "default": True},
                    {
                        "name": "pv",
                        "price_kwh": 0.08,
                        "schedule": [{"days": ["mon"], "from": "07:00", "to": "20:00"}],
                    },
                ]
            )
        )


def test_photovoltaic_requires_pv_tariff():
    with pytest.raises(BillingConfigError, match="pv"):
        parse_billing_config(
            _minimal(shared={"photovoltaic": {"device": 5}})
        )


def test_bad_day_name_rejected():
    with pytest.raises(BillingConfigError):
        parse_billing_config(
            _minimal(
                tariffs=[
                    {
                        "name": "a",
                        "price_kwh": 0.1,
                        "default": True,
                        "schedule": [
                            {"days": ["monday"], "from": "07:00", "to": "20:00"}
                        ],
                    }
                ]
            )
        )


def test_bad_time_rejected():
    with pytest.raises(BillingConfigError):
        parse_billing_config(
            _minimal(
                tariffs=[
                    {
                        "name": "a",
                        "price_kwh": 0.1,
                        "default": True,
                        "schedule": [{"days": ["mon"], "from": "7 am", "to": "20:00"}],
                    }
                ]
            )
        )


def test_unknown_section_rejected():
    with pytest.raises(BillingConfigError):
        parse_billing_config(_minimal(profiles={"p": {"sections": ["nope"]}}))


def test_profiles_default_injected():
    config = parse_billing_config(_minimal())
    assert config.default_profile.name == "default"
    assert "electricity" in config.default_profile.sections
    assert config.default_profile.formats == ("markdown", "csv")
