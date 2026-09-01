"""Aquarea Home (Panasonic RAC Solo / Innova) integration — v2 cloud API."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AquareaHomeClient, AquareaHomeError, AuthError, DeviceOffline
from .const import COMMAND_REFRESH_DELAY_SECONDS, DOMAIN, UPDATE_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CLIMATE, Platform.SENSOR]

# consecutive poll misses tolerated on last-known state before the entities
# go unavailable (the backend browns out for a few minutes at a time)
POLL_FAILURE_GRACE = 3


class AquareaHomeCoordinator(DataUpdateCoordinator):
    """Polls SendDevice(get_state) for every unit on the account.

    The v2 backend returns the full climate block on every poll, so the
    stream-first machinery of v0.2.x is gone; commands are followed by a
    quick re-poll for confirmation. The bearer token is persisted because
    v2 tokens are valid for a year — a restart never needs a fresh login,
    which is what took the v1 integration down when its login endpoint
    died on 2026-08-31."""

    def __init__(self, hass: HomeAssistant, client: AquareaHomeClient,
                 devices: list[dict], entry_id: str,
                 auth_store: Store) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.client = client
        self.devices = devices
        self._auth_store = auth_store
        # last-good-state cache: survives restarts through cloud outages —
        # RestoreEntity can't help when the entity was already unavailable
        # at shutdown (learned the hard way, 2026-07-09)
        self._store: Store = Store(hass, 1, f"{DOMAIN}.{entry_id}.state")
        self._poll_failures = 0
        self._relogin_lock = asyncio.Lock()

    async def async_load_cache(self) -> None:
        cached = await self._store.async_load()
        if cached:
            self.data = cached
            _LOGGER.debug("seeded state from cache for %s", list(cached))

    def _save_cache(self) -> None:
        self._store.async_delay_save(lambda: self.data or {}, 30)

    async def async_save_token(self) -> None:
        await self._auth_store.async_save({"token": self.client.token})

    async def _relogin(self) -> None:
        """Token rejected: log in once with the stored password."""
        async with self._relogin_lock:
            try:
                await self.client.login()
            except AuthError as err:
                raise ConfigEntryAuthFailed from err
            await self.async_save_token()
            _LOGGER.info("re-authenticated with the Aquarea Home cloud")

    async def _poll_all(self) -> dict[str, dict]:
        data: dict[str, dict] = {}
        for dev in self.devices:
            data[dev["mac"]] = await self.client.get_state(dev["mac"], dev.get("node_id", 0))
        return data

    async def _async_update_data(self) -> dict[str, dict]:
        try:
            try:
                data = await self._poll_all()
            except AuthError:
                await self._relogin()
                data = await self._poll_all()
        except ConfigEntryAuthFailed:
            raise
        except (DeviceOffline, AquareaHomeError, asyncio.TimeoutError) as err:
            self._poll_failures += 1
            if self._poll_failures >= POLL_FAILURE_GRACE or not self.data:
                raise UpdateFailed(str(err)) from err
            _LOGGER.info("status poll failed (%s); keeping last data (%d/%d)",
                         err, self._poll_failures, POLL_FAILURE_GRACE)
            return self.data
        self._poll_failures = 0
        self.data = data
        self._save_cache()
        return data

    async def async_command(self, mac: str, node_id: int, optimistic: dict[str, Any],
                            **fields: Any) -> None:
        """Send a partial set_state, apply the expected result locally so the
        UI answers at once, then confirm with a poll."""
        try:
            await self.client.set_state(mac, node_id, **fields)
        except AuthError:
            await self._relogin()
            await self.client.set_state(mac, node_id, **fields)
        data = dict(self.data or {})
        status = dict(data.get(mac) or {})
        status.update(optimistic)
        data[mac] = status
        self.async_set_updated_data(data)
        self._save_cache()
        self.hass.async_create_task(self._refresh_after_command())

    async def _refresh_after_command(self) -> None:
        await asyncio.sleep(COMMAND_REFRESH_DELAY_SECONDS)
        await self.async_request_refresh()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    auth_store: Store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.auth")
    saved = await auth_store.async_load() or {}
    client = AquareaHomeClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD],
        token=saved.get("token"),
    )
    try:
        try:
            devices = await client.get_devices()
        except AuthError:
            # stale/absent token: one fresh login, then the password itself
            # is judged
            await client.login()
            devices = await client.get_devices()
    except AuthError as err:
        raise ConfigEntryAuthFailed from err
    except (AquareaHomeError, asyncio.TimeoutError) as err:
        raise ConfigEntryNotReady(str(err)) from err
    await auth_store.async_save({"token": client.token})

    if not devices:
        _LOGGER.warning("No devices found in Aquarea Home account")

    coordinator = AquareaHomeCoordinator(hass, client, devices, entry.entry_id, auth_store)
    await coordinator.async_load_cache()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coordinator: AquareaHomeCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.client.close()
    return ok
