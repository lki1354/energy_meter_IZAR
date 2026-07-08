"""The ``energy_meter_izar.generate_bill`` service.

Reads the billing configuration (``/config/energy_meter_izar/billing.yaml``,
built-in defaults when absent), runs the billing engine over the reading
store, and writes the rendered bills to
``/config/energy_meter_izar/bills/<start>_<end>_<profile>.<ext>``.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .billing import FILE_EXTENSIONS, RENDERERS, render_bill
from .billing.config import (
    BillingConfig,
    BillingConfigError,
    Profile,
    load_billing_config,
)
from .billing.engine import BillResult, generate_bill
from .const import (
    ATTR_END,
    ATTR_FORMATS,
    ATTR_PROFILE,
    ATTR_START,
    BILLING_CONFIG_FILENAME,
    BILLS_SUBDIR,
    DOMAIN,
    EVENT_BILL_GENERATED,
    SERVICE_GENERATE_BILL,
)
from .coordinator import IzarConfigEntry

_LOGGER = logging.getLogger(__name__)

GENERATE_BILL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START): cv.date,
        vol.Required(ATTR_END): cv.date,
        vol.Optional(ATTR_PROFILE): cv.string,
        vol.Optional(ATTR_FORMATS): vol.All(
            cv.ensure_list, [cv.string], vol.Length(min=1)
        ),
    }
)


def _loaded_entry(hass: HomeAssistant) -> IzarConfigEntry:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_entry_loaded"
        )
    return entries[0]


def _resolve_profile(config: BillingConfig, name: str | None) -> Profile:
    if name is None:
        return config.default_profile
    try:
        return config.profiles[name]
    except KeyError:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_profile",
            translation_placeholders={
                "profile": name,
                "profiles": ", ".join(config.profiles),
            },
        ) from None


def _resolve_formats(profile: Profile, requested: list[str] | None) -> list[str]:
    formats = requested if requested is not None else list(profile.formats)
    unsupported = [fmt for fmt in formats if fmt not in RENDERERS]
    if unsupported:
        _LOGGER.warning(
            "skipping unsupported bill format(s): %s", ", ".join(unsupported)
        )
    supported = [fmt for fmt in formats if fmt in RENDERERS]
    if not supported:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_supported_format",
            translation_placeholders={
                "requested": ", ".join(formats),
                "supported": ", ".join(RENDERERS),
            },
        )
    return supported


def _write_bills(
    result: BillResult,
    profile: Profile,
    formats: list[str],
    bills_dir: Path,
) -> list[str]:
    """Render and write all requested formats (runs in the executor)."""
    bills_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result.start.date()}_{result.end.date()}_{profile.name}"
    paths = []
    for fmt in formats:
        path = bills_dir / f"{stem}.{FILE_EXTENSIONS[fmt]}"
        path.write_text(render_bill(result, profile, fmt), encoding="utf-8")
        paths.append(str(path))
    return paths


async def _async_generate_bill(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    entry = _loaded_entry(hass)
    reading_store = entry.runtime_data.reading_store
    if reading_store is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_entry_loaded"
        )

    start: dt.date = call.data[ATTR_START]
    end: dt.date = call.data[ATTR_END]
    if end <= start:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_period",
            translation_placeholders={"start": str(start), "end": str(end)},
        )

    try:
        config = await hass.async_add_executor_job(
            load_billing_config, hass.config.path(DOMAIN, BILLING_CONFIG_FILENAME)
        )
    except BillingConfigError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_billing_config",
            translation_placeholders={"error": str(err)},
        ) from err

    profile = _resolve_profile(config, call.data.get(ATTR_PROFILE))
    formats = _resolve_formats(profile, call.data.get(ATTR_FORMATS))

    # Store timestamps are naive local; bill over local midnights.
    start_dt = dt.datetime.combine(start, dt.time.min)
    end_dt = dt.datetime.combine(end, dt.time.min)
    result = await hass.async_add_executor_job(
        generate_bill, reading_store, config, start_dt, end_dt
    )
    files = await hass.async_add_executor_job(
        _write_bills,
        result,
        profile,
        formats,
        Path(hass.config.path(DOMAIN, BILLS_SUBDIR)),
    )

    event_data = {
        ATTR_START: str(start),
        ATTR_END: str(end),
        ATTR_PROFILE: profile.name,
        "files": files,
        "total": round(result.total, 2),
        "currency": result.currency,
    }
    hass.bus.async_fire(EVENT_BILL_GENERATED, event_data)
    persistent_notification.async_create(
        hass,
        (
            f"Bill {start} – {end} ({profile.name}): "
            f"{result.total:.2f} {result.currency}\n\n"
            + "\n".join(f"- {file}" for file in files)
        ),
        title="Energy bill generated",
        notification_id=f"{DOMAIN}_bill_{start}_{end}_{profile.name}",
    )
    _LOGGER.info("generated bill %s – %s (%s): %s", start, end, profile.name, files)

    if call.return_response:
        return event_data
    return None


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's services (idempotent per HA lifetime)."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_BILL,
        _async_generate_bill,
        schema=GENERATE_BILL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
