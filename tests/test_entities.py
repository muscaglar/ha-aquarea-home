"""Entity-side gates: a unit left out of the data must read unavailable on
its own, and an unsupported mode must be refused before it reaches the cloud."""
import types

import pytest

import ha_stub
from custom_components.aquarea_home import climate, sensor

UNIT_A = {"mac": "A", "name": "Lounge"}
UNIT_B = {"mac": "B", "name": "Bedroom"}


def coordinator(data, ok=True):
    return types.SimpleNamespace(data=data, last_update_success=ok)


def test_a_dropped_units_sensors_are_unavailable_the_others_are_not():
    coord = coordinator({"A": {"room_temperature": 22.0, "wifi_rssi": -61}})
    assert sensor.RoomTemperatureSensor(coord, UNIT_A).available
    assert sensor.RoomTemperatureSensor(coord, UNIT_A).native_value == 22.0
    assert sensor.WifiRssiSensor(coord, UNIT_A).native_value == -61
    assert not sensor.RoomTemperatureSensor(coord, UNIT_B).available
    assert not sensor.WifiRssiSensor(coord, UNIT_B).available


def test_a_failed_update_makes_every_sensor_unavailable():
    coord = coordinator({"A": {"room_temperature": 22.0}}, ok=False)
    assert not sensor.RoomTemperatureSensor(coord, UNIT_A).available


def test_climate_is_available_per_unit_and_reads_the_units_own_lists():
    coord = coordinator({"A": {"power": True, "operation_mode": 3, "fan_speed": 5,
                               "mode_options": [2, 3], "fan_options": [1, 5],
                               "setpoint": 21.5, "setpoint_min": 18.0}})
    unit = climate.AquareaHomeClimate(coord, UNIT_A)
    assert unit.available and not climate.AquareaHomeClimate(coord, UNIT_B).available
    assert unit.hvac_mode == climate.HVACMode.COOL and unit.fan_mode == "max"
    assert unit.hvac_modes == [climate.HVACMode.OFF, climate.HVACMode.HEAT,
                               climate.HVACMode.COOL]
    assert unit.fan_modes == ["auto", "max"]
    assert (unit.min_temp, unit.max_temp, unit.target_temperature_step) == (18.0, 31.0, 0.5)


async def test_an_unsupported_hvac_mode_is_refused_before_it_is_sent():
    coord = coordinator({"A": {"power": True, "mode_options": [2, 3]}})
    unit = climate.AquareaHomeClimate(coord, UNIT_A)
    with pytest.raises(ha_stub.ServiceValidationError):
        await unit.async_set_hvac_mode(climate.HVACMode.DRY)
