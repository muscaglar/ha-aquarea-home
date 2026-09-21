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
# consecutive misses a unit rides out on its last-known state before its
# entities go unavailable (the backend browns out for a few minutes at a time)
POLL_FAILURE_GRACE = 3
# a token rejected again right after a fresh login is the backend's problem,
# not the password's: don't hammer the login endpoint every poll
RELOGIN_MIN_INTERVAL_SECONDS = 600
# a login endpoint that is down is not asked again on every 30 s poll either;
# short enough that the retry still lands inside the three-poll grace
LOGIN_RETRY_SECONDS = 60
# every unit answering PERMISSION_DENIED may be the token's doing, or the
# units really are gone from the account: worth one fresh login a day
FORBIDDEN_RELOGIN_SECONDS = 86400

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
