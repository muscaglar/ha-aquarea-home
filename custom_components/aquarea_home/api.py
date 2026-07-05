"""Client for the Aquarea Home cloud (SolutionTech/Innova backend).

REST for auth + topology, gRPC (grpclib, pure python) for live status and
control. Protocol reverse-engineered from the Android app — see PROTOCOL.md
in the project repository.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
from typing import Any

import aiohttp
from grpclib.client import Channel
from grpclib.const import Cardinality

from .const import (
    GRPC_HOST,
    GRPC_PORT,
    REST_BASE,
    STREAM_IDLE_TIMEOUT_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class AquareaHomeError(Exception):
    """Base error."""


class AuthError(AquareaHomeError):
    """Invalid credentials."""


class RawMessage:
    """Duck-typed protobuf message carrying raw bytes (for grpclib codec)."""

    def __init__(self, data: bytes = b"") -> None:
        self.data = data

    def SerializeToString(self) -> bytes:  # noqa: N802 (protobuf API)
        return self.data

    @classmethod
    def FromString(cls, data: bytes) -> "RawMessage":  # noqa: N802
        return cls(data)


# ---------------------------------------------------------------------------
# minimal protobuf wire codec
# ---------------------------------------------------------------------------

def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def encode_type_value(type_: int, value: int) -> bytes:
    """Encode SetDeviceValueRequest {1: type, 2: value} (proto3 semantics:
    zero-valued fields are encoded explicitly here; the server treats absent
    and zero identically, and the app sends both fields)."""
    return (b"\x08" + _write_varint(type_)) + (b"\x10" + _write_varint(value))


def decode_message(buf: bytes) -> dict[int, list[Any]]:
    """Decode a protobuf message into {field_number: [values]}; length-
    delimited fields are returned as raw bytes for the caller to interpret."""
    out: dict[int, list[Any]] = {}
    i = 0
    while i < len(buf):
        tag, i = _read_varint(buf, i)
        fnum, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _read_varint(buf, i)
        elif wt == 1:
            v = struct.unpack("<q", buf[i:i + 8])[0]
            i += 8
        elif wt == 5:
            v = struct.unpack("<i", buf[i:i + 4])[0]
            i += 4
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            v = buf[i:i + ln]
            i += ln
        else:
            raise AquareaHomeError(f"unsupported wire type {wt}")
        out.setdefault(fnum, []).append(v)
    return out


def _first(msg: dict[int, list[Any]], field: int, default: Any = None) -> Any:
    vals = msg.get(field)
    return vals[0] if vals else default


def _signed(v: int | None) -> int | None:
    """Interpret a varint as a signed 64-bit int (two's complement)."""
    if v is None:
        return None
    return v - (1 << 64) if v >= (1 << 63) else v


def parse_status(raw: bytes) -> dict[str, Any]:
    """Parse GetDeviceStatusResponse for a duepuntozero (RAC Solo) device."""
    root = decode_message(raw)
    status: dict[str, Any] = {}

    iot_raw = _first(root, 1)
    if iot_raw is not None:
        iot = decode_message(iot_raw)
        status["iot_fw_version"] = _first(iot, 1)
        wifi_raw = _first(iot, 5)
        if wifi_raw is not None:
            wifi = decode_message(wifi_raw)
            ssid = _first(wifi, 1)
            status["wifi_ssid"] = ssid.decode() if isinstance(ssid, bytes) else None
            status["wifi_rssi"] = _signed(_first(wifi, 2))

    main_raw = _first(root, 2)
    if main_raw is not None:
        main = decode_message(main_raw)
        serial = _first(main, 2)
        status["serial_number"] = serial.decode() if isinstance(serial, bytes) else None
        dp_raw = _first(main, 4)
        if dp_raw is not None:
            dp = decode_message(dp_raw)
            status["power"] = bool(_first(dp, 2, 0))
            sp_raw = _first(dp, 3)
            if sp_raw is not None:
                sp = decode_message(sp_raw)
                status["setpoint"] = _first(sp, 1, 0) / 10
                status["setpoint_min"] = _first(sp, 2, 160) / 10
                status["setpoint_max"] = _first(sp, 3, 310) / 10
                status["setpoint_step"] = _first(sp, 4, 5) / 10
            room = _first(dp, 4)
            if room is not None:
                # room_temperature may arrive as plain varint or SetpointStatus
                if isinstance(room, bytes):
                    status["room_temperature"] = _first(decode_message(room), 1, 0) / 10
                else:
                    status["room_temperature"] = room / 10
            status["operation_mode"] = _first(dp, 5, 0)
            status["fan_speed"] = _first(dp, 6, 0)
            status["flap"] = _first(dp, 7, 0)
            status["is_duepuntozero"] = True
    return status


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class AquareaHomeClient:
    """Async client: REST auth/topology + gRPC status/control."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None
        self._token_exp: float = 0
        self._channel: Channel | None = None
        self._stream_channel: Channel | None = None

    # ---------------- REST ----------------

    async def login(self) -> dict[str, Any]:
        async with self._session.post(
            f"{REST_BASE}/users/login",
            json={"email": self._email, "password": self._password},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status == 401:
                raise AuthError(body.get("message", "invalid credentials"))
            if resp.status != 200:
                raise AquareaHomeError(f"login failed: {resp.status} {body}")
        token: str = body["token"]
        self._token = token
        self._token_exp = self._jwt_exp(token)
        return body.get("user", {})

    @staticmethod
    def _jwt_exp(token: str) -> float:
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
        except Exception:  # noqa: BLE001 — opaque token, assume 1h
            return time.time() + 3600

    async def _ensure_token(self) -> str:
        if not self._token or time.time() > self._token_exp - 300:
            await self.login()
        assert self._token
        return self._token

    async def get_devices(self) -> list[dict[str, Any]]:
        """Flat device list from homes topology."""
        await self._ensure_token()
        async with self._session.get(
            f"{REST_BASE}/homes",
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 401:
                await self.login()
                return await self.get_devices()
            homes = await resp.json(content_type=None)
        devices = []
        for home in homes or []:
            for room in home.get("rooms") or []:
                for dev in room.get("devices") or []:
                    devices.append({
                        "mac": dev["macAddress"],
                        "name": dev.get("name") or "Aquarea Home device",
                        "serial": dev.get("serialNumber"),
                        "room": room.get("name"),
                        "home": home.get("name"),
                    })
        return devices

    # ---------------- gRPC ----------------

    def _get_channel(self) -> Channel:
        if self._channel is None:
            self._channel = Channel(GRPC_HOST, GRPC_PORT, ssl=True)
        return self._channel

    def _get_stream_channel(self) -> Channel:
        # event streams get their own channel: a unary error must never
        # tear down the connection under a live subscription
        if self._stream_channel is None:
            self._stream_channel = Channel(GRPC_HOST, GRPC_PORT, ssl=True)
        return self._stream_channel

    def _close_unary_channel(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None

    def close(self) -> None:
        self._close_unary_channel()
        if self._stream_channel is not None:
            self._stream_channel.close()
            self._stream_channel = None

    async def _unary(self, method: str, payload: bytes, mac: str) -> bytes:
        token = await self._ensure_token()
        metadata = [("authorization", f"Bearer {token}"), ("mac_address", mac)]
        try:
            async with self._get_channel().request(
                method, Cardinality.UNARY_UNARY, RawMessage, RawMessage,
                metadata=metadata, timeout=25,
            ) as stream:
                await stream.send_message(RawMessage(payload), end=True)
                reply = await stream.recv_message()
                return reply.data if reply else b""
        except Exception:
            # drop a possibly-wedged unary channel so the next call
            # reconnects; the stream channel is deliberately untouched
            self._close_unary_channel()
            raise

    async def get_status(self, mac: str) -> dict[str, Any]:
        raw = await self._unary("/device_controls.Controls/GetDeviceStatus", b"", mac)
        return parse_status(raw)

    async def subscribe_events(self, mac: str):
        """Yield (event_type, value) from the push stream. Event types
        (DuepuntozeroEventType): 249 ManualMode, 250 Flap, 251 FanSpeed,
        252 OperationMode, 253 RoomTemperature, 254 Setpoint, 255 PowerState.
        Values arrive as 2-byte big-endian Modbus registers."""
        token = await self._ensure_token()
        metadata = [("authorization", f"Bearer {token}"), ("mac_address", mac)]
        async with self._get_stream_channel().request(
            "/device_controls.Controls/SubscribeToDeviceEvents",
            Cardinality.UNARY_STREAM, RawMessage, RawMessage,
            metadata=metadata,
        ) as stream:
            await stream.send_message(RawMessage(b""), end=True)
            while True:
                # idle timeout: a half-open TCP connection would otherwise
                # look "healthy" forever; reconnecting is one cheap RPC
                msg = await asyncio.wait_for(
                    stream.recv_message(), timeout=STREAM_IDLE_TIMEOUT_SECONDS)
                if msg is None:
                    return
                fields = decode_message(msg.data)
                etype = _first(fields, 1)
                raw_val = _first(fields, 2)
                if isinstance(raw_val, (bytes, bytearray)):
                    value = int.from_bytes(raw_val, "big")
                    if value >= 0x8000:  # signed 16-bit register
                        value -= 0x10000
                else:
                    value = _signed(raw_val) or 0  # varint path: same sign rule
                if etype is not None:
                    yield etype, value

    async def set_value(self, mac: str, type_: int, value: int) -> None:
        _LOGGER.debug("SetDeviceValue mac=%s type=%s value=%s", mac, type_, value)
        await self._unary(
            "/device_controls.Controls/SetDeviceValue",
            encode_type_value(type_, value), mac,
        )
