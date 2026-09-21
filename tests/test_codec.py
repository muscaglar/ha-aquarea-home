"""The hand-built protobuf codec: request bytes and get_state parsing."""
import struct

import pytest

from custom_components.aquarea_home import api

MAC = "AA:BB:CC:11:22:33"
MAC_HEX = "aabbcc112233"


# ---------------------------------------------------------------------------
# fixture builders (the documented v2 layout, see PROTOCOL.md)
# ---------------------------------------------------------------------------

def ac_block(*, power=True, setpoint=21.5, mode=3, fan=2, flap=None, room=22.4,
             limits=(16.0, 31.0, 0.5)) -> bytes:
    out = b""
    if power:
        out += api._vint(2, 1)  # absent on the wire when off
    sp = api._f32(1, setpoint) + api._f32(2, limits[0]) + api._f32(3, limits[1]) \
        + api._f32(4, limits[2])
    out += api._ld(3, sp)
    out += api._ld(4, api._vint(1, mode) + api._ld(3, bytes([1, 2, 3, 4, 5])))
    out += api._ld(5, api._vint(1, fan) + api._ld(2, bytes([1, 2, 3, 4, 5])))
    if flap is not None:
        out += api._vint(6, flap)
    if room is not None:
        out += api._f32(7, room)
    return out


def node_entry(key: int, node: bytes) -> bytes:
    """One entry of State.nodes (map<uint32, Node>)."""
    entry = (api._vint(1, key) if key else b"") + api._ld(2, node)
    return api._ld(2, entry)


def metadata(rssi: int = -61) -> bytes:
    wifi = api._ld(1, b"MyWiFi") + api._vint(2, rssi & 0xFFFFFFFFFFFFFFFF)
    return api._ld(1, api._vint(2, 50) + api._ld(3, b"%IN00000000")
                   + api._ld(4, api._ld(2, api._ld(1, wifi))))


def reply(*parts: bytes) -> bytes:
    """Response{device(2)={shared(1)={state(1)=State}}}."""
    return api._ld(2, api._ld(1, api._ld(1, b"".join(parts))))


def error_reply(code: int) -> bytes:
    return api._ld(1, api._vint(1, code))


# ---------------------------------------------------------------------------
# request builders
# ---------------------------------------------------------------------------

def test_get_state_golden_bytes():
    assert api.build_get_state(MAC).hex() == "0a06" + MAC_HEX + "1a0412020a00"


def test_get_state_with_node_id():
    assert api.build_get_state(MAC, 3).hex() == "0a06" + MAC_HEX + "1003" + "1a0412020a00"


def test_set_state_encodes_only_the_given_fields():
    raw = api.build_set_state(MAC, setpoint=21.5)
    body = "15" + struct.pack("<f", 21.5).hex()          # AcSetState{setpoint(2)}
    assert raw.hex() == "0a06" + MAC_HEX + "1a09" + "1a07" + "0a05" + body


def test_set_state_power_off_is_an_explicit_zero():
    raw = api.build_set_state(MAC, power=False)
    assert raw.hex().endswith("1a06" + "1a04" + "0a02" + "0800")


def test_negative_varint_is_refused_instead_of_looping_forever():
    with pytest.raises(ValueError):
        api._write_varint(-1)


# ---------------------------------------------------------------------------
# parse_state
# ---------------------------------------------------------------------------

def test_parse_full_reply():
    status = api.parse_state(reply(metadata(), node_entry(0, api._ld(1, ac_block(flap=1)))))
    assert status == {
        "fw_version": 50, "serial_number": "%IN00000000",
        "wifi_ssid": "MyWiFi", "wifi_rssi": -61,
        "power": True,
        "setpoint": 21.5, "setpoint_min": 16.0, "setpoint_max": 31.0, "setpoint_step": 0.5,
        "operation_mode": 3, "mode_options": [1, 2, 3, 4, 5],
        "fan_speed": 2, "fan_options": [1, 2, 3, 4, 5],
        "flap": 1, "room_temperature": 22.4,
    }


def test_power_absent_on_the_wire_means_off_not_unknown():
    status = api.parse_state(reply(node_entry(0, api._ld(1, ac_block(power=False)))))
    assert status["power"] is False


def test_unknown_enum_values_are_passed_through():
    status = api.parse_state(reply(node_entry(0, api._ld(1, ac_block(mode=9, fan=8)))))
    assert (status["operation_mode"], status["fan_speed"]) == (9, 8)


def test_node_is_picked_by_id_when_there_are_several():
    raw = reply(node_entry(1, api._ld(1, ac_block(setpoint=18.0))),
                node_entry(2, api._ld(1, ac_block(setpoint=25.0))))
    assert api.parse_state(raw, 2)["setpoint"] == 25.0
    # no entry for the id: a single-node unit's one entry, whatever its key
    assert api.parse_state(raw, 7)["setpoint"] == 18.0


@pytest.mark.parametrize(("code", "name"), [(1, "RESPONSE_TIMEOUT"), (2, "CACHE_NOT_READY"),
                                            (9, "error code 9")])
def test_error_wrapper_is_device_offline_with_a_readable_code(code, name):
    with pytest.raises(api.DeviceOffline, match=name):
        api.parse_state(error_reply(code))


def test_empty_reply_is_device_offline():
    with pytest.raises(api.DeviceOffline):
        api.parse_state(b"")


@pytest.mark.parametrize(("code", "name"), [(1, "OFFLINE"), (2, "CACHE_NOT_READY"),
                                            (3, "INTERNAL")])
def test_node_error_is_device_offline_not_an_empty_state(code, name):
    raw = reply(metadata(), node_entry(0, api._vint(6, code)))
    with pytest.raises(api.DeviceOffline, match=name):
        api.parse_state(raw)


def test_non_ac_node_is_device_offline():
    raw = reply(metadata(), node_entry(0, api._ld(2, b"\x08\x01")))   # fancoil = 2
    with pytest.raises(api.DeviceOffline, match="not an AC node"):
        api.parse_state(raw)


def test_reply_without_nodes_is_device_offline():
    with pytest.raises(api.DeviceOffline, match="no node state"):
        api.parse_state(reply(metadata()))


@pytest.mark.parametrize("raw", [b"\x80", b"\x0d\x00\x00", b"\x09\x00", b"\x0a\x05ab"])
def test_malformed_input_raises_the_integration_error(raw):
    with pytest.raises(api.AquareaHomeError):
        api.decode_message(raw)


def test_every_truncation_of_a_real_reply_fails_cleanly():
    raw = reply(metadata(), node_entry(0, api._ld(1, ac_block(flap=0))))
    for cut in range(len(raw)):
        try:
            api.parse_state(raw[:cut])
        except api.AquareaHomeError:
            pass   # includes DeviceOffline; anything else would fail the test
