"""Sensors for Aquarea Home devices: room temperature + WiFi diagnostics."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AquareaHomeCoordinator
from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: AquareaHomeCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for dev in coordinator.devices:
        entities.append(RoomTemperatureSensor(coordinator, dev))
        entities.append(WifiRssiSensor(coordinator, dev))
    async_add_entities(entities)


class _Base(CoordinatorEntity[AquareaHomeCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: AquareaHomeCoordinator, device: dict) -> None:
        super().__init__(coordinator)
        self._mac = device["mac"]
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, self._mac)})

    @property
    def _status(self) -> dict:
        return (self.coordinator.data or {}).get(self._mac, {})


class RoomTemperatureSensor(_Base):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_translation_key = "room_temperature"

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{DOMAIN}_{self._mac}_room_temperature"
        self._attr_name = "Room temperature"

    @property
    def native_value(self) -> float | None:
        return self._status.get("room_temperature")


class WifiRssiSensor(_Base):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{DOMAIN}_{self._mac}_wifi_rssi"
        self._attr_name = "WiFi signal"

    @property
    def native_value(self) -> int | None:
        return self._status.get("wifi_rssi")
