"""Sensor entities: one HA device per physical meter plus the gateway itself."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import IzarConfigEntry, IzarCoordinator
from .mbus_parser import TelegramLayout
from .pipeline import MeterState


@dataclass(frozen=True, kw_only=True)
class IzarMeterSensorDescription(SensorEntityDescription):
    """Describes one sensor derived from a meter's decoded readings.

    ``quantity`` selects a reading by its decoded quantity name; ``value_fn``
    (mutually exclusive) derives the value from the whole meter state.
    """

    quantity: str | None = None
    value_fn: Callable[[MeterState], Any] | None = None


def _quantity(
    quantity: str,
    key: str,
    *,
    device_class: SensorDeviceClass | None = None,
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT,
    unit: str | None = None,
    suggested_unit: str | None = None,
    enabled: bool = True,
    diagnostic: bool = False,
) -> IzarMeterSensorDescription:
    return IzarMeterSensorDescription(
        key=key,
        translation_key=key,
        quantity=quantity,
        device_class=device_class,
        state_class=state_class,
        native_unit_of_measurement=unit,
        suggested_unit_of_measurement=suggested_unit,
        entity_registry_enabled_default=enabled,
        entity_category=EntityCategory.DIAGNOSTIC if diagnostic else None,
    )


def _energy(quantity: str, key: str, *, enabled: bool = True) -> IzarMeterSensorDescription:
    return _quantity(
        quantity,
        key,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        unit=UnitOfEnergy.WATT_HOUR,
        suggested_unit=UnitOfEnergy.KILO_WATT_HOUR,
        enabled=enabled,
    )


_THERMAL_DESCRIPTIONS = (
    _quantity(
        "flow temperature (C)",
        "flow_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
    ),
    _quantity(
        "return temperature (C)",
        "return_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
    ),
    _quantity(
        "flow rate (m^3/h)",
        "flow_rate",
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        unit=UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
    ),
    _quantity(
        "power (W)",
        "power",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
    ),
    _quantity(
        "on time (seconds)",
        "on_time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        unit=UnitOfTime.SECONDS,
        enabled=False,
        diagnostic=True,
    ),
)

_RESERVOIR_DESCRIPTIONS = (
    _energy("energy (Wh)", "energy"),
    _quantity(
        "volume (m^3)",
        "volume",
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL_INCREASING,
        unit=UnitOfVolume.CUBIC_METERS,
    ),
    *_THERMAL_DESCRIPTIONS,
    _quantity(
        "error (none)",
        "error_code",
        state_class=None,
        enabled=False,
        diagnostic=True,
    ),
)

LAYOUT_DESCRIPTIONS: dict[TelegramLayout, tuple[IzarMeterSensorDescription, ...]] = {
    TelegramLayout.ELECTRICITY: (
        _energy("Energie 1 (Wh)", "energy"),
        _energy("Energie 2 (Wh)", "energy_2", enabled=False),
        _energy("Energie 3 (Wh)", "energy_3", enabled=False),
        _energy("Energie 4 (Wh)", "energy_4", enabled=False),
        *(
            _quantity(
                f"Leistung {phase} (W)",
                f"power_l{phase}",
                device_class=SensorDeviceClass.POWER,
                unit=UnitOfPower.WATT,
            )
            for phase in (1, 2, 3)
        ),
        *(
            _quantity(
                f"Spannung {phase} (V)",
                f"voltage_l{phase}",
                device_class=SensorDeviceClass.VOLTAGE,
                unit=UnitOfElectricPotential.VOLT,
                enabled=False,
            )
            for phase in (1, 2, 3)
        ),
        *(
            _quantity(
                f"Strom {phase} (A)",
                f"current_l{phase}",
                device_class=SensorDeviceClass.CURRENT,
                unit=UnitOfElectricCurrent.AMPERE,
                enabled=False,
            )
            for phase in (1, 2, 3)
        ),
    ),
    TelegramLayout.WATER: (
        _quantity(
            "water_1 (m^3)",
            "volume",
            device_class=SensorDeviceClass.WATER,
            state_class=SensorStateClass.TOTAL_INCREASING,
            unit=UnitOfVolume.CUBIC_METERS,
        ),
    ),
    TelegramLayout.HEAT: (
        _energy("heat_energy_1 (Wh)", "energy"),
        _energy("heat_energy_2 (Wh)", "energy_2", enabled=False),
        _quantity(
            "heat_volume_1 (m^3)",
            "volume",
            device_class=SensorDeviceClass.VOLUME,
            state_class=SensorStateClass.TOTAL_INCREASING,
            unit=UnitOfVolume.CUBIC_METERS,
        ),
        *_THERMAL_DESCRIPTIONS,
    ),
    TelegramLayout.RESERVOIR: _RESERVOIR_DESCRIPTIONS,
    TelegramLayout.RESERVOIR_WW: _RESERVOIR_DESCRIPTIONS,
}


def _meter_status(state: MeterState) -> str | None:
    for reading in state.readings.values():
        return reading.status
    return None


def _meter_last_seen(state: MeterState) -> dt.datetime | None:
    return _localize(state.last_seen)


METER_DIAGNOSTIC_DESCRIPTIONS: tuple[IzarMeterSensorDescription, ...] = (
    IzarMeterSensorDescription(
        key="mbus_status",
        translation_key="mbus_status",
        value_fn=_meter_status,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IzarMeterSensorDescription(
        key="last_reading",
        translation_key="last_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_meter_last_seen,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


@dataclass(frozen=True, kw_only=True)
class IzarGatewaySensorDescription(SensorEntityDescription):
    """Describes one diagnostic sensor of the gateway device."""

    value_fn: Callable[[IzarCoordinator], Any] = None  # type: ignore[assignment]


def _localize(value: dt.datetime | None) -> dt.datetime | None:
    """CP32 timestamps are naive local time; HA timestamps must be aware."""
    if value is None:
        return None
    return value.replace(tzinfo=dt_util.get_default_time_zone())


GATEWAY_DESCRIPTIONS: tuple[IzarGatewaySensorDescription, ...] = (
    IzarGatewaySensorDescription(
        key="last_readout",
        translation_key="last_readout",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: _localize(c.data.last_readout_time),
    ),
    IzarGatewaySensorDescription(
        key="bus_voltage",
        translation_key="bus_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.gateway.bus_voltage_v if c.data.gateway else None,
    ),
    IzarGatewaySensorDescription(
        key="bus_current",
        translation_key="bus_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.gateway.bus_current_ma if c.data.gateway else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IzarConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create gateway sensors now and meter sensors as meters appear."""
    coordinator = entry.runtime_data
    known_meters: set[int] = set()

    @callback
    def _sync_meters() -> None:
        new_entities: list[SensorEntity] = []
        for number, state in coordinator.data.meters.items():
            if number in known_meters:
                continue
            known_meters.add(number)
            quantity_descriptions = LAYOUT_DESCRIPTIONS[state.definition.layout]
            new_entities.extend(
                IzarMeterSensor(coordinator, number, description)
                for description in (*quantity_descriptions, *METER_DIAGNOSTIC_DESCRIPTIONS)
            )
        if new_entities:
            async_add_entities(new_entities)

    async_add_entities(
        IzarGatewaySensor(coordinator, description) for description in GATEWAY_DESCRIPTIONS
    )
    _sync_meters()
    entry.async_on_unload(coordinator.async_add_listener(_sync_meters))


class IzarGatewaySensor(CoordinatorEntity[IzarCoordinator], SensorEntity):
    """A diagnostic value of the M-Bus gateway itself."""

    entity_description: IzarGatewaySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: IzarCoordinator, description: IzarGatewaySensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        gateway = coordinator.data.gateway if coordinator.data else None
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="M-Bus Gateway",
            manufacturer="HC2",
            model=gateway.gateway_type if gateway else None,
            serial_number=gateway.device_id if gateway else None,
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)


class IzarMeterSensor(CoordinatorEntity[IzarCoordinator], SensorEntity):
    """One decoded quantity (or diagnostic) of one physical meter."""

    entity_description: IzarMeterSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IzarCoordinator,
        device_number: int,
        description: IzarMeterSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._device_number = device_number
        definition = coordinator.data.meters[device_number].definition
        self._attr_unique_id = f"{device_number}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(device_number))},
            name=f"{definition.medium.replace('_', ' ').title()} {definition.location}",
            model=definition.medium,
            serial_number=str(device_number),
            suggested_area=definition.location,
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

    @property
    def _state(self) -> MeterState | None:
        return self.coordinator.data.meters.get(self._device_number)

    @property
    def available(self) -> bool:
        if not super().available or self._state is None:
            return False
        if self.entity_description.quantity is not None:
            return self.entity_description.quantity in self._state.readings
        return True

    @property
    def native_value(self) -> Any:
        state = self._state
        if state is None:
            return None
        description = self.entity_description
        if description.value_fn is not None:
            return description.value_fn(state)
        reading = state.readings.get(description.quantity)
        return reading.value if reading is not None else None
