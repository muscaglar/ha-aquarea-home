"""Climate entity for Aquarea Home RAC Solo (duepuntozero) devices."""
from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AquareaHomeCoordinator
from .const import (
    DOMAIN,
    FAN_AUTO, FAN_MAX, FAN_MEDIUM, FAN_MIN,
    MODE_AUTO, MODE_COOL, MODE_DRY, MODE_FAN, MODE_HEAT,
    OP_FAN, OP_MODE, OP_POWER, OP_SETPOINT,
)

MODE_TO_HVAC = {
    MODE_AUTO: HVACMode.AUTO,
    MODE_HEAT: HVACMode.HEAT,
    MODE_COOL: HVACMode.COOL,
    MODE_FAN: HVACMode.FAN_ONLY,
    MODE_DRY: HVACMode.DRY,
}
HVAC_TO_MODE = {v: k for k, v in MODE_TO_HVAC.items()}

FAN_TO_HA = {FAN_AUTO: "auto", FAN_MIN: "low", FAN_MEDIUM: "medium", FAN_MAX: "high"}
HA_TO_FAN = {v: k for k, v in FAN_TO_HA.items()}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: AquareaHomeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        AquareaHomeClimate(coordinator, dev) for dev in coordinator.devices
    )


class AquareaHomeClimate(CoordinatorEntity[AquareaHomeCoordinator], ClimateEntity):
    """The RAC Solo as a thermostat."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT,
                        HVACMode.COOL, HVACMode.FAN_ONLY, HVACMode.DRY]
    _attr_fan_modes = ["auto", "low", "medium", "high"]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: AquareaHomeCoordinator, device: dict) -> None:
        super().__init__(coordinator)
        self._mac = device["mac"]
        self._attr_unique_id = f"{DOMAIN}_{self._mac}_climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            name=device["name"],
            manufacturer="Panasonic (Innova / SolutionTech)",
            model="RAC Solo",
            serial_number=device.get("serial"),
            suggested_area=device.get("room"),
        )

    @property
    def _status(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._mac, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._status)

    @property
    def hvac_mode(self) -> HVACMode | None:
        if not self._status.get("power"):
            return HVACMode.OFF
        return MODE_TO_HVAC.get(self._status.get("operation_mode", 0))

    @property
    def fan_mode(self) -> str | None:
        return FAN_TO_HA.get(self._status.get("fan_speed", 0))

    @property
    def current_temperature(self) -> float | None:
        return self._status.get("room_temperature")

    @property
    def target_temperature(self) -> float | None:
        return self._status.get("setpoint")

    @property
    def min_temp(self) -> float:
        return self._status.get("setpoint_min", 16.0)

    @property
    def max_temp(self) -> float:
        return self._status.get("setpoint_max", 31.0)

    @property
    def target_temperature_step(self) -> float:
        return self._status.get("setpoint_step", 0.5)

    async def _send(self, type_: int, value: int) -> None:
        await self.coordinator.client.set_value(self._mac, type_, value)

    async def _refresh_soon(self) -> None:
        await asyncio.sleep(2)
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self._send(OP_POWER, 0)
        else:
            if not self._status.get("power"):
                await self._send(OP_POWER, 1)
            await self._send(OP_MODE, HVAC_TO_MODE[hvac_mode])
        await self._refresh_soon()

    async def async_turn_on(self) -> None:
        await self._send(OP_POWER, 1)
        await self._refresh_soon()

    async def async_turn_off(self) -> None:
        await self._send(OP_POWER, 0)
        await self._refresh_soon()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            await self._send(OP_SETPOINT, round(float(temp) * 10))
            await self._refresh_soon()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._send(OP_FAN, HA_TO_FAN[fan_mode])
        await self._refresh_soon()
