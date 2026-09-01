"""Client for the Aquarea Home cloud, v2 API (SolutionTech/Innova backend).

REST for auth + topology, gRPC (grpclib, pure python) for live status and
control. The protocol was reverse-engineered from the Android apps — see
PROTOCOL.md in the project repository. Messages are built and parsed by
hand so no generated protobuf code (and no protobuf/grpcio pins that clash
with Home Assistant's constraints) is needed.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import struct
from typing import Any

import aiohttp
from grpclib.client import Channel
from grpclib.const import Cardinality, Status
from grpclib.exceptions import GRPCError, StreamTerminatedError

from .const import GRPC_HOST, GRPC_PORT, GRPC_SERVICE, REST_BASE

_LOGGER = logging.getLogger(__name__)

REST_TIMEOUT = aiohttp.ClientTimeout(total=20)
GRPC_TIMEOUT = 20
USER_AGENT = "AquareaHome/3.1.0 (Home Assistant integration)"


class AquareaHomeError(Exception):
    """Base error."""


class AuthError(AquareaHomeError):
    """Credentials or token rejected."""


class DeviceOffline(AquareaHomeError):
    """The cloud cannot reach the unit right now."""


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


def _tag(field: int, wire: int) -> bytes:
    return _write_varint((field << 3) | wire)


def _ld(field: int, payload: bytes) -> bytes:
    """Length-delimited field (wire type 2)."""
    return _tag(field, 2) + _write_varint(len(payload)) + payload


def _vint(field: int, value: int) -> bytes:
    return _tag(field, 0) + _write_varint(value)


def _f32(field: int, value: float) -> bytes:
    return _tag(field, 5) + struct.pack("<f", value)


def decode_message(buf: bytes) -> dict[int, list[Any]]:
    """Decode a protobuf message into {field_number: [values]}.

    Length-delimited fields come back as raw bytes for the caller to
    interpret; fixed32 fields are decoded as IEEE floats (the only fixed32
    values this API uses are temperatures)."""
    out: dict[int, list[Any]] = {}
    i = 0
    n = len(buf)
    while i < n:
        tag, i = _read_varint(buf, i)
        fnum, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _read_varint(buf, i)
        elif wt == 1:
            v = struct.unpack("<q", buf[i:i + 8])[0]
            i += 8
        elif wt == 5:
            v = struct.unpack("<f", buf[i:i + 4])[0]
            i += 4
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            if i + ln > n:
                raise AquareaHomeError(
                    f"truncated length-delimited field {fnum}: need {ln}, have {n - i}")
            v = buf[i:i + ln]
            i += ln
        else:
            raise AquareaHomeError(f"unsupported wire type {wt} on field {fnum}")
        out.setdefault(fnum, []).append(v)
    return out


def _first(msg: dict[int, list[Any]], field: int, default: Any = None) -> Any:
    vals = msg.get(field)
    return vals[0] if vals else default


def _sub(msg: dict[int, list[Any]], field: int) -> dict[int, list[Any]]:
    raw = _first(msg, field)
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return {}
    return decode_message(bytes(raw))


def _signed(v: int | None) -> int | None:
    """Interpret a varint as a signed 64-bit int (two's complement)."""
    if v is None:
        return None
    return v - (1 << 64) if v >= (1 << 63) else v


def _packed_varints(buf: Any) -> list[int]:
    if not isinstance(buf, (bytes, bytearray)):
        return []
    data = bytes(buf)
    out: list[int] = []
    i = 0
    while i < len(data):
        v, i = _read_varint(data, i)
        out.append(v)
    return out


def _text(v: Any) -> str | None:
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode()
        except UnicodeDecodeError:
            return None
    return None


def mac_to_bytes(mac: str) -> bytes:
    """'AA:BB:CC:DD:EE:FF' -> 6 raw bytes (the wire form of mac_address)."""
    return bytes.fromhex(mac.replace(":", "").replace("-", ""))


# ---------------------------------------------------------------------------
# request builders / response parser (services.app.AppService)
# ---------------------------------------------------------------------------

def build_get_state(mac: str, node_id: int = 0) -> bytes:
    """DeviceRequest{mac(1), node_id(2)?, request(3)=Command{shared(2)={get_state(1)={}}}}."""
    command = _ld(2, _ld(1, b""))
    req = _ld(1, mac_to_bytes(mac))
    if node_id:
        req += _vint(2, node_id)
    return req + _ld(3, command)


def build_set_state(mac: str, node_id: int = 0, *, power: bool | None = None,
                    setpoint: float | None = None, hvac_mode: int | None = None,
                    fan_speed: int | None = None,
                    flap_swing: bool | None = None) -> bytes:
    """DeviceRequest{..., request(3)=Command{ac(3)={set_state(1)=AcSetState}}}.

    Only the given fields are encoded; the backend applies them as a partial
    update."""
    ss = b""
    if power is not None:
        ss += _vint(1, 1 if power else 0)
    if setpoint is not None:
        ss += _f32(2, float(setpoint))
    if hvac_mode is not None:
        ss += _vint(3, int(hvac_mode))
    if fan_speed is not None:
        ss += _vint(4, int(fan_speed))
    if flap_swing is not None:
        ss += _vint(5, 1 if flap_swing else 0)
    command = _ld(3, _ld(1, ss))
    req = _ld(1, mac_to_bytes(mac))
    if node_id:
        req += _vint(2, node_id)
    return req + _ld(3, command)


def parse_state(raw: bytes) -> dict[str, Any]:
    """Parse a SendDevice(get_state) response for an AC (RAC Solo / Innova
    2.0) unit into a flat status dict.

    Layout (field numbers, observed live 2026-09-01):
      resp.2.1.1 = device
        .1 = metadata {2: fw, 3: serial, 4: {2: {1: {1: ssid, 2: rssi(int64)}}}}
        .2.2.1 = AC block {2: power, 3: setpoint{1 value,2 min,3 max,4 step},
                          4: mode{1 value, 3: packed options},
                          5: fan{1 value, 2: packed options},
                          6: flap, 7: room temperature}
    A response with field 1 and no field 2 is an error wrapper (e.g. code 1
    = RESPONSE_TIMEOUT): the cloud could not reach the unit."""
    root = decode_message(raw)
    if 2 not in root:
        err = _sub(root, 1)
        raise DeviceOffline(f"cloud could not reach the unit (error {dict(err) or 'empty'})")
    dev = _sub(_sub(_sub(root, 2), 1), 1)
    status: dict[str, Any] = {}

    meta = _sub(dev, 1)
    if meta:
        status["fw_version"] = _first(meta, 2)
        status["serial_number"] = _text(_first(meta, 3))
        wifi = _sub(_sub(_sub(meta, 4), 2), 1)
        if wifi:
            status["wifi_ssid"] = _text(_first(wifi, 1))
            status["wifi_rssi"] = _signed(_first(wifi, 2))

    ac = _sub(_sub(_sub(dev, 2), 2), 1)
    if ac:
        status["power"] = bool(_first(ac, 2, 0))
        sp = _sub(ac, 3)
        if sp:
            status["setpoint"] = round(float(_first(sp, 1, 0.0)), 1)
            status["setpoint_min"] = round(float(_first(sp, 2, 16.0)), 1)
            status["setpoint_max"] = round(float(_first(sp, 3, 31.0)), 1)
            status["setpoint_step"] = round(float(_first(sp, 4, 0.5)), 2)
        mode = _sub(ac, 4)
        status["operation_mode"] = _first(mode, 1, 0)
        opts = _packed_varints(_first(mode, 3))
        if opts:
            status["mode_options"] = opts
        fan = _sub(ac, 5)
        status["fan_speed"] = _first(fan, 1, 0)
        opts = _packed_varints(_first(fan, 2))
        if opts:
            status["fan_options"] = opts
        if 6 in ac:
            status["flap"] = _first(ac, 6)
        room = _first(ac, 7)
        if isinstance(room, float):
            status["room_temperature"] = round(room, 1)
    return status


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class AquareaHomeClient:
    """Async client: REST auth/topology + gRPC status/control."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str,
                 token: str | None = None) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = token
        self._ssl: ssl.SSLContext | None = None
        self._channel: Channel | None = None

    @property
    def token(self) -> str | None:
        """The bearer token in use (v2 tokens live for a year — persist it)."""
        return self._token

    # ---------------- REST ----------------

    async def login(self) -> dict[str, Any]:
        try:
            async with self._session.post(
                f"{REST_BASE}/users/login",
                json={"email": self._email, "password": self._password},
                headers={"User-Agent": USER_AGENT},
                timeout=REST_TIMEOUT,
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 401:
                    raise AuthError(body.get("message", "invalid credentials")
                                    if isinstance(body, dict) else "invalid credentials")
                if resp.status != 200:
                    raise AquareaHomeError(f"login failed: {resp.status} {body}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise AquareaHomeError(f"login network error: {err}") from err
        token = body.get("token") if isinstance(body, dict) else None
        if not token:
            raise AquareaHomeError("login response had no token")
        self._token = token
        return body.get("user", {})

    async def get_devices(self) -> list[dict[str, Any]]:
        """Flat device list from the homes topology (identity only; live
        state is gRPC)."""
        if not self._token:
            await self.login()
        try:
            async with self._session.get(
                f"{REST_BASE}/homes",
                headers={"Authorization": f"Bearer {self._token}",
                         "User-Agent": USER_AGENT},
                timeout=REST_TIMEOUT,
            ) as resp:
                if resp.status in (401, 403):
                    raise AuthError(f"token rejected (HTTP {resp.status})")
                if resp.status != 200:
                    raise AquareaHomeError(f"GET homes -> HTTP {resp.status}")
                homes = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise AquareaHomeError(f"homes network error: {err}") from err
        devices: list[dict[str, Any]] = []
        for home in homes or []:
            rooms = {r.get("id"): r.get("name") for r in home.get("rooms") or []}
            for dev in home.get("devices") or []:
                devices.append({
                    "mac": dev["macAddress"],
                    "node_id": dev.get("nodeId") or 0,
                    "name": dev.get("name") or "Aquarea Home device",
                    "serial": dev.get("serialNumber"),
                    "room": rooms.get(dev.get("roomId")),
                    "home": home.get("name"),
                    "home_id": home.get("id"),
                })
        return devices

    # ---------------- gRPC ----------------

    @staticmethod
    def _build_ssl_context() -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["h2"])
        return ctx

    async def _ensure_ssl(self) -> ssl.SSLContext:
        # loading CA certs does blocking disk I/O — keep it off the event loop
        ctx = self._ssl
        if ctx is None:
            ctx = await asyncio.get_running_loop().run_in_executor(
                None, self._build_ssl_context)
            self._ssl = ctx
        return ctx

    def _get_channel(self, ctx: ssl.SSLContext) -> Channel:
        if self._channel is None:
            self._channel = Channel(GRPC_HOST, GRPC_PORT, ssl=ctx)
        return self._channel

    def _close_channel(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None

    def close(self) -> None:
        self._close_channel()

    async def _send_device(self, payload: bytes) -> bytes:
        if not self._token:
            await self.login()
        ctx = await self._ensure_ssl()
        metadata = [("authorization", f"Bearer {self._token}")]
        try:
            async with self._get_channel(ctx).request(
                f"{GRPC_SERVICE}/SendDevice", Cardinality.UNARY_UNARY,
                RawMessage, RawMessage, metadata=metadata, timeout=GRPC_TIMEOUT,
            ) as stream:
                await stream.send_message(RawMessage(payload), end=True)
                reply = await stream.recv_message()
                return reply.data if reply else b""
        except GRPCError as err:
            # drop a possibly-wedged channel so the next call reconnects
            self._close_channel()
            if err.status in (Status.UNAUTHENTICATED, Status.PERMISSION_DENIED):
                raise AuthError(f"gRPC {err.status.name}: {err.message}") from err
            if err.status in (Status.UNAVAILABLE, Status.DEADLINE_EXCEEDED):
                raise DeviceOffline(f"gRPC {err.status.name}: {err.message}") from err
            raise AquareaHomeError(f"gRPC {err.status.name}: {err.message}") from err
        except (OSError, asyncio.TimeoutError, StreamTerminatedError,
                ConnectionError) as err:
            self._close_channel()
            raise AquareaHomeError(f"gRPC transport error: {err}") from err

    async def get_state(self, mac: str, node_id: int = 0) -> dict[str, Any]:
        """Live status of one unit. Raises DeviceOffline when the cloud
        cannot reach it."""
        raw = await self._send_device(build_get_state(mac, node_id))
        return parse_state(raw)

    async def set_state(self, mac: str, node_id: int = 0, **fields: Any) -> None:
        """Partial AcSetState write: power / setpoint / hvac_mode /
        fan_speed / flap_swing."""
        _LOGGER.debug("set_state mac=%s %s", mac, fields)
        raw = await self._send_device(build_set_state(mac, node_id, **fields))
        root = decode_message(raw) if raw else {}
        if root and 2 not in root and 1 in root:
            raise DeviceOffline(
                f"command not delivered (error {dict(_sub(root, 1)) or 'empty'})")
