"""Aquarea Home (Panasonic RAC Solo) integration — v2 cloud API."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AquareaHomeClient, AquareaHomeError, AuthError, DeviceOffline
from .const import (
    COMMAND_REFRESH_DELAY_SECONDS,
    DOMAIN,
    FORBIDDEN_RELOGIN_SECONDS,
    LOGIN_RETRY_SECONDS,
    POLL_FAILURE_GRACE,
    RELOGIN_MIN_INTERVAL_SECONDS,
    UPDATE_INTERVAL_SECONDS,
)
from .poll import UnitBook, all_forbidden, poll_units

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CLIMATE, Platform.SENSOR]

# the state cache is stamped with the API generation: v0.2.x wrote the same
# file with v1 enum numbering, which must never be shown as v2 state
CACHE_API = 2
# these change every few polls and are not worth a disk write on their own
_VOLATILE_KEYS = ("room_temperature", "wifi_rssi")


def _auth_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, 1, f"{DOMAIN}.{entry_id}.auth")


def _state_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, 1, f"{DOMAIN}.{entry_id}.state")


def _within(stamp: float | None, seconds: float) -> bool:
    """Was the wall-clock `stamp` less than `seconds` ago? (A clock that
    jumped backwards must not throttle forever.)"""
    return stamp is not None and 0 <= time.time() - stamp < seconds


class AquareaHomeCoordinator(DataUpdateCoordinator):
    """Polls SendDevice(get_state) for every unit on the account.

    The v2 backend returns the full climate block on every poll, so the
    stream-first machinery of v0.2.x is gone; commands are followed by a
    quick re-poll for confirmation. Failures are kept per unit: one unit the
    cloud cannot reach must not take the others down with it. The bearer
    token is persisted because v2 tokens are valid for a year — a restart
    never needs a fresh login, which is what took the v1 integration down
    when its login endpoint died on 2026-08-31."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 client: AquareaHomeClient, devices: list[dict],
                 auth_store: Store, last_login_at: float | None = None) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.client = client
        self.devices = devices
        self._names = {d["mac"]: d["name"] for d in devices}
        self._auth_store = auth_store
        # last-good-state cache: survives restarts through cloud outages —
        # RestoreEntity can't help when the entity was already unavailable
        # at shutdown (learned the hard way, 2026-07-09)
        self._store = _state_store(hass, entry.entry_id)
        self._cache_units: dict[str, dict] = {}
        self._cache_stable: dict[str, dict] | None = None
        self._unloaded = False
        self._book = UnitBook(self._names, POLL_FAILURE_GRACE)
        self._answered: set[str] = set()
        # per-unit count of commands applied: a poll that was already in
        # flight when a command landed must not write pre-command state back
        self._cmd_seq: dict[str, int] = {}
        self._relogin_lock = asyncio.Lock()
        # wall clock and persisted with the token, so that neither a setup
        # retry loop nor a restart gets around the throttle
        self._last_login_at = last_login_at
        self._login_failed_at: float | None = None
        self._relogin_warned = False
        self._password_rejected = False

    # ---------------- state cache ----------------

    async def async_load_cache(self) -> None:
        cached = await self._store.async_load()
        if not isinstance(cached, dict) or cached.get("api") != CACHE_API:
            return
        units = cached.get("units")
        if not isinstance(units, dict):
            return
        self.data = {mac: st for mac, st in units.items()
                     if mac in self._names and isinstance(st, dict)}
        _LOGGER.debug("seeded state from cache for %d unit(s)", len(self.data))

    def _cache_payload(self) -> dict[str, Any]:
        return {"api": CACHE_API, "units": self._cache_units}

    def _save_cache(self, data: dict[str, dict]) -> None:
        if self._unloaded or not data:
            return
        self._cache_units = data
        stable = {mac: {k: v for k, v in st.items() if k not in _VOLATILE_KEYS}
                  for mac, st in data.items()}
        if stable == self._cache_stable:
            return
        self._cache_stable = stable
        self._store.async_delay_save(self._cache_payload, 30)

    async def async_flush_cache(self) -> None:
        """Last write, on unload. A poll or command still in flight finishes
        after this; it must not arm the delayed write again, or the file
        would come back after the entry is removed."""
        self._unloaded = True
        if self._cache_stable is not None:
            await self._store.async_save(self._cache_payload())

    # ---------------- auth ----------------

    async def async_save_token(self) -> None:
        if self._unloaded:
            return
        await self._auth_store.async_save(
            {"token": self.client.token, "login_at": self._last_login_at})

    async def _relogin(self, rejected: str | None) -> None:
        """The cloud rejected `rejected`: log in once with the stored password."""
        async with self._relogin_lock:
            if self._password_rejected:
                # only the reauth flow can fix this; don't try the login again
                raise ConfigEntryAuthFailed("password rejected")
            if self.client.token and self.client.token != rejected:
                return  # a concurrent caller already got a fresh token
            if _within(self._last_login_at, RELOGIN_MIN_INTERVAL_SECONDS):
                if not self._relogin_warned:
                    self._relogin_warned = True
                    _LOGGER.warning(
                        "The Aquarea Home cloud rejects a token it has just issued; "
                        "the password is fine, so this is on their side. Logging in "
                        "again at most every %d minutes until it clears",
                        RELOGIN_MIN_INTERVAL_SECONDS // 60)
                raise AquareaHomeError("token rejected again right after a fresh login")
            if (self._login_failed_at is not None and
                    time.monotonic() - self._login_failed_at < LOGIN_RETRY_SECONDS):
                raise AquareaHomeError("login failed a moment ago; retrying shortly")
            try:
                await self.client.login()
            except AuthError as err:
                self._password_rejected = True
                raise ConfigEntryAuthFailed from err
            except AquareaHomeError:
                self._login_failed_at = time.monotonic()
                raise
            # only a login that issued a token can be "rejected right after"
            self._login_failed_at = None
            self._last_login_at = time.time()
            await self.async_save_token()
            _LOGGER.info("re-authenticated with the Aquarea Home cloud")

    # ---------------- polling ----------------

    async def _pass(self) -> tuple[dict[str, dict], dict[str, Exception]]:
        answered, self._answered = self._answered, set()
        fresh, failed = await poll_units(self.client, self.devices, answered_last=answered)
        self._answered = set(fresh)
        return fresh, failed

    async def _poll_once(self) -> tuple[dict[str, dict], dict[str, Exception]]:
        token = self.client.token
        try:
            fresh, failed = await self._pass()
        except AuthError:
            await self._relogin(token)
            return await self._pass()
        if all_forbidden(fresh, failed) and not _within(
                self._last_login_at, FORBIDDEN_RELOGIN_SECONDS):
            # every unit refused at once may be the token's doing: one fresh
            # login settles it, and if that changes nothing it is the units
            try:
                await self._relogin(token)
            except AquareaHomeError:
                return fresh, failed
            return await self._pass()
        return fresh, failed

    async def _async_update_data(self) -> dict[str, dict]:
        seq = dict(self._cmd_seq)
        try:
            fresh, failed = await self._poll_once()
        except ConfigEntryAuthFailed:
            raise
        except (AquareaHomeError, asyncio.TimeoutError) as err:
            # login unreachable, or the fresh token was rejected as well
            fresh, failed = {}, {d["mac"]: err for d in self.devices}
        previous = self.data or {}
        for mac in fresh:
            if self._cmd_seq.get(mac, 0) != seq.get(mac, 0) and mac in previous:
                # a command landed mid-poll; its own re-poll will confirm it
                fresh[mac] = previous[mac]
        data = self._book.settle(previous, fresh, failed)
        if failed and not data:
            mac, err = next(iter(failed.items()))
            # with nothing left this is the only line the log gets, so a
            # unit-level failure has to say which unit
            raise UpdateFailed(f"{self._names.get(mac, 'unit')}: {err}"
                               if isinstance(err, DeviceOffline) else str(err))
        if fresh:
            self._relogin_warned = False
            self._save_cache(data)
        return data

    async def _set_state(self, mac: str, node_id: int, **fields: Any) -> None:
        try:
            await self.client.set_state(mac, node_id, **fields)
        except (AuthError, DeviceOffline):
            raise
        except AquareaHomeError:
            # polls share the gRPC channel and close it when a unit times
            # out, which cuts a command in flight. set_state carries absolute
            # values, so once more on a fresh channel is safe
            await self.client.set_state(mac, node_id, **fields)

    async def async_command(self, mac: str, node_id: int, optimistic: dict[str, Any],
                            **fields: Any) -> None:
        """Send a partial set_state, apply the expected result locally so the
        UI answers at once, then confirm with a poll."""
        token = self.client.token
        try:
            try:
                await self._set_state(mac, node_id, **fields)
            except AuthError:
                await self._relogin(token)
                await self._set_state(mac, node_id, **fields)
        except ConfigEntryAuthFailed as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                "Aquarea Home rejected the stored password; re-authenticate") from err
        except (AquareaHomeError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(f"Aquarea Home: {err}") from err
        self._book.reached(mac)
        self._cmd_seq[mac] = self._cmd_seq.get(mac, 0) + 1
        data = dict(self.data or {})
        if mac in data:
            # a unit dropped while the command was in flight has no state to
            # patch; the confirm poll below brings all of it back
            data[mac] = {**data[mac], **optimistic}
            self.async_set_updated_data(data)
            self._save_cache(data)
        self.config_entry.async_create_background_task(
            self.hass, self._refresh_after_command(), f"{DOMAIN} confirm command")

    async def _refresh_after_command(self) -> None:
        await asyncio.sleep(COMMAND_REFRESH_DELAY_SECONDS)
        await self.async_request_refresh()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    auth_store = _auth_store(hass, entry.entry_id)
    saved = await auth_store.async_load() or {}
    login_at: float | None = saved.get("login_at")
    client = AquareaHomeClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD],
        token=saved.get("token"),
    )
    # runs on unload and after a failed setup alike, so a retry loop does not
    # leave a gRPC connection open per attempt
    entry.async_on_unload(client.close)
    try:
        try:
            devices = await client.get_devices()
        except AuthError:
            # stale token (or none, and the implicit login failed): one fresh
            # login, and only that login judges the password
            if _within(login_at, RELOGIN_MIN_INTERVAL_SECONDS):
                raise ConfigEntryNotReady(
                    "token rejected again right after a fresh login") from None
            try:
                await client.login()
            except AuthError as err:
                raise ConfigEntryAuthFailed from err
            login_at = time.time()
            # saved before it is used: a retry must start from this token
            # and this timestamp, not log in again
            await auth_store.async_save({"token": client.token, "login_at": login_at})
            devices = await client.get_devices()
    except AuthError as err:
        # a token issued seconds ago was refused: their side, not the password
        raise ConfigEntryNotReady(f"fresh token rejected: {err}") from err
    except (AquareaHomeError, asyncio.TimeoutError) as err:
        raise ConfigEntryNotReady(str(err)) from err
    if client.token != saved.get("token") and login_at == saved.get("login_at"):
        # no token was stored, so get_devices logged in by itself
        login_at = time.time()
        await auth_store.async_save({"token": client.token, "login_at": login_at})

    if not devices:
        _LOGGER.warning("No devices found in Aquarea Home account")

    coordinator = AquareaHomeCoordinator(hass, entry, client, devices, auth_store, login_at)
    await coordinator.async_load_cache()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coordinator: AquareaHomeCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_flush_cache()
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Take the year-long bearer token and the state cache out of .storage."""
    await _auth_store(hass, entry.entry_id).async_remove()
    await _state_store(hass, entry.entry_id).async_remove()
