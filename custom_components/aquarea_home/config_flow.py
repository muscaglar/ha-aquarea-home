"""Config flow for Aquarea Home."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AquareaHomeClient, AuthError
from .const import DOMAIN

SCHEMA = vol.Schema({
    vol.Required(CONF_EMAIL): str,
    vol.Required(CONF_PASSWORD): str,
})


class AquareaHomeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Email/password login against the SolutionTech backend."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            client = AquareaHomeClient(
                async_get_clientsession(self.hass),
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD],
            )
            try:
                await client.login()
                devices = await client.get_devices()
            except AuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
                self._abort_if_unique_id_configured()
                title = devices[0]["name"] if len(devices) == 1 else "Aquarea Home"
                return self.async_create_entry(title=title, data=user_input)
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)
