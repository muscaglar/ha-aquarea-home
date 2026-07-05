# Aquarea Home (SolutionTech/Innova) — Protocol Notes

Reverse-engineering log for integrating the Panasonic RAC Solo ("Bedroom AC")
into Home Assistant. Started 2026-07-05.

## Identity

- "Aquarea Home" (Panasonic) is a **white-label of Innova's app**, built by
  SolutionTech (`tech.solutiontech.aquarea_home`, internals `tech.solutiontech.innova`).
- RAC Solo ≈ rebadged **Innova 2.0** ("duepuntozero" in the protocol) — the
  no-outdoor-unit monobloc. Other device families in the app: fancoil, waterloop, m6, m7.
- OEM tenancy exists (`oem_innova` / `oem_panasonic`) but login worked without
  an OEM discriminator.
- The unit itself: WiFi, **zero open TCP ports** (full 65k scan) — cloud + BLE
  provisioning only. Local control impossible (short of BLE RE).
- Device on LAN: <device-lan-ip>, MAC `AA:BB:CC:DD:EE:FF`, serial `%INXXXXXXXX`,
  firmware 50, "Device type 2.0".

## REST API

Base: `https://api.aquarea-home.solutiontech.tech/api/`
Backend: Rust (serde deserialization errors). Clean JSON. JWT bearer auth.

### Auth
- `POST users/login` `{"email","password"}` → `{"user":{...},"token":"<JWT>"}`
  (strict schema: extra fields ignored, missing field → 422 serde error)
- Google SSO variant: `users/login-google` (+ `/nonce`). iOS presumably adds Apple.
- Errors: `{"code":301,"message":"Invalid username or password"}`,
  `{"code":314,"message":"Authentication token not found"}`

### Endpoints (from APK string harvest)
```
users/login  users/login-google  users/login-google/nonce  users/me
users/change-password  users/reset-password  users/send-email-confirmation  users/verify-email
homes  homes/{homeId}  homes/{homeId}/calendars
devices/{macAddress}            GET,HEAD,DELETE,PATCH  (MAC must be colon-separated, case-insensitive)
devices/{macAddress}/preset     PATCH only — body starts {"calendarId": <UUID>} (calendar assignment, NOT live control)
devices/{macAddress}/room       PATCH only
rooms/{roomId}  calendars  calendars/{calendarId}
invites  invites/{inviteId}  invites/accept/{id}  invites/decline/{id}
locations  members  presets
```

### Key observed responses
- `GET homes` → homes[] with members (role: owner), rooms[] each with
  devices[] `{macAddress, name, fwVersionCode, serialNumber, deviceId}`.
- `GET devices/{mac}` → metadata only (name, serial, roomId, homeId,
  isUpdateAvailable). **No live status in REST.**

## gRPC (live status + control)

Host: `grpc.aquarea-home.solutiontech.tech` (port TBD — assume 443/TLS)

Service **`device_controls.Controls`** — methods (from METHODID_ constants):
```
GetDevice, GetDeviceStatus, GetDeviceConfiguration, GetConnectedDevices
SetDeviceValue                      ← primary control verb
SubscribeToDeviceEvents             ← live updates stream
GetCalendar / SetCalendar, GetTimezone
GetFirmware / GetLatestFirmware, RebootDevice, RegisterConnection
ExecuteModbusCommand, RecordModbusRegisters, SetDeviceModbusRegistersTelemetry
```
Also services: `device_telemetry.Telemetry`, `telemetry_modbus_registers.ModbusRegisters`.

Status message fields (protobuf-lite field-name strings; numbers TBD via jadx):
```
powerState_, operationMode_, fanSpeed_, activeSetpointType_,
airHumiditySetpoint_, airQualitySetpoint_, mainStatus_, iotStatus_,
connectionStatus_(Case), deviceStatus_(Case — oneof: duepuntozeroStatus |
fancoilStatus | ...), macAddress(es)_, hotelMode_, modeLock_, batteryStatus_,
powerSupplyType_, managedDeviceType_, additionalData_
```
Fan speed UI enum: auto / min / medium / max.

Auth wrinkle to decode: `device_account_api`, `device_account_jwt_creation` —
the gRPC channel may use a device-scoped JWT minted separately from the login JWT.

## Plan

1. jadx-decompile `device_controls.*` + `GrpcManager` → reconstruct .proto
   (field numbers, enums, channel config, auth metadata).
2. Python probe with grpcio: GetDeviceStatus for our MAC → confirm schema live.
3. Library `aioaquareahome`: REST auth/topology + gRPC status/control.
4. HA custom integration: climate entity (power/mode/setpoint/fan),
   temperature sensors; DataUpdateCoordinator on SubscribeToDeviceEvents or poll.
5. Publish under muscaglar/, report findings to panasonic_cc#310.

## Etiquette

Undocumented third-party cloud: poll gently (≥60s), reuse tokens, no writes
until schema is certain. One-connection-per-account caution from Panasonic
docs appears NOT to apply (REST is stateless JWT), but verify app coexistence.

## ✅ FIRST CONTACT — 2026-07-05

`GetDeviceStatus` succeeded via grpcio: empty request + metadata
(`authorization: Bearer <login JWT>`, `mac_address: <colon MAC>`) to
`grpc.aquarea-home.solutiontech.tech:443`, `/device_controls.Controls/GetDeviceStatus`.

Decoded live (probe_status.py):
- Temperatures are **deci-degrees int32**: setpoint 165 = 16.5°C, min 160, max 310, step 5; room_temperature 251 = 25.1°C
- SetpointStatus {value,min,max,step,offset} — ready-made HA climate min/max/step
- operation_mode=2, fan_speed=3, flap=1 observed; power_state omitted (proto3 zero = off)
- RSSI is negative varint (two's complement): 18446744073709551548 = −68 dBm

## Remaining for full integration
1. Enum semantics: operation_mode / fan_speed / power_state values ← decompile
   Duepuntozero UI (where DeviceOperation(type, value) is constructed)
2. SetDeviceValue opcode table (same source)
3. SubscribeToDeviceEvents Event schema (type=1, value=2?)
4. Then: aioaquareahome lib + HA custom component (climate + sensors)

## ✅ SHIPPED — 2026-07-05

- Opcodes (DuepuntozeroValueType): 1=PowerState 2=Setpoint 3=OperationMode 4=FanSpeed 5=Flap (6=RawModbus 7=Reboot 8=Calendar 9=ManualMode)
- OperationMode: 0=Auto 1=Heat 2=Cool 3=Fan 4=Dry · FanSpeed: 0=Auto 1=Min 2=Medium 3=Max
- Event stream types: 249=ManualMode 250=Flap 251=FanSpeed 252=OperationMode 253=RoomTemperature 254=Setpoint 255=PowerState
- SetDeviceValue verified with safe no-op write (setpoint→same value)
- HA integration `aquarea_home` live: config flow, climate.bedroom_ac (modes/fan/setpoint,
  min/max/step from device), room temp + RSSI sensors, 60s polling via grpclib (pure python)
- TODO for the public release: SubscribeToDeviceEvents push updates, reauth flow,
  multi-device testing, HACS metadata, publish to muscaglar/ha-aquarea-home
