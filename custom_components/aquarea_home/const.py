"""Constants for the Aquarea Home integration."""

DOMAIN = "aquarea_home"

REST_BASE = "https://api.aquarea-home.solutiontech.tech/api"
GRPC_HOST = "grpc.aquarea-home.solutiontech.tech"
GRPC_PORT = 443

UPDATE_INTERVAL_SECONDS = 60

# SetDeviceValue opcodes (DuepuntozeroValueType from the app)
OP_POWER = 1
OP_SETPOINT = 2
OP_MODE = 3
OP_FAN = 4
OP_FLAP = 5

# operation_mode values (DuepuntozeroOperationMode)
MODE_AUTO = 0
MODE_HEAT = 1
MODE_COOL = 2
MODE_FAN = 3
MODE_DRY = 4

# fan_speed values (DuepuntozeroFanSpeed)
FAN_AUTO = 0
FAN_MIN = 1
FAN_MEDIUM = 2
FAN_MAX = 3
