"""The few Home Assistant names the integration touches, as in-memory fakes.

Enough to exercise the integration's own logic (per-unit polling, re-login
throttle, state cache, commands, setup and unload, entity availability)
without installing Home Assistant. It says nothing about Home Assistant's
real behaviour; hassfest and a live install cover that side."""
import enum
import sys
import types


class ConfigEntryAuthFailed(Exception):
    pass


class ConfigEntryNotReady(Exception):
    pass


class HomeAssistantError(Exception):
    pass


class ServiceValidationError(HomeAssistantError):
    pass


class UpdateFailed(Exception):
    pass


class Store:
    """.storage, as a dict shared by every Store of one test."""

    files: dict[str, object] = {}

    def __init__(self, hass, version, key) -> None:
        self.key = key
        self.pending = None

    async def async_load(self):
        return Store.files.get(self.key)

    async def async_save(self, data) -> None:
        self.pending = None
        Store.files[self.key] = data

    def async_delay_save(self, data_func, delay=0) -> None:
        self.pending = data_func

    async def async_remove(self) -> None:
        Store.files.pop(self.key, None)


class DataUpdateCoordinator:
    def __init__(self, hass, logger, *, config_entry=None, name=None,
                 update_interval=None) -> None:
        self.hass = hass
        self.config_entry = config_entry
        self.data = None
        self.last_update_success = True
        self.refresh_requests = 0

    def async_set_updated_data(self, data) -> None:
        self.data = data
        self.last_update_success = True

    async def async_request_refresh(self) -> None:
        self.refresh_requests += 1

    async def poll(self):
        """What Home Assistant does with _async_update_data on each tick:
        a failed update leaves self.data alone."""
        try:
            self.data = await self._async_update_data()
        except UpdateFailed:
            self.last_update_success = False
            raise
        self.last_update_success = True
        return self.data

    async def async_config_entry_first_refresh(self) -> None:
        try:
            await self.poll()
        except UpdateFailed as err:
            raise ConfigEntryNotReady(str(err)) from err


class CoordinatorEntity:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success


class ConfigEntry:
    def __init__(self, entry_id: str = "entry1") -> None:
        self.entry_id = entry_id
        self.data = {"email": "me@example.com", "password": "hunter2"}
        self.on_unload: list = []
        self.reauth_started = 0
        self.background: list = []

    def async_on_unload(self, func) -> None:
        self.on_unload.append(func)

    def async_start_reauth(self, hass) -> None:
        self.reauth_started += 1

    def async_create_background_task(self, hass, coro, name):
        coro.close()            # the 2 s confirm re-poll is not under test
        self.background.append(name)


def fake_hass():
    async def forward(entry, platforms):
        return None

    async def unload(entry, platforms):
        return True

    return types.SimpleNamespace(data={}, config_entries=types.SimpleNamespace(
        async_forward_entry_setups=forward, async_unload_platforms=unload))


class HVACMode(str, enum.Enum):
    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    AUTO = "auto"
    DRY = "dry"
    FAN_ONLY = "fan_only"


class ClimateEntityFeature(enum.IntFlag):
    TARGET_TEMPERATURE = 1
    FAN_MODE = 8
    SWING_MODE = 32
    TURN_OFF = 128
    TURN_ON = 256


def install() -> None:
    if "homeassistant" in sys.modules:
        return

    def module(name: str, **names) -> None:
        mod = types.ModuleType(name)
        mod.__dict__.update(names)
        sys.modules[name] = mod

    space = types.SimpleNamespace
    module("homeassistant")
    module("homeassistant.config_entries", ConfigEntry=ConfigEntry)
    module("homeassistant.const", CONF_EMAIL="email", CONF_PASSWORD="password",
           Platform=space(CLIMATE="climate", SENSOR="sensor"),
           ATTR_TEMPERATURE="temperature", UnitOfTemperature=space(CELSIUS="°C"),
           SIGNAL_STRENGTH_DECIBELS_MILLIWATT="dBm",
           EntityCategory=space(DIAGNOSTIC="diagnostic"))
    module("homeassistant.core", HomeAssistant=object)
    module("homeassistant.exceptions", ConfigEntryAuthFailed=ConfigEntryAuthFailed,
           ConfigEntryNotReady=ConfigEntryNotReady, HomeAssistantError=HomeAssistantError,
           ServiceValidationError=ServiceValidationError)
    module("homeassistant.helpers")
    module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
    module("homeassistant.helpers.storage", Store=Store)
    module("homeassistant.helpers.update_coordinator",
           DataUpdateCoordinator=DataUpdateCoordinator, UpdateFailed=UpdateFailed,
           CoordinatorEntity=CoordinatorEntity)
    module("homeassistant.helpers.device_registry", DeviceInfo=dict)
    module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    module("homeassistant.helpers.restore_state", RestoreEntity=type("RestoreEntity", (), {}))
    module("homeassistant.components")
    module("homeassistant.components.sensor", SensorEntity=type("SensorEntity", (), {}),
           SensorDeviceClass=space(TEMPERATURE="temperature",
                                   SIGNAL_STRENGTH="signal_strength"),
           SensorStateClass=space(MEASUREMENT="measurement"))
    module("homeassistant.components.climate", ClimateEntity=type("ClimateEntity", (), {}),
           ClimateEntityFeature=ClimateEntityFeature, HVACMode=HVACMode)
