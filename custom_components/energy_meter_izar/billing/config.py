"""billing.yaml schema, validation, and typed configuration objects.

Everything that was hardcoded in the ``energy_bill_analysis*.py`` notebooks
(prices, tariff windows, device maps, splits) becomes configuration here.
The file lives at ``/config/energy_meter_izar/billing.yaml``; when it does
not exist, :func:`load_billing_config` falls back to a built-in default that
mirrors the building the notebooks were written for, so the
``generate_bill`` service works out of the box.

Pure Python — no Home Assistant imports.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import voluptuous as vol
import yaml

#: Reserved tariff name that prices PV self-consumption. It is not a
#: time-of-use tariff: it never has a schedule and can never be the default.
PV_TARIFF = "pv"

SECTION_ELECTRICITY = "electricity"
SECTION_HEATING = "heating"
SECTION_HOT_WATER = "hot_water"
SECTION_WATER_VOLUME = "water_volume"
SECTION_SUMMARY = "summary"
SECTIONS = (
    SECTION_ELECTRICITY,
    SECTION_HEATING,
    SECTION_HOT_WATER,
    SECTION_WATER_VOLUME,
    SECTION_SUMMARY,
)

FORMAT_MARKDOWN = "markdown"
FORMAT_CSV = "csv"
FORMAT_PDF = "pdf"
FORMATS = (FORMAT_MARKDOWN, FORMAT_CSV, FORMAT_PDF)

LANGUAGES = ("de", "en")

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class BillingConfigError(ValueError):
    """billing.yaml is missing required data or fails validation."""


def _parse_hhmm(value: str) -> int:
    """'HH:MM' → minutes since midnight; '24:00' marks end of day."""
    try:
        hours, minutes = value.split(":")
        total = int(hours) * 60 + int(minutes)
    except (ValueError, AttributeError) as err:
        raise vol.Invalid(f"expected 'HH:MM', got {value!r}") from err
    if not 0 <= total <= 24 * 60 or int(minutes) > 59:
        raise vol.Invalid(f"time of day out of range: {value!r}")
    return total


@dataclass(frozen=True)
class TariffWindow:
    """One schedule rule: these weekdays, from..to (end exclusive)."""

    days: frozenset[int]  # 0 = Monday … 6 = Sunday
    start_minute: int
    end_minute: int

    def matches(self, timestamp: dt.datetime) -> bool:
        minute = timestamp.hour * 60 + timestamp.minute
        return (
            timestamp.weekday() in self.days
            and self.start_minute <= minute < self.end_minute
        )


@dataclass(frozen=True)
class Tariff:
    """A grid price band; windows are matched in configuration order."""

    name: str
    price_kwh: float
    windows: tuple[TariffWindow, ...] = ()
    default: bool = False


@dataclass(frozen=True)
class UnitDevices:
    """Meter numbers of one billable unit (apartment)."""

    name: str
    electricity: int | None = None
    heat: int | None = None
    hot_water: int | None = None
    cold_water: int | None = None


@dataclass(frozen=True)
class CommonElectricity:
    device: int
    split: str = "equal"


@dataclass(frozen=True)
class HeatPump:
    device: int
    split_method: str = "reservoir_ratio"
    reservoir_heating: int | None = None
    reservoir_hot_water: int | None = None
    fallback_heating_share: float = 0.70


@dataclass(frozen=True)
class Photovoltaic:
    device: int
    allocation: str = "proportional"


@dataclass(frozen=True)
class Profile:
    """A named export configuration (which sections, language, formats)."""

    name: str
    sections: tuple[str, ...]
    language: str = "de"
    formats: tuple[str, ...] = (FORMAT_MARKDOWN, FORMAT_CSV)


@dataclass(frozen=True)
class BillingConfig:
    currency: str
    units: dict[str, UnitDevices]
    tariffs: tuple[Tariff, ...]  # grid tariffs only, in match order
    pv_price_kwh: float | None
    profiles: dict[str, Profile]
    common_electricity: CommonElectricity | None = None
    heat_pump: HeatPump | None = None
    photovoltaic: Photovoltaic | None = None
    notes: tuple[str, ...] = field(default=())

    @property
    def default_tariff(self) -> Tariff:
        return next(t for t in self.tariffs if t.default)

    @property
    def default_profile(self) -> Profile:
        return next(iter(self.profiles.values()))


_WINDOW_SCHEMA = vol.Schema(
    {
        vol.Required("days"): [vol.In(_DAYS)],
        vol.Required("from"): _parse_hhmm,
        vol.Required("to"): _parse_hhmm,
    }
)

_TARIFF_SCHEMA = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Required("price_kwh"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("schedule"): [_WINDOW_SCHEMA],
        vol.Optional("default", default=False): bool,
    }
)

_UNIT_SCHEMA = vol.Schema(
    {
        vol.Optional("electricity"): vol.Coerce(int),
        vol.Optional("heat"): vol.Coerce(int),
        vol.Optional("hot_water"): vol.Coerce(int),
        vol.Optional("cold_water"): vol.Coerce(int),
    }
)

_PROFILE_SCHEMA = vol.Schema(
    {
        vol.Required("sections"): vol.All([vol.In(SECTIONS)], vol.Length(min=1)),
        vol.Optional("language", default="de"): vol.In(LANGUAGES),
        vol.Optional("formats", default=list((FORMAT_MARKDOWN, FORMAT_CSV))): vol.All(
            [vol.In(FORMATS)], vol.Length(min=1)
        ),
    }
)

_SHARED_SCHEMA = vol.Schema(
    {
        vol.Optional("common_electricity"): vol.Schema(
            {
                vol.Required("device"): vol.Coerce(int),
                vol.Optional("split", default="equal"): vol.In(["equal"]),
            }
        ),
        vol.Optional("heat_pump"): vol.Schema(
            {
                vol.Required("device"): vol.Coerce(int),
                vol.Optional("split_method", default="reservoir_ratio"): vol.In(
                    ["reservoir_ratio"]
                ),
                vol.Optional("reservoir_heating"): vol.Coerce(int),
                vol.Optional("reservoir_hot_water"): vol.Coerce(int),
                vol.Optional("fallback_heating_share", default=0.70): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=1)
                ),
            }
        ),
        vol.Optional("photovoltaic"): vol.Schema(
            {
                vol.Required("device"): vol.Coerce(int),
                vol.Optional("allocation", default="proportional"): vol.In(
                    ["proportional"]
                ),
            }
        ),
    }
)

BILLING_SCHEMA = vol.Schema(
    {
        vol.Optional("currency", default="CHF"): str,
        vol.Required("units"): vol.All({str: _UNIT_SCHEMA}, vol.Length(min=1)),
        vol.Optional("shared", default=dict): _SHARED_SCHEMA,
        vol.Required("tariffs"): vol.All([_TARIFF_SCHEMA], vol.Length(min=1)),
        vol.Optional("profiles", default=dict): {str: _PROFILE_SCHEMA},
    }
)


def _parse_tariffs(raw: list[dict]) -> tuple[tuple[Tariff, ...], float | None]:
    """Split raw tariff list into (grid tariffs, PV price)."""
    grid: list[Tariff] = []
    pv_price: float | None = None
    for item in raw:
        if item["name"] == PV_TARIFF:
            if pv_price is not None:
                raise BillingConfigError("more than one 'pv' tariff configured")
            if item.get("schedule") or item["default"]:
                raise BillingConfigError(
                    "the 'pv' tariff prices self-consumption and cannot have "
                    "a schedule or be the default"
                )
            pv_price = item["price_kwh"]
            continue
        windows = tuple(
            TariffWindow(
                days=frozenset(_DAYS[d] for d in window["days"]),
                start_minute=window["from"],
                end_minute=window["to"],
            )
            for window in item.get("schedule", [])
        )
        grid.append(
            Tariff(
                name=item["name"],
                price_kwh=item["price_kwh"],
                windows=windows,
                default=item["default"],
            )
        )

    if not grid:
        raise BillingConfigError("at least one grid tariff is required")
    defaults = [t for t in grid if t.default]
    if len(grid) == 1 and not defaults:
        grid[0] = Tariff(grid[0].name, grid[0].price_kwh, grid[0].windows, default=True)
        defaults = [grid[0]]
    if len(defaults) != 1:
        raise BillingConfigError(
            "exactly one tariff must be marked 'default: true' "
            "(it catches every time not covered by a schedule)"
        )
    for tariff in grid:
        if not tariff.default and not tariff.windows:
            raise BillingConfigError(
                f"tariff {tariff.name!r} has no schedule and is not the "
                "default, so it would never match"
            )
    return tuple(grid), pv_price


def parse_billing_config(raw: dict) -> BillingConfig:
    """Validate a raw billing.yaml mapping into a :class:`BillingConfig`."""
    try:
        data = BILLING_SCHEMA(raw)
    except vol.Invalid as err:
        raise BillingConfigError(f"invalid billing configuration: {err}") from err

    units = {
        name: UnitDevices(name=name, **devices)
        for name, devices in data["units"].items()
    }

    tariffs, pv_price = _parse_tariffs(data["tariffs"])

    shared = data["shared"]
    common = (
        CommonElectricity(**shared["common_electricity"])
        if "common_electricity" in shared
        else None
    )
    heat_pump = HeatPump(**shared["heat_pump"]) if "heat_pump" in shared else None
    photovoltaic = (
        Photovoltaic(**shared["photovoltaic"]) if "photovoltaic" in shared else None
    )
    if photovoltaic is not None and pv_price is None:
        raise BillingConfigError(
            "a photovoltaic device is configured but there is no 'pv' tariff "
            "pricing self-consumption"
        )

    profiles = {
        name: Profile(
            name=name,
            sections=tuple(profile["sections"]),
            language=profile["language"],
            formats=tuple(profile["formats"]),
        )
        for name, profile in data["profiles"].items()
    }
    if not profiles:
        profiles["default"] = Profile(name="default", sections=SECTIONS)

    return BillingConfig(
        currency=data["currency"],
        units=units,
        tariffs=tariffs,
        pv_price_kwh=pv_price,
        profiles=profiles,
        common_electricity=common,
        heat_pump=heat_pump,
        photovoltaic=photovoltaic,
    )


def default_billing_config() -> BillingConfig:
    """Built-in configuration mirroring the notebook pipeline's building.

    Used when no billing.yaml exists yet; prices are the Q3/2025 reference
    prices from the last manual bill.
    """
    return parse_billing_config(
        {
            "currency": "CHF",
            "units": {
                "EG": {
                    "electricity": 11601997,
                    "heat": 2800001,
                    "hot_water": 2800002,
                    "cold_water": 2800003,
                },
                "1.OG": {
                    "electricity": 11601989,
                    "heat": 2801004,
                    "hot_water": 2801005,
                    "cold_water": 2801006,
                },
                "DG": {
                    "electricity": 11601959,
                    "heat": 2801007,
                    "hot_water": 2801008,
                    "cold_water": 2801009,
                },
            },
            "shared": {
                "common_electricity": {"device": 11601990, "split": "equal"},
                "heat_pump": {
                    "device": 11601992,
                    "split_method": "reservoir_ratio",
                    "reservoir_heating": 2891010,
                    "reservoir_hot_water": 2891011,
                    "fallback_heating_share": 0.70,
                },
                "photovoltaic": {"device": 11601893, "allocation": "proportional"},
            },
            "tariffs": [
                {
                    "name": "hochtarif",
                    "price_kwh": 0.2218,
                    "schedule": [
                        {
                            "days": ["mon", "tue", "wed", "thu", "fri"],
                            "from": "07:00",
                            "to": "20:00",
                        },
                        {"days": ["sat"], "from": "07:00", "to": "13:00"},
                    ],
                },
                {"name": "niedertarif", "price_kwh": 0.2028, "default": True},
                {"name": PV_TARIFF, "price_kwh": 0.088},
            ],
            "profiles": {
                "quarterly_full": {
                    "sections": [
                        SECTION_ELECTRICITY,
                        SECTION_HEATING,
                        SECTION_HOT_WATER,
                        SECTION_SUMMARY,
                    ],
                    "language": "de",
                    "formats": [FORMAT_MARKDOWN, FORMAT_CSV, FORMAT_PDF],
                },
                "water_only": {
                    "sections": [SECTION_WATER_VOLUME],
                    "formats": [FORMAT_CSV],
                },
            },
        }
    )


def load_billing_config(path: str | Path) -> BillingConfig:
    """Load billing.yaml; fall back to the built-in default when absent."""
    path = Path(path)
    if not path.exists():
        return default_billing_config()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise BillingConfigError(f"{path.name} is not valid YAML: {err}") from err
    if not isinstance(raw, dict):
        raise BillingConfigError(f"{path.name} must contain a YAML mapping")
    return parse_billing_config(raw)
