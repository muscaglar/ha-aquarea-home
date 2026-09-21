# Aquarea Home (SolutionTech/Innova) — Protocol Notes

Reverse-engineering log for integrating the Panasonic RAC Solo ("Bedroom AC")
into Home Assistant. Started 2026-07-05.

> **Read this first.** The integration (v0.3.0+) uses the **v2 API**, which is
> the first half of this document. Everything under
> [Historical: the v1 API](#historical-the-v1-api-dead-since-2026-08-31) is the
> original notebook, kept for history: v1 stopped accepting logins on
> 2026-08-31. The two differ in hosts, service names, enum numbering (v1
> 0-based with Fan=3/Dry=4; v2 1-based with Dry=4/Fan=5) and temperature
> encoding (v1 deci-degree ints, v2 floats in °C). MAC, serial, IP and SSID
> values in this document are placeholders.

## Identity

- "Aquarea Home" (Panasonic) is a **white-label of Innova's app**, built by
  SolutionTech (`tech.solutiontech.aquarea_home`, internals `tech.solutiontech.innova`).
- RAC Solo ≈ rebadged **Innova 2.0** ("duepuntozero" in the v1 protocol) — the
  no-outdoor-unit monobloc. Other device families in the app: fancoil, waterloop, m6, m7.
- OEM tenancy exists (`oem_innova` / `oem_panasonic`) but the v1 login worked
  without an OEM discriminator. In v2 each brand has its own host pair and
  tokens are tenant-scoped (see Endpoints).
- The unit itself: WiFi, **zero open TCP ports** (full 65k scan) — cloud + BLE
  provisioning only. Local control impossible (short of BLE RE).
- Device on LAN: 192.0.2.10, MAC `AA:BB:CC:11:22:33`, serial `%IN00000000`,
  firmware 50, "Device type 2.0".

## ✅ v2 API — current since 2026-09-01 (v0.3.0)

On 2026-08-31 ~21:20 UTC the v1 login (`POST api.aquarea-home.solutiontech.tech/api/users/login`)
started answering `500 {"code":402,"message":"Something went wrong, please try again later"}`
for every request — correct password, wrong password and non-existent accounts alike — while
`GET /api/users/me` without a token still answered `401 {"code":314}`. The sibling Innova v1
host kept working, the Aquarea Home Android app had shipped 3.1.0 four days earlier, and the
v2 backend accepted the same credentials, so v1 login was treated as dead and the integration
was ported. The v1 gRPC service rejects v2 tokens (`UNAUTHENTICATED: InvalidAudience`), so
there is no half-way migration.

Credits: the v2 wire format was taken from buenaonda/innova-farna-ha (`docs/PROTOCOL.md`,
reverse-engineered from `tech.solutiontech.innova` 3.0.0) and validated here against the RAC
Solo. Message and enum names below follow the `.proto` that achillecalegari/hass-innova-cloud
(MIT) recovered statically from the Innova iOS app (`proto/innova_app.proto`). That schema is
also an independent cross-check: every field number, wire type and enum used here matches it.
See it for the fields this integration does not read (alarms, humidity, ERV, silent mode,
operation modes, the other device families).

### Endpoints
- REST base `https://v2.api.aquarea-home.solutiontech.tech/app/`
  - `POST users/login {"email","password"}` → `{"token","user"}`; JWT (PS256), `aud: user-api`,
    **exp = 365 days**. Bad password → `401 {"code":1304,"message":"Invalid credentials"}`.
  - `GET homes` (Bearer) → `[home{id, name, rooms[{id, name}], devices[{macAddress
    (colon-separated), nodeId, name, uid{vendorId, productId, hwRevision}, serialNumber,
    roomId}]}]`. In v2 the devices are a **flat list on the home** and point at their room
    through `roomId` (v1 nested them under rooms); the integration reads `home.devices` and
    uses `home.rooms` only to turn `roomId` into a name. Missing/invalid token →
    `401 {"code":1302}`.
- gRPC `v2.grpc.aquarea-home.solutiontech.tech:443` (TLS/h2), service `services.app.AppService`,
  metadata `authorization: Bearer <token>` only (no `mac_address` metadata; the MAC rides in the
  request). `v2.grpc.innova.solutiontech.tech` refuses Aquarea Home tokens.

### SendDevice (unary) — read state
`SendDeviceRequest{ bytes mac_address = 1 (6 raw bytes); uint32 node_id = 2 (omitted when 0);
CloudMessage.Request request = 3 }` with `Request{ shared = 2 { get_state = 1 {} } }`
→ request bytes for MAC `AA:BB:CC:11:22:33`: `0a06aabbcc1122331a0412020a00`.
(The code comments call these `DeviceRequest` / `Command` / `AcSetState`.)

Response (observed 2026-09-01, unit off, cool, fan high, flap on, room 22.9 °C):
```
2.1.1           state            (Response.device → shared → state)
  .1            gateway: 2 = firmware_version_code (50), 3 = serial_number "%IN00000000",
                4.2.1 = wifi network { 1 = ssid "MyWiFi", 2 = rssi, sign-extended varint (-68) }
  .2            nodes: one map entry per node { 1 = node_id (omitted when 0), 2 = Node }
  .2.2.1        Node.ac, the AC block:
                  2 = power (bool varint, absent = off)
                  3 = temperature_setpoint { 1 value, 2 min, 3 max, 4 step }  floats, °C (19.0/16.0/31.0/0.5)
                  4 = hvac_mode { 1 value, 3 = packed capabilities [1,2,3,4,5] }
                  5 = fan_speed { 1 value, 2 = packed capabilities [2,3,4,5,1] }
                  6 = flap_swing (1 = swinging)
                  7 = air_temperature (float)
```
Enums (confirmed live, same as Innova FÄRNA): hvac_mode 1=auto 2=heat 3=cool 4=dry 5=fan_only;
fan_speed 1=auto 2=low 3=medium 4=high 5=max (the RAC Solo now exposes five fan levels; v1 had
four). Those fan names are Home Assistant's; the schema calls them `AUTO / MIN / MID / MAX /
BOOST`, so HA `high` is the cloud's MAX and HA `max` is its BOOST. The capability lists feed
the climate entity's mode and fan lists.

### Errors and node shapes
A `SendDevice` reply is `DeviceMessage.Response{ oneof error = 1 | device = 2 | service = 3 }`.

- **Error wrapper** (field 1 and no field 2): `Error{ Code code = 1; optional string message = 2 }`
  with `1 = RESPONSE_TIMEOUT` (the cloud timed out talking to the unit) and
  `2 = CACHE_NOT_READY`. Only code 1 has been seen on the RAC Solo. Up to v0.3.0 the log
  showed the raw field map, so `error {1: [1]}` is code 1; v0.3.1 prints the name.
- **Nodes**: `state.nodes` (field 2) is a `map<uint32 node_id, Node>`, on the wire one
  `{1: key, 2: Node}` entry per node — which is why the AC block sits at `.2.2.1`. `Node` is a
  oneof: `ac = 1, fancoil = 2, thermostat = 3, heatpump = 4, butler = 5, error = 6`, where
  `error` is the enum `NodeError{ OFFLINE = 1; CACHE_NOT_READY = 2; INTERNAL = 3 }`. The
  integration takes the entry for the device's `nodeId` (a RAC Solo has just the one) and
  decodes `ac` only. A node error or a non-AC node counts as "this unit is not available",
  never as a good poll. Node errors have not been seen on the RAC Solo.
- **Tokens**: the login JWT lives for about a year and is the only credential on both REST and
  gRPC. REST answers a missing or invalid token with `401 {"code":1302}`. On gRPC a bad token
  should come back as `UNAUTHENTICATED` — that is from the hass-innova-cloud notes (missing
  token → `UNAUTHENTICATED "Authentication token is missing or invalid"`) and has not been
  observed on this tenant yet; the only local observation is the v1 service refusing v2
  tokens that way. The integration answers it with one fresh login (since v0.3.1 at most one
  every 10 minutes if the new token is refused too); a rejected password starts Home
  Assistant's re-authentication flow. `PERMISSION_DENIED` / `NOT_FOUND` for one unit among
  others is that unit's problem (removed or unshared), not the token's. When every unit is
  refused at once the token is given the benefit of the doubt: one fresh login, at most once
  a day, and if nothing changes the refusal is reported as it is.
- **gRPC statuses**: `DEADLINE_EXCEEDED` and a client-side timeout count against the one unit
  that was asked, and so does any other status the server answers with (`INTERNAL`,
  `FAILED_PRECONDITION`, …) and a reply the codec cannot read. `UNAVAILABLE`,
  `RESOURCE_EXHAUSTED`, `UNIMPLEMENTED` and transport errors are the path to the cloud: the
  pass ends there and every unit not yet asked counts a miss. None of these statuses has been
  seen from this backend yet; the split is a precaution.

### SendDevice — control
`Request{ ac = 3 { set_state = 1 SetState } }`,
`ac.Request.SetState{ bool power = 1; float temperature_setpoint = 2; HvacMode.Type hvac_mode = 3;
FanSpeed.Type fan_speed = 4; bool flap_swing = 5 }`, all `optional` (the schema also lists
`erv = 6` and `silent_mode = 7`, unused here). **Partial updates work**: a setpoint-only write
while the unit was off changed the setpoint and left power off (verified 2026-09-01, 19.0 →
19.5 → 19.0).

### Polling
v0.3.x polls `get_state` every 30 s per unit, one unit after the other over one channel, and
re-polls 2 s after each command. Since v0.3.1 failures are counted per unit: a unit keeps its
last-known state until its third missed poll in a row (60–90 s), then only that unit goes
unavailable. Units that answered on the previous pass are asked first. If two requests time
out in a pass in which nothing has answered, although something answered on the pass before,
the cloud itself is taken to be down and the rest of the pass is skipped rather than waited
out at 20 s per unit.

### Not used yet
`SubscribeEvents(SubscribeEventsRequest{home_id = raw 16-byte UUID}) → stream Event` exists
(delta events only, no replay on subscribe). The hass-innova-cloud schema has the event
messages (`ac.Event`, numbered differently from `ac.State`); they have not been tried here,
so there is no push stream in v0.3.x.


## Historical: the v1 API (dead since 2026-08-31)

> Everything below describes the **v1** backend (`api.` / `grpc.aquarea-home.solutiontech.tech`,
> service `device_controls.Controls`), which v0.1.0–v0.2.6 used. It is a dated notebook, left
> as written apart from the notes marked *v2:*. Do not mix its enums, metadata or temperature
> encoding with the v2 section above.

### REST API

Base: `https://api.aquarea-home.solutiontech.tech/api/`
Backend: Rust (serde deserialization errors). Clean JSON. JWT bearer auth.

#### Auth
- `POST users/login` `{"email","password"}` → `{"user":{...},"token":"<JWT>"}`
  (strict schema: extra fields ignored, missing field → 422 serde error)
- Google SSO variant: `users/login-google` (+ `/nonce`). iOS presumably adds Apple.
- Errors: `{"code":301,"message":"Invalid username or password"}`,
  `{"code":314,"message":"Authentication token not found"}`

#### Endpoints (from APK string harvest)
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

#### Key observed responses
- `GET homes` → homes[] with members (role: owner), rooms[] each with
  devices[] `{macAddress, name, fwVersionCode, serialNumber, deviceId}`.
  (*v2:* devices are a flat list on the home.)
- `GET devices/{mac}` → metadata only (name, serial, roomId, homeId,
  isUpdateAvailable). **No live status in REST.**

### gRPC (live status + control)

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

### Plan

1. jadx-decompile `device_controls.*` + `GrpcManager` → reconstruct .proto
   (field numbers, enums, channel config, auth metadata).
2. Python probe with grpcio: GetDeviceStatus for our MAC → confirm schema live.
3. Library `aioaquareahome`: REST auth/topology + gRPC status/control.
4. HA custom integration: climate entity (power/mode/setpoint/fan),
   temperature sensors; DataUpdateCoordinator on SubscribeToDeviceEvents or poll.
5. Publish under muscaglar/, report findings to panasonic_cc#310.

### Etiquette

Undocumented third-party cloud: poll gently (≥60s), reuse tokens, no writes
until schema is certain. One-connection-per-account caution from Panasonic
docs appears NOT to apply (REST is stateless JWT), but verify app coexistence.
(*v2:* v0.3.x polls each unit every 30 s; v0.1.0–v0.2.6 polled at 60 s.)

### ✅ FIRST CONTACT — 2026-07-05

`GetDeviceStatus` succeeded via grpcio: empty request + metadata
(`authorization: Bearer <login JWT>`, `mac_address: <colon MAC>`) to
`grpc.aquarea-home.solutiontech.tech:443`, `/device_controls.Controls/GetDeviceStatus`.
(*v2:* no `mac_address` metadata; the MAC rides in the request.)

Decoded live (probe_status.py):
- Temperatures are **deci-degrees int32**: setpoint 165 = 16.5°C, min 160, max 310, step 5; room_temperature 251 = 25.1°C
  (*v2:* floats in °C, no scaling)
- SetpointStatus {value,min,max,step,offset} — ready-made HA climate min/max/step
- operation_mode=2, fan_speed=3, flap=1 observed; power_state omitted (proto3 zero = off)
- RSSI is negative varint (two's complement): 18446744073709551548 = −68 dBm

### Remaining for full integration
1. Enum semantics: operation_mode / fan_speed / power_state values ← decompile
   Duepuntozero UI (where DeviceOperation(type, value) is constructed)
2. SetDeviceValue opcode table (same source)
3. SubscribeToDeviceEvents Event schema (type=1, value=2?)
4. Then: aioaquareahome lib + HA custom component (climate + sensors)

### ✅ SHIPPED — 2026-07-05

- Opcodes (DuepuntozeroValueType): 1=PowerState 2=Setpoint 3=OperationMode 4=FanSpeed 5=Flap (6=RawModbus 7=Reboot 8=Calendar 9=ManualMode)
- OperationMode: 0=Auto 1=Heat 2=Cool 3=Fan 4=Dry · FanSpeed: 0=Auto 1=Min 2=Medium 3=Max
  (**v1 numbering.** *v2:* 1=auto 2=heat 3=cool 4=dry 5=fan_only, fan 1..5 — Dry and Fan swap places)
- Event stream types: 249=ManualMode 250=Flap 251=FanSpeed 252=OperationMode 253=RoomTemperature 254=Setpoint 255=PowerState
- SetDeviceValue verified with safe no-op write (setpoint→same value)
- HA integration `aquarea_home` live: config flow, climate.bedroom_ac (modes/fan/setpoint,
  min/max/step from device), room temp + RSSI sensors, 60s polling via grpclib (pure python)
- TODO for the public release: SubscribeToDeviceEvents push updates, reauth flow,
  multi-device testing, HACS metadata, publish to muscaglar/ha-aquarea-home

### Flap semantics (probed live 2026-07-07)

- Flap (opcode 5 / event 250) is **binary**: `1` = swinging (louvre rotation), `0` = fixed/stopped.
- Writes of 2–8 are accepted by the RPC but the backend clamps state back to `1` — there is NO
  positional louvre control in this protocol (matches the app, which only offers a swing toggle).
- Exposed in HA as climate swing_mode on/off since v0.2.5 (the first published release with it).
  (*v2:* still binary, `flap_swing` bool.)

### Backend change 2026-07-09 (~10:28 BST) — GetDeviceStatus crippled, stream authoritative

- `GetDeviceStatus` now returns ONLY the iot section (fw/wifi/rssi) — the whole
  `main_status.duepuntozero_status` block is absent, regardless of request body
  (empty and all field-flag variants tested), regardless of an open subscription.
- REST login/topology, `SetDeviceValue` (ACKs), and `SubscribeToDeviceEvents` all
  fully functional; live events verified end-to-end (setpoint change in the app
  arrived as event 254 within a second).
- `/api/homes` carries topology only — no state summary. The official app runs
  stream-first with local caching; it never needed the poll's climate block.
- Server reflection: UNIMPLEMENTED.
- App "Boost" fan setting == wire fan_speed 3 (identical to Max/high; verified by
  event capture — Boost→Max→Boost emits a single 251=3 with no further events).
  (*v2:* Boost is its own level, fan_speed 5.)
- Integration v0.2.5: partial polls merge over last-known state (transport counts
  as healthy), climate availability gates on 'power' knowledge, RestoreEntity
  seeds state across restarts, events keep everything live.

### Recovery learnings (2026-07-09 afternoon)

- Cold-start blindness: HA's RestoreEntity only snapshots the state at shutdown —
  if the entity was already unavailable then, restarts stay blind until an event.
  v0.2.6 adds a coordinator-level persistent last-good cache (helpers.storage)
  saved on every update/event, loaded before first refresh.
- Write-echo bootstrap: our own SetDeviceValue commands come back as stream
  events — re-asserting known state through the integration (or the app)
  populates a blind session field-by-field. Useful manual recovery technique.
- Event delivery to a reconnecting subscriber appeared delayed during heavy
  multi-client testing; a config-entry reload (fresh subscription) resolved it.
  Multi-subscriber semantics post-backend-change remain unverified.
