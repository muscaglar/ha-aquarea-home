"""Aquarea Home (Panasonic RAC Solo / Innova) integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AquareaHomeClient, AquareaHomeError, AuthError
from .const import DOMAIN, UPDATE_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CLIMATE, Platform.SENSOR]


class AquareaHomeCoordinator(DataUpdateCoordinator):
    """Polls status for every device on the account."""

    def __init__(self, hass: HomeAssistant, client: AquareaHomeClient,
                 devices: list[dict]) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.client = client
        self.devices = devices

    async def _async_update_data(self) -> dict[str, dict]:
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

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coordinator: AquareaHomeCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.client.close()
    return ok
