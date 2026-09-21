"""The coordinator's own logic, against in-memory Home Assistant fakes."""
import asyncio
import logging
import types

import pytest

import custom_components.aquarea_home as integration
import ha_stub
from custom_components.aquarea_home import api

UNITS = [{"mac": m, "name": n, "node_id": 0}
         for m, n in (("A", "Lounge"), ("B", "Bedroom"), ("C", "Office"), ("D", "Loft"))]
ON = {"power": True, "setpoint": 21.0, "room_temperature": 22.0, "wifi_rssi": -60}
STATE_FILE = "aquarea_home.entry1.state"
AUTH_FILE = "aquarea_home.entry1.auth"


class FakeClient:
    """get_state answers from a per-MAC script: a dict, an exception, or a
    callable taking the client (for answers that depend on the token).
    set_state raises the next entry of command_errors, if any."""

    def __init__(self, script=None, token="t1", login_error=None, devices=UNITS) -> None:
        self.script = script or {}
        self.token = token
        self.login_error = login_error
        self.devices = devices
        self.logins = 0
        self.closed = 0
        self.asked: list[str] = []
        self.commands: list[tuple] = []
        self.command_errors: list[Exception] = []

    async def login(self):
        self.logins += 1
        if self.login_error is not None:
            raise self.login_error
        self.token = f"t{self.logins + 1}"

    async def get_devices(self):
        if isinstance(self.devices, Exception):
            raise self.devices
        if callable(self.devices):
            return self.devices(self)
        return self.devices

    async def get_state(self, mac, node_id=0):
        self.asked.append(mac)
        answer = self.script.get(mac, ON)
        if callable(answer):
            answer = answer(self)
        if asyncio.iscoroutine(answer):
            answer = await answer
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    async def set_state(self, mac, node_id=0, **fields):
        if self.command_errors:
            raise self.command_errors.pop(0)
        self.commands.append((mac, fields))

    def close(self):
        self.closed += 1


def make(script=None, *, devices=UNITS, last_login_at=None, **client_kwargs):
    ha_stub.Store.files.clear()
    client = FakeClient(script, **client_kwargs)
    entry = ha_stub.ConfigEntry()
    coordinator = integration.AquareaHomeCoordinator(
        None, entry, client, devices, integration._auth_store(None, entry.entry_id),
        last_login_at)
    return coordinator, client, entry


@pytest.fixture
def clock(monkeypatch):
    """The integration's view of time, without touching the event loop's."""
    now = [1_800_000_000.0]
    monkeypatch.setattr(integration, "time", types.SimpleNamespace(
        time=lambda: now[0], monotonic=lambda: now[0]))
    return now


def rejects(*tokens):
    return lambda client: api.AuthError("UNAUTHENTICATED") if client.token in tokens else ON


# ---------------------------------------------------------------------------
# per-unit isolation
# ---------------------------------------------------------------------------

async def test_a_unit_offline_at_first_refresh_does_not_block_setup():
    coordinator, _, _ = make({"B": api.DeviceOffline("RESPONSE_TIMEOUT")})
    data = await coordinator.poll()
    assert sorted(data) == ["A", "C", "D"] and coordinator.last_update_success


async def test_nothing_reachable_and_nothing_cached_fails_the_update_naming_the_unit():
    coordinator, _, _ = make({d["mac"]: api.DeviceOffline("OFFLINE") for d in UNITS})
    with pytest.raises(ha_stub.UpdateFailed, match="Lounge: OFFLINE"):
        await coordinator.poll()


async def test_one_unit_drops_alone_after_the_grace_and_comes_back():
    coordinator, client, _ = make()
    await coordinator.poll()
    client.script["B"] = api.DeviceOffline("RESPONSE_TIMEOUT")
    assert "B" in await coordinator.poll()              # miss 1
    assert "B" in await coordinator.poll()              # miss 2
    data = await coordinator.poll()                     # miss 3
    assert sorted(data) == ["A", "C", "D"] and coordinator.last_update_success
    del client.script["B"]
    assert sorted(await coordinator.poll()) == ["A", "B", "C", "D"]


async def test_cloud_outage_rides_the_grace_then_fails_then_recovers():
    coordinator, client, _ = make()
    await coordinator.poll()
    client.script["A"] = api.AquareaHomeError("gRPC transport error: [Errno -3] Try again")
    assert len(await coordinator.poll()) == 4
    assert len(await coordinator.poll()) == 4
    with pytest.raises(ha_stub.UpdateFailed, match=r"^gRPC transport error.*Try again"):
        await coordinator.poll()                        # not a unit's fault: no unit named
    del client.script["A"]
    assert len(await coordinator.poll()) == 4 and coordinator.last_update_success


async def test_two_slow_units_listed_first_do_not_take_the_healthy_ones_down():
    slow = api.RequestTimeout("no reply within 20 s")
    coordinator, client, _ = make({"A": slow, "B": slow})
    for _ in range(5):
        assert sorted(await coordinator.poll()) == ["C", "D"]
    assert client.asked[-4:] == ["C", "D", "A", "B"]   # whoever answered goes first


async def test_an_account_without_units_is_not_a_failure():
    coordinator, _, _ = make(devices=[])
    assert await coordinator.poll() == {}


# ---------------------------------------------------------------------------
# token and password
# ---------------------------------------------------------------------------

async def test_an_expired_token_costs_one_login_and_the_new_one_is_saved(clock):
    coordinator, client, _ = make({d["mac"]: rejects("t1") for d in UNITS})
    assert len(await coordinator.poll()) == 4
    assert client.logins == 1
    assert ha_stub.Store.files[AUTH_FILE] == {"token": "t2", "login_at": clock[0]}
    await coordinator.poll()
    assert client.logins == 1


async def test_a_token_rejected_right_after_login_is_not_retried_every_poll(clock, caplog):
    always = lambda client: api.AuthError("UNAUTHENTICATED: InvalidAudience")  # noqa: E731
    coordinator, client, _ = make({d["mac"]: always for d in UNITS})
    with caplog.at_level(logging.WARNING):
        for _ in range(6):
            clock[0] += 30
            with pytest.raises(ha_stub.UpdateFailed):
                await coordinator.poll()
    assert client.logins == 1
    assert len([r for r in caplog.records if "rejects a token" in r.getMessage()]) == 1
    clock[0] += integration.RELOGIN_MIN_INTERVAL_SECONDS
    with pytest.raises(ha_stub.UpdateFailed):
        await coordinator.poll()
    assert client.logins == 2


async def test_the_throttle_survives_a_new_coordinator(clock):
    # a setup retry loop and a restart both build a fresh coordinator
    always = lambda client: api.AuthError("UNAUTHENTICATED")  # noqa: E731
    coordinator, client, _ = make({d["mac"]: always for d in UNITS},
                                  last_login_at=clock[0] - 60)
    with pytest.raises(ha_stub.UpdateFailed, match="right after a fresh login"):
        await coordinator.poll()
    assert client.logins == 0


async def test_a_failed_login_does_not_arm_the_throttle(clock, caplog):
    coordinator, client, _ = make({d["mac"]: rejects("t1") for d in UNITS},
                                  login_error=api.AquareaHomeError("login failed: HTTP 502"))
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ha_stub.UpdateFailed, match="HTTP 502"):
            await coordinator.poll()
        clock[0] += 30                                  # still inside the short back-off
        with pytest.raises(ha_stub.UpdateFailed, match="retrying shortly"):
            await coordinator.poll()
        assert client.logins == 1
        client.login_error = None
        clock[0] += 30
        assert len(await coordinator.poll()) == 4       # third poll: back within the grace
    assert client.logins == 2
    assert not [r for r in caplog.records if "rejects a token" in r.getMessage()]


async def test_a_rejected_password_goes_to_reauth_and_is_not_tried_again():
    coordinator, client, _ = make({d["mac"]: rejects("t1") for d in UNITS},
                                  login_error=api.AuthError("invalid credentials"))
    for _ in range(3):
        with pytest.raises(ha_stub.ConfigEntryAuthFailed):
            await coordinator.poll()
    assert client.logins == 1


async def test_poll_and_command_racing_on_an_expired_token_log_in_once():
    gate = asyncio.Event()

    class Racy(FakeClient):
        async def login(self):
            await gate.wait()
            await super().login()

        async def set_state(self, mac, node_id=0, **fields):
            if self.token == "t1":
                raise api.AuthError("UNAUTHENTICATED")
            await super().set_state(mac, node_id, **fields)

    ha_stub.Store.files.clear()
    client = Racy({d["mac"]: rejects("t1") for d in UNITS})
    entry = ha_stub.ConfigEntry()
    coordinator = integration.AquareaHomeCoordinator(
        None, entry, client, UNITS, integration._auth_store(None, entry.entry_id))
    poll = asyncio.ensure_future(coordinator.poll())
    await asyncio.sleep(0)
    command = asyncio.ensure_future(
        coordinator.async_command("A", 0, {"power": False}, power=False))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(poll, command)
    assert client.logins == 1 and client.commands == [("A", {"power": False})]


async def test_every_unit_refused_tries_one_fresh_token_then_reports_the_refusal(clock):
    refused = api.UnitForbidden("gRPC PERMISSION_DENIED: not yours")
    coordinator, client, _ = make({"A": refused}, devices=UNITS[:1])
    for _ in range(4):
        clock[0] += 30
        with pytest.raises(ha_stub.UpdateFailed, match="Lounge: gRPC PERMISSION_DENIED"):
            await coordinator.poll()
    clock[0] += integration.RELOGIN_MIN_INTERVAL_SECONDS + 60
    with pytest.raises(ha_stub.UpdateFailed, match="PERMISSION_DENIED"):
        await coordinator.poll()
    assert client.logins == 1                           # not one per poll, not one per 10 min
    clock[0] += integration.FORBIDDEN_RELOGIN_SECONDS
    with pytest.raises(ha_stub.UpdateFailed):
        await coordinator.poll()
    assert client.logins == 2                           # the hedge is kept, once a day


async def test_every_unit_refused_under_a_stale_token_is_cured_by_the_login(clock):
    stale = lambda c: api.UnitForbidden("PERMISSION_DENIED") if c.token == "t1" else ON  # noqa: E731
    coordinator, client, _ = make({d["mac"]: stale for d in UNITS})
    assert len(await coordinator.poll()) == 4 and client.logins == 1


async def test_one_refused_unit_among_others_never_costs_a_login():
    coordinator, client, _ = make({"C": api.UnitForbidden("PERMISSION_DENIED")})
    assert sorted(await coordinator.poll()) == ["A", "B", "D"] and client.logins == 0


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

async def test_command_applies_the_expected_state_and_schedules_the_confirm_poll():
    coordinator, client, entry = make()
    await coordinator.poll()
    await coordinator.async_command("A", 0, {"power": False}, power=False)
    assert client.commands == [("A", {"power": False})]
    assert coordinator.data["A"]["power"] is False and coordinator.data["B"]["power"] is True
    assert entry.background == ["aquarea_home confirm command"]


async def test_a_poll_already_in_flight_does_not_undo_a_command():
    gate = asyncio.Event()

    async def slow_answer():
        await gate.wait()
        return ON                                       # read before the command

    coordinator, client, _ = make()
    await coordinator.poll()
    client.script["A"] = lambda _client: slow_answer()
    in_flight = asyncio.ensure_future(coordinator.poll())
    await asyncio.sleep(0)
    await coordinator.async_command("A", 0, {"power": False}, power=False)
    gate.set()
    data = await in_flight
    assert data["A"]["power"] is False
    # and the guard lets go again: the next poll's news is taken
    client.script["A"] = {**ON, "power": False, "setpoint": 19.0}
    assert (await coordinator.poll())["A"]["setpoint"] == 19.0


async def test_command_failure_is_a_home_assistant_error():
    coordinator, client, _ = make()
    await coordinator.poll()
    client.command_errors = [api.DeviceOffline("command not delivered (RESPONSE_TIMEOUT)")]
    with pytest.raises(ha_stub.HomeAssistantError, match="RESPONSE_TIMEOUT"):
        await coordinator.async_command("A", 0, {"power": False}, power=False)
    assert coordinator.data["A"]["power"] is True and client.commands == []


async def test_a_command_cut_off_by_a_closed_channel_is_sent_once_more():
    coordinator, client, _ = make()
    await coordinator.poll()
    client.command_errors = [api.AquareaHomeError("gRPC transport error: Connection lost")]
    await coordinator.async_command("A", 0, {"setpoint": 19.0}, setpoint=19.0)
    assert client.commands == [("A", {"setpoint": 19.0})]
    client.command_errors = [api.AquareaHomeError("gone"), api.AquareaHomeError("still gone")]
    with pytest.raises(ha_stub.HomeAssistantError, match="still gone"):
        await coordinator.async_command("A", 0, {"setpoint": 18.0}, setpoint=18.0)


async def test_a_delivered_command_restarts_the_units_grace():
    coordinator, client, _ = make(devices=UNITS[:2])
    await coordinator.poll()
    client.script["A"] = api.DeviceOffline("RESPONSE_TIMEOUT")
    await coordinator.poll()
    await coordinator.poll()                            # two misses
    await coordinator.async_command("A", 0, {"power": False}, power=False)
    assert "A" in await coordinator.poll()              # counts from one again


async def test_a_command_for_a_unit_dropped_meanwhile_leaves_no_partial_state():
    coordinator, client, entry = make(devices=UNITS[:2])
    await coordinator.poll()
    client.script["A"] = api.DeviceOffline("OFFLINE")
    for _ in range(3):
        await coordinator.poll()
    assert "A" not in coordinator.data
    await coordinator.async_command("A", 0, {"setpoint": 19.0}, setpoint=19.0)
    assert "A" not in coordinator.data and entry.background    # confirm poll still asked for
    assert "A" not in coordinator._cache_units                 # and no one-key stub cached


async def test_command_with_a_rejected_password_starts_reauth():
    coordinator, client, entry = make(login_error=api.AuthError("invalid credentials"))
    await coordinator.poll()
    client.command_errors = [api.AuthError("UNAUTHENTICATED")]
    with pytest.raises(ha_stub.HomeAssistantError, match="re-authenticate"):
        await coordinator.async_command("A", 0, {"power": False}, power=False)
    assert entry.reauth_started == 1


# ---------------------------------------------------------------------------
# state cache
# ---------------------------------------------------------------------------

async def test_a_v0_2_cache_with_v1_enums_is_ignored():
    coordinator, _, _ = make()
    ha_stub.Store.files[STATE_FILE] = {"A": {"power": True, "operation_mode": 2}}
    await coordinator.async_load_cache()
    assert coordinator.data is None


async def test_a_stamped_cache_seeds_the_known_units_only():
    coordinator, _, _ = make()
    ha_stub.Store.files[STATE_FILE] = {"api": 2, "units": {"A": ON, "gone": ON, "B": "junk"}}
    await coordinator.async_load_cache()
    assert coordinator.data == {"A": ON}


async def test_cache_is_written_for_real_changes_not_for_temperature_drift():
    coordinator, client, _ = make()
    await coordinator.poll()
    assert coordinator._store.pending is not None
    await coordinator._store.async_save(coordinator._store.pending())
    client.script["A"] = {**ON, "room_temperature": 22.5, "wifi_rssi": -71}
    await coordinator.poll()
    assert coordinator._store.pending is None
    client.script["A"] = {**ON, "setpoint": 19.0}
    await coordinator.poll()
    assert coordinator._store.pending is not None


async def test_a_pass_with_nothing_fresh_does_not_touch_the_cache():
    coordinator, client, _ = make(devices=UNITS[:2])
    await coordinator.poll()
    await coordinator._store.async_save(coordinator._store.pending())
    client.script.update({"A": api.DeviceOffline("OFFLINE"), "B": api.DeviceOffline("OFFLINE")})
    await coordinator.poll()                            # both ride the grace
    assert coordinator._store.pending is None


async def test_the_cache_is_never_overwritten_with_nothing():
    coordinator, _, _ = make()
    await coordinator.poll()
    coordinator._save_cache({})
    await coordinator.async_flush_cache()
    assert sorted(ha_stub.Store.files[STATE_FILE]["units"]) == ["A", "B", "C", "D"]


async def test_restart_in_an_outage_then_unload_keeps_the_good_cache():
    coordinator, _, _ = make({d["mac"]: api.DeviceOffline("OFFLINE") for d in UNITS})
    ha_stub.Store.files[STATE_FILE] = {"api": 2, "units": {"A": ON, "B": ON}}
    await coordinator.async_load_cache()
    assert sorted(await coordinator.poll()) == ["A", "B"]      # rides the grace on the cache
    await coordinator.async_flush_cache()
    assert ha_stub.Store.files[STATE_FILE]["units"] == {"A": ON, "B": ON}


async def test_a_poll_that_outlives_the_unload_cannot_bring_the_file_back():
    gate = asyncio.Event()

    async def slow_answer():
        await gate.wait()
        return {**ON, "setpoint": 25.0}                 # a real change: would arm a write

    coordinator, client, entry = make()
    await coordinator.poll()
    client.script["A"] = lambda _client: slow_answer()
    in_flight = asyncio.ensure_future(coordinator.poll())
    await asyncio.sleep(0)
    await coordinator.async_flush_cache()
    await integration.async_remove_entry(None, entry)
    gate.set()
    await in_flight
    assert coordinator._store.pending is None and STATE_FILE not in ha_stub.Store.files


# ---------------------------------------------------------------------------
# setup, unload, removal
# ---------------------------------------------------------------------------

def setup_with(monkeypatch, client, *, auth=None, state=None):
    ha_stub.Store.files.clear()
    if auth is not None:
        ha_stub.Store.files[AUTH_FILE] = auth
    if state is not None:
        ha_stub.Store.files[STATE_FILE] = state
    monkeypatch.setattr(integration, "AquareaHomeClient", lambda *a, **k: client)
    return ha_stub.fake_hass(), ha_stub.ConfigEntry()


async def test_setup_registers_close_and_unload_flushes_the_cache(monkeypatch):
    client = FakeClient()
    hass, entry = setup_with(monkeypatch, client, auth={"token": "t1"})
    assert await integration.async_setup_entry(hass, entry)
    assert entry.on_unload == [client.close]
    assert ha_stub.Store.files[AUTH_FILE] == {"token": "t1"}   # unchanged: not rewritten
    assert STATE_FILE not in ha_stub.Store.files                # only the delayed write is armed
    assert await integration.async_unload_entry(hass, entry)
    assert sorted(ha_stub.Store.files[STATE_FILE]["units"]) == ["A", "B", "C", "D"]
    assert "entry1" not in hass.data["aquarea_home"]


async def test_setup_during_an_outage_comes_up_on_the_cached_state(monkeypatch):
    client = FakeClient({d["mac"]: api.DeviceOffline("OFFLINE") for d in UNITS})
    hass, entry = setup_with(monkeypatch, client, auth={"token": "t1"},
                             state={"api": 2, "units": {d["mac"]: ON for d in UNITS}})
    assert await integration.async_setup_entry(hass, entry)
    assert sorted(hass.data["aquarea_home"]["entry1"].data) == ["A", "B", "C", "D"]


async def test_setup_with_the_cloud_down_is_not_ready_and_still_closes(monkeypatch):
    client = FakeClient(devices=api.AquareaHomeError("homes network error"))
    hass, entry = setup_with(monkeypatch, client, auth={"token": "t1"})
    with pytest.raises(ha_stub.ConfigEntryNotReady, match="homes network error"):
        await integration.async_setup_entry(hass, entry)
    assert entry.on_unload == [client.close]            # Home Assistant runs it after a failure


async def test_setup_with_a_stale_token_logs_in_once_and_saves_before_use(monkeypatch, clock):
    seen = []

    def homes(client):
        seen.append(dict(ha_stub.Store.files.get(AUTH_FILE) or {}))
        if client.token == "t1":
            raise api.AuthError("token rejected (HTTP 401)")
        return UNITS

    client = FakeClient(devices=homes)
    hass, entry = setup_with(monkeypatch, client, auth={"token": "t1"})
    assert await integration.async_setup_entry(hass, entry)
    assert client.logins == 1
    assert seen[1] == {"token": "t2", "login_at": clock[0]}    # a retry would start here


async def test_setup_with_a_wrong_password_asks_for_reauth(monkeypatch):
    client = FakeClient(devices=api.AuthError("token rejected (HTTP 401)"),
                        login_error=api.AuthError("invalid credentials"))
    hass, entry = setup_with(monkeypatch, client, auth={"token": "t1"})
    with pytest.raises(ha_stub.ConfigEntryAuthFailed):
        await integration.async_setup_entry(hass, entry)


async def test_setup_retries_do_not_log_in_again_when_fresh_tokens_are_refused(monkeypatch, clock):
    client = FakeClient(devices=api.AuthError("token rejected (HTTP 401)"))
    hass, entry = setup_with(monkeypatch, client, auth={"token": "t1"})
    for _ in range(4):                                  # Home Assistant's retry loop
        clock[0] += 80
        with pytest.raises(ha_stub.ConfigEntryNotReady):
            await integration.async_setup_entry(hass, entry)
    assert client.logins == 1                           # not a reauth prompt, not four logins


async def test_a_first_setup_stamps_the_implicit_login(monkeypatch, clock):
    def homes(client):
        client.token = "t9"                             # get_devices logs in by itself
        return UNITS

    client = FakeClient(token=None, devices=homes)
    hass, entry = setup_with(monkeypatch, client)
    assert await integration.async_setup_entry(hass, entry)
    assert ha_stub.Store.files[AUTH_FILE] == {"token": "t9", "login_at": clock[0]}


async def test_removing_the_entry_removes_the_token_and_the_cache():
    coordinator, _, entry = make()
    await coordinator.poll()
    await coordinator.async_save_token()
    await coordinator.async_flush_cache()
    assert {STATE_FILE, AUTH_FILE} <= set(ha_stub.Store.files)
    await integration.async_remove_entry(None, entry)
    assert not {STATE_FILE, AUTH_FILE} & set(ha_stub.Store.files)
