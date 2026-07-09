"""Aquarea Home (Panasonic RAC Solo / Innova) integration."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AquareaHomeClient, AuthError
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
        # transient-blip tolerance: the backend drops the odd poll
        self._poll_failures = 0
        self._stream_up = False
        self._partial_poll = False

    def _sync_poll_interval(self) -> None:
        """Single owner of the poll cadence: gentle only while the stream
        is up AND polls are clean. Guarded — HA's update_interval setter
        unconditionally cancels the scheduled refresh and any pending
        debounced refresh, so no-op writes are not free."""
        seconds = (STREAM_HEALTHY_POLL_SECONDS
                   if self._stream_up and self._poll_failures == 0
                   else UPDATE_INTERVAL_SECONDS)
        interval = timedelta(seconds=seconds)
        if self.update_interval != interval:
            self.update_interval = interval

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
            connected_at: float | None = None

            def _connected() -> None:
                # fires when the subscription is accepted (server headers),
                # not on the first event — an idle unit may never send one,
                # and the stream was previously considered down all night
                # because of it
                nonlocal connected_at
                connected_at = time.monotonic()
                if not self._stream_up:
                    self._stream_up = True
                    self._sync_poll_interval()
                    _LOGGER.info("event stream up for %s", mac)

            try:
                async for etype, value in self.client.subscribe_events(
                        mac, on_connect=_connected):
                    self._apply_event(mac, etype, value)
                # graceful server-side stream end (~hourly recycle) — without
                # this line the journal shows 'up' with no matching end and
                # stream continuity can't be audited
                _LOGGER.info("event stream for %s ended by server", mac)
            except asyncio.CancelledError:
                raise
            except AuthError:
                # credentials are bad — retrying would hammer the login
                # endpoint forever. The poll path raises ConfigEntryAuthFailed
                # which starts the reauth flow; entry reload recreates us.
                _LOGGER.warning(
                    "event stream for %s stopped: authentication failed", mac)
                self._stream_up = False
                self._sync_poll_interval()
                return
            except TimeoutError as err:
                if connected_at is not None:
                    # recv idle timeout — routine half-open-TCP protection;
                    # the stream was genuinely up, so keep state and
                    # resubscribe immediately
                    _LOGGER.debug("event stream for %s idle-cycled", mac)
                    continue
                _LOGGER.debug(
                    "event stream for %s connect timed out: %s", mac, err)
            except Exception as err:  # noqa: BLE001 — any transport error
                if connected_at is not None:
                    _LOGGER.info("event stream for %s dropped: %s", mac, err)
                else:
                    _LOGGER.debug(
                        "event stream for %s reconnect failed: %s", mac, err)
            self._stream_up = False
            self._sync_poll_interval()
            # a connection that survived a while earns a fresh backoff; one
            # that died straight after headers must keep growing it, or a
            # flapping backend gets hammered every 5s all night
            if connected_at is not None and time.monotonic() - connected_at > 60:
                backoff = 5
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    async def _async_update_data(self) -> dict[str, dict]:
        start_seq = self._event_seq
        data: dict[str, dict] = {}
        try:
            for dev in self.devices:
                data[dev["mac"]] = await self.client.get_status(dev["mac"])
        except AuthError as err:
            raise ConfigEntryAuthFailed from err
        except Exception as err:  # noqa: BLE001 — grpc transport errors
            # the backend browns out for 2-4 min at a time (observed
            # 2026-07-05); empty status payloads also land here. Grace two
            # cycles on stale data and retry fast; three consecutive
            # misses = genuinely down. Counter is shared across devices —
            # fine for this single-unit account, revisit if a second unit
            # ever appears.
            self._poll_failures += 1
            self._sync_poll_interval()
            if self._poll_failures >= 3 or not self.data:
                raise UpdateFailed(f"gRPC error: {err}") from err
            _LOGGER.warning(
                "status poll failed (%s); keeping last data, retrying in %ss",
                err, UPDATE_INTERVAL_SECONDS)
            return self.data
        self._poll_failures = 0
        self._sync_poll_interval()
        # 2026-07-09 backend change: GetDeviceStatus may return only the iot
        # section (fw/wifi) with the whole climate block missing, while the
        # event stream stays fully live. A partial poll is a HEALTHY
        # transport answer — merge it over last-known state instead of
        # discarding everything; events keep the climate fields current.
        last = self.data or {}
        partial = False
        for mac, status in data.items():
            if "power" not in status:
                partial = True
                merged = dict(last.get(mac) or {})
                merged.update(status)
                data[mac] = merged
        if partial != self._partial_poll:
            self._partial_poll = partial
            _LOGGER.info(
                "status polls are %s the climate section%s",
                "MISSING" if partial else "again carrying",
                " — running stream-first on last-known state" if partial else "")
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
