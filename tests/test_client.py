"""AquareaHomeClient error mapping: only the integration's own exceptions may
leave the API layer, and the class tells the caller whose problem it is."""
import asyncio

import pytest
from grpclib.const import Status
from grpclib.exceptions import GRPCError, StreamTerminatedError

from custom_components.aquarea_home import api


class FakeResponse:
    def __init__(self, status: int, payload=None, *, not_json: bool = False) -> None:
        self.status = status
        self._payload = payload
        self._not_json = not_json

    async def json(self, content_type=None):
        if self._not_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    def post(self, *args, **kwargs):
        return self._response

    get = post


def client_for(response: FakeResponse, token: str | None = None) -> api.AquareaHomeClient:
    return api.AquareaHomeClient(FakeSession(response), "me@example.com", "hunter2", token=token)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

async def test_login_keeps_the_token():
    client = client_for(FakeResponse(200, {"token": "t0k3n", "user": {"id": 1}}))
    assert await client.login() == {"id": 1}
    assert client.token == "t0k3n"


async def test_login_401_is_an_auth_error():
    with pytest.raises(api.AuthError):
        await client_for(FakeResponse(401, {"message": "bad password"})).login()


async def test_login_html_error_page_is_not_a_json_traceback():
    with pytest.raises(api.AquareaHomeError, match="HTTP 502"):
        await client_for(FakeResponse(502, not_json=True)).login()


async def test_login_error_never_echoes_the_reply_body():
    body = {"code": 402, "debug": "hunter2 t0k3n"}
    with pytest.raises(api.AquareaHomeError) as caught:
        await client_for(FakeResponse(500, body)).login()
    assert "402" in str(caught.value)
    assert "hunter2" not in str(caught.value) and "t0k3n" not in str(caught.value)


async def test_login_200_without_token_is_an_error():
    with pytest.raises(api.AquareaHomeError, match="no token"):
        await client_for(FakeResponse(200, {"user": {}})).login()


async def test_devices_are_flattened_and_entries_without_a_mac_skipped():
    homes = [{"id": "h1", "name": "Home", "rooms": [{"id": "r1", "name": "Bedroom"}],
              "devices": [{"macAddress": "AA:BB:CC:11:22:33", "name": "Bedroom AC",
                           "roomId": "r1", "serialNumber": "%IN00000000"},
                          {"name": "no mac"}, "junk"]},
             "junk"]
    devices = await client_for(FakeResponse(200, homes), token="t").get_devices()
    assert devices == [{"mac": "AA:BB:CC:11:22:33", "node_id": 0, "name": "Bedroom AC",
                        "serial": "%IN00000000", "room": "Bedroom", "home": "Home",
                        "home_id": "h1"}]


@pytest.mark.parametrize("status", [401, 403])
async def test_devices_token_rejected(status):
    with pytest.raises(api.AuthError):
        await client_for(FakeResponse(status), token="t").get_devices()


async def test_devices_non_json_and_wrong_shape():
    with pytest.raises(api.AquareaHomeError, match="non-JSON"):
        await client_for(FakeResponse(200, not_json=True), token="t").get_devices()
    with pytest.raises(api.AquareaHomeError, match="unexpected reply shape"):
        await client_for(FakeResponse(200, {"homes": []}), token="t").get_devices()
    assert await client_for(FakeResponse(200, None), token="t").get_devices() == []


# ---------------------------------------------------------------------------
# gRPC
# ---------------------------------------------------------------------------

class FakeStream:
    def __init__(self, error: BaseException | None, reply: bytes) -> None:
        self._error = error
        self._reply = reply

    async def send_message(self, message, end=False):
        if self._error is not None:
            raise self._error

    async def recv_message(self):
        return api.RawMessage(self._reply)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeChannel:
    def __init__(self, error: BaseException | None = None, reply: bytes = b"") -> None:
        self._error = error
        self._reply = reply
        self.closed = False

    def request(self, *args, **kwargs):
        return FakeStream(self._error, self._reply)

    def close(self):
        self.closed = True


def grpc_client(channel: FakeChannel) -> api.AquareaHomeClient:
    client = client_for(FakeResponse(500), token="t")
    client._ssl = object()          # skip building a real TLS context
    client._channel = channel
    return client


@pytest.mark.parametrize(("error", "raised", "unit_level", "closes"), [
    (GRPCError(Status.UNAUTHENTICATED, "InvalidAudience"), api.AuthError, False, False),
    (GRPCError(Status.PERMISSION_DENIED, "nope"), api.UnitForbidden, True, False),
    (GRPCError(Status.NOT_FOUND, "nope"), api.UnitForbidden, True, False),
    (GRPCError(Status.DEADLINE_EXCEEDED, "slow"), api.RequestTimeout, True, True),
    (GRPCError(Status.UNAVAILABLE, "down"), api.AquareaHomeError, False, True),
    (GRPCError(Status.RESOURCE_EXHAUSTED, "slow down"), api.AquareaHomeError, False, True),
    (GRPCError(Status.INTERNAL, "oops"), api.UnitError, True, False),
    (GRPCError(Status.FAILED_PRECONDITION, "no"), api.UnitError, True, False),
    (asyncio.TimeoutError(), api.RequestTimeout, True, True),
    (OSError(-3, "Try again"), api.AquareaHomeError, False, True),
    (StreamTerminatedError("reset"), api.AquareaHomeError, False, True),
    (RuntimeError("h2 protocol error"), api.AquareaHomeError, False, True),
])
async def test_grpc_failures_map_to_the_right_class(error, raised, unit_level, closes):
    channel = FakeChannel(error)
    client = grpc_client(channel)
    with pytest.raises(api.AquareaHomeError) as caught:
        await client.get_state("AA:BB:CC:11:22:33")
    assert type(caught.value) is raised
    assert isinstance(caught.value, api.DeviceOffline) is unit_level
    assert channel.closed is closes
    assert (client._channel is None) is closes


async def test_a_failed_call_does_not_close_a_newer_channel():
    old, new = FakeChannel(), FakeChannel()
    client = grpc_client(new)
    client._drop_channel(old)               # the call that failed was on `old`
    assert old.closed and not new.closed and client._channel is new


async def test_set_state_error_wrapper_names_the_code():
    client = grpc_client(FakeChannel(reply=api._ld(1, api._vint(1, 1))))
    with pytest.raises(api.DeviceOffline, match="RESPONSE_TIMEOUT"):
        await client.set_state("AA:BB:CC:11:22:33", power=True)


def _state_reply(ac: bytes, meta: bytes = b"") -> bytes:
    node = api._ld(2, api._ld(2, api._ld(1, ac)))           # nodes entry -> Node.ac
    return api._ld(2, api._ld(1, api._ld(1, meta + node)))


@pytest.mark.parametrize("reply", [
    _state_reply(api._ld(3, api._ld(1, b"x"))),              # setpoint value as bytes
    _state_reply(b"", api._ld(1, api._ld(4, api._ld(2, api._ld(1, api._ld(2, b"x")))))),
    b"\x12\x05ab",                                           # truncated
], ids=["setpoint-type", "rssi-type", "truncated"])
async def test_a_reply_the_codec_cannot_read_is_that_units_problem(reply):
    client = grpc_client(FakeChannel(reply=reply))
    with pytest.raises(api.BadReply):
        await client.get_state("AA:BB:CC:11:22:33")

