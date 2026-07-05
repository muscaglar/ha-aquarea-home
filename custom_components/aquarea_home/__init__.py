"""Aquarea Home (Panasonic RAC Solo / Innova) integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AquareaHomeClient, AquareaHomeError, AuthError
from .const import DOMAIN, STREAM_HEALTHY_POLL_SECONDS, UPDATE_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CLIMATE, Platform.SENSOR]


class AquareaHomeCoordinator(DataUpdateCoordinator):
    """Status via push events (SubscribeToDeviceEvents) with polling as
    reconciliation: 300s while the stream is healthy, 60s when it is not."""

    def __init__(self, hass: HomeAssistant, client: AquareaHomeClient,
                 devices: list[dict]) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.client = client
        self.devices = devices
        # lost-update protection: events that land while a poll is in
        # flight must win over the poll's older snapshot, per field
        self._event_seq = 0
        self._last_events: dict[str, dict[str, tuple[int, object]]] = {}

    # -- push event stream ---------------------------------------------

    _EVENT_FIELDS = {
        255: ("power", lambda v: bool(v)),
        254: ("setpoint", lambda v: v / 10),
        253: ("room_temperature", lambda v: v / 10),
        252: ("operation_mode", lambda v: v),
        251: ("fan_speed", lambda v: v),
        250: ("flap", lambda v: v),
    }

    def _apply_event(self, mac: str, etype: int, value: int) -> None:
        mapping = self._EVENT_FIELDS.get(etype)
        if mapping is None:
            return
        field, convert = mapping
        parsed = convert(value)
        self._event_seq += 1
        self._last_events.setdefault(mac, {})[field] = (self._event_seq, parsed)

        data = dict(self.data or {})
        status = dict(data.get(mac) or {})
        status[field] = parsed
        data[mac] = status
        # update state + notify WITHOUT async_set_updated_data: that would
        # cancel/reschedule the reconciliation poll on every event (starving
        # it) and silently cancel pending debounced refreshes
        self.data = data
        self.async_update_listeners()

    async def stream_events(self, mac: str) -> None:
        """Long-running per-device task: consume push events, reconnect
        with backoff, and adapt the polling interval to stream health."""
        backoff = 5
        while True:
            try:
                connected = False
                async for etype, value in self.client.subscribe_events(mac):
                    if not connected:
                        connected = True
                        backoff = 5
                        self.update_interval = timedelta(
                            seconds=STREAM_HEALTHY_POLL_SECONDS)
                        _LOGGER.debug("event stream up for %s", mac)
                    self._apply_event(mac, etype, value)
            except asyncio.CancelledError:
                raise
            except AuthError:
                # credentials are bad — retrying would hammer the login
                # endpoint forever. The poll path raises ConfigEntryAuthFailed
                # which starts the reauth flow; entry reload recreates us.
                _LOGGER.warning(
                    "event stream for %s stopped: authentication failed", mac)
                self.update_interval = timedelta(seconds=UPDATE_INTERVAL_SECONDS)
                return
            except Exception as err:  # noqa: BLE001 — any transport error
                _LOGGER.debug("event stream for %s dropped: %s", mac, err)
            self.update_interval = timedelta(seconds=UPDATE_INTERVAL_SECONDS)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    async def _async_update_data(self) -> dict[str, dict]:
        start_seq = self._event_seq
        data: dict[str, dict] = {}
        for dev in self.devices:
            try:
                data[dev["mac"]] = await self.client.get_status(dev["mac"])
            except AuthError as err:
                raise ConfigEntryAuthFailed from err
            except AquareaHomeError as err:
                raise UpdateFailed(str(err)) from err
            except Exception as err:  # noqa: BLE001 — grpc transport errors
                raise UpdateFailed(f"gRPC error: {err}") from err
        # overlay events that arrived while the poll was in flight — they
        # are newer than the snapshot and must not be overwritten
        for mac, fields in self._last_events.items():
            if mac in data:
                for field, (seq, value) in fields.items():
                    if seq > start_seq:
                        data[mac][field] = value
        return data


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = AquareaHomeClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD],
    )
    try:
        await client.login()
        devices = await client.get_devices()
    except AuthError as err:
        raise ConfigEntryAuthFailed from err
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(str(err)) from err

    if not devices:
        _LOGGER.warning("No devices found in Aquarea Home account")

    coordinator = AquareaHomeCoordinator(hass, client, devices)
    await coordinator.async_config_entry_first_refresh()

    for dev in devices:
        entry.async_create_background_task(
            hass, coordinator.stream_events(dev["mac"]),
            name=f"{DOMAIN}_stream_{dev['mac']}",
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coordinator: AquareaHomeCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.client.close()
    return ok
