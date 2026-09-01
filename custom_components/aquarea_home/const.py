"""Constants for the Aquarea Home integration (v2 cloud API)."""

DOMAIN = "aquarea_home"

# v2 backend (the one the 2.1+/3.x Aquarea Home apps use). The original v1
# login endpoint (api.aquarea-home.solutiontech.tech/api/users/login) died on
# 2026-08-31 — every login, valid or not, returned 500 {"code": 402}.
REST_BASE = "https://v2.api.aquarea-home.solutiontech.tech/app"
GRPC_HOST = "v2.grpc.aquarea-home.solutiontech.tech"
GRPC_PORT = 443
GRPC_SERVICE = "/services.app.AppService"

UPDATE_INTERVAL_SECONDS = 30          # get_state poll cadence
COMMAND_REFRESH_DELAY_SECONDS = 2     # re-poll after a command lands

# AcSetState.hvac_mode / AcState hvac_mode (v2 enum, confirmed live)
MODE_AUTO = 1
MODE_HEAT = 2
MODE_COOL = 3
MODE_DRY = 4
MODE_FAN = 5

# AcSetState.fan_speed / AcState fan_speed (v2 enum, confirmed live)
FAN_AUTO = 1
FAN_LOW = 2
FAN_MEDIUM = 3
FAN_HIGH = 4
FAN_MAX = 5
