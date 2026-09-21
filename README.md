# Aquarea Home for Home Assistant

Home Assistant integration for air-conditioning units managed by
Panasonic's **Aquarea Home** app — written for the **Panasonic RAC Solo**,
which Comfort Cloud does not support.

> Aquarea Home is a white-label of the Innova app, both built and hosted by
> SolutionTech, and every brand lives on its own tenant. This integration
> talks to the **Aquarea Home** tenant only
> (`v2.api.aquarea-home.solutiontech.tech`). Accounts created in the Innova
> app or another white-label app are not supported — see
> [Related projects](#related-projects).
>
> The protocol (REST + gRPC) is undocumented. The v1 API was
> reverse-engineered from the official Android app; the v2 wire format used
> since v0.3.0 follows the notes in
> [innova-farna-ha](https://github.com/buenaonda/innova-farna-ha), was
> validated against a real RAC Solo, and its field numbers have since been
> cross-checked against the schema recovered independently by
> [hass-innova-cloud](https://github.com/achillecalegari/hass-innova-cloud).
> See [PROTOCOL.md](PROTOCOL.md) for the write-up.

## Supported hardware

| Device | Status |
|---|---|
| Panasonic RAC Solo (Innova 2.0 hardware family), set up in the Aquarea Home app | ✅ tested |
| Several RAC Solos on one account | 🤞 should work (since v0.3.1 each unit is handled on its own) — reports welcome |
| Units set up in the **Innova** app or another white-label app | ❌ not supported: this integration only signs in to the Aquarea Home tenant, and we have no Innova account to test with |
| Aquarea Air fan coils, Loop, Vent, M6/M7, Waterloop | ❌ not supported (each family has its own state message) |

**Requirements:** Home Assistant 2024.11 or newer, and the unit set up in
the **Aquarea Home** app with an **email + password** account. If you signed
in with Apple/Google SSO, set a password first (the app's password-reset
flow works for this).

## What you get

> **v0.3.0 (2026-09-01): moved to the v2 cloud API.** SolutionTech's original
> (v1) login endpoint stopped working on 2026-08-31 — every login, valid or
> not, returns `500 {"code": 402}` — while the v2 API that the current
> Aquarea Home / Innova apps use kept working with the same credentials.
> Existing installs keep their entities and device; a restart after the
> update is all that is needed.
>
> **v0.3.1: failures are handled per unit.** A unit the cloud cannot reach
> keeps its last-known state until it has missed three polls in a row
> (60–90 s), then only that unit's entities go unavailable; the other units
> carry on, and setup succeeds even if some units are offline. Errors name
> the cloud's code (`RESPONSE_TIMEOUT`, `CACHE_NOT_READY`, node `OFFLINE`)
> instead of a raw `{1: [1]}`, a cloud that keeps rejecting freshly issued
> tokens is asked for a new login at most once every 10 minutes, and
> removing the integration deletes its stored token and state cache. The
> minimum Home Assistant version is now 2024.11.

- `climate` entity — power, HVAC modes (auto/heat/cool/dry/fan only), fan
  speed, swing on/off (the flap either sweeps or stays put; the protocol has
  no positions), target temperature with the unit's own min/max/step. The
  mode and fan lists come from the unit's own capability lists
- Room temperature sensor
- WiFi signal diagnostic sensor
- State polled every 30 s per unit, plus a re-poll about 2 s after a command
- The login token is persisted (v2 tokens are valid for a year), so a Home
  Assistant restart never depends on the login endpoint being up
- Re-authentication flow if your password changes

### Fan speed names

Home Assistant's names differ from the cloud's for the top two levels:

| HA `fan_mode` | App / cloud name | Wire value |
|---|---|---|
| `auto` | Auto | 1 |
| `low` | Min | 2 |
| `medium` | Med | 3 |
| `high` | Max | 4 |
| `max` | Boost | 5 |

So HA `high` is the app's Max and HA `max` is its Boost. The HA names stay
as they are, so existing automations keep working.

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/muscaglar/ha-aquarea-home` (category: Integration)
2. Install **Aquarea Home**, restart Home Assistant

### Manual

Copy `custom_components/aquarea_home/` into your `config/custom_components/`
directory and restart.

### Configure

**Settings → Devices & Services → Add Integration → "Aquarea Home"** —
sign in with your Aquarea Home app email and password. Devices are
discovered from your account automatically.

## Limitations & troubleshooting

- **Cloud only, and an undocumented cloud at that.** No local API exists on
  these units (a full port scan confirms the WiFi module is outbound-only),
  so everything goes through `v2.api.aquarea-home.solutiontech.tech` /
  `v2.grpc.aquarea-home.solutiontech.tech`, operated by SolutionTech, the
  developer of the official app. When it is down so is the integration, and
  it may break without notice if the backend changes.
- **Changes made in the app take up to ~30 s to show in HA.** There is no
  push stream in v0.3.x; state is polled. Commands sent from HA show at once
  and are confirmed by the re-poll.
- **One unit unavailable, with a WARNING naming it.** The cloud answered but
  could not deliver that unit for three polls in a row. The message carries
  the cloud's code: `RESPONSE_TIMEOUT` (the cloud timed out talking to the
  unit, usually a WiFi drop on the AC side), `CACHE_NOT_READY`, or node
  `OFFLINE`. The other units are not affected, an INFO line marks the unit's
  return, and it clears by itself.
- **Every unit unavailable, with one ERROR** (`Error fetching aquarea_home
  data: …`). Nothing on the account answered for three polls, and the message
  says why. `gRPC transport error`, `[Errno -3] Try again` (a temporary DNS
  failure on the Home Assistant host) or `no reply within 20 s` point at the
  path to the cloud: internet or a backend brown-out. A message that starts
  with a unit's name, such as `Bedroom AC: cloud could not reach the unit
  (RESPONSE_TIMEOUT)`, points at the units themselves. With a single unit on
  the account this ERROR is how that unit's outage is reported: there is no
  per-unit WARNING when no unit is left. Both clear by themselves.
- **Re-authentication prompt.** The cloud rejected the stored password. A
  rejected token is handled quietly with a fresh login; if the cloud keeps
  rejecting fresh tokens, one WARNING says so and the login is retried at
  most every 10 minutes.
- Units added in the app appear after reloading the integration. Only the AC
  state is decoded, so other device families on the same account stay
  unavailable here.
- **Reporting a problem:** include the HA version, the integration version,
  how many units you have, and a debug log:

  ```yaml
  logger:
    logs:
      custom_components.aquarea_home: debug
  ```

## Notes & etiquette

- The integration polls each unit every 30 s over one reused gRPC connection
  and keeps its login token for the year it is valid. The interval is not
  configurable on purpose — we're guests on this API.
- The v2 API sends temperatures as plain °C floats (v1 used deci-degrees);
  the protocol details are documented in [PROTOCOL.md](PROTOCOL.md).

## Related projects

- [hass-innova-cloud](https://github.com/achillecalegari/hass-innova-cloud)
  (MIT) — a separate integration for the same SolutionTech backend, with a
  brand selector. Use it if your account was created in the Innova app or
  another white-label app (Rhoss Tema, Etherma Fire+Ice 2, DiffusApp), or if
  you have fan coils, thermostats or another brand's hardware. Its recovered
  `.proto` is the schema our field numbers were checked against.
- [innova-farna-ha](https://github.com/buenaonda/innova-farna-ha) — the
  protocol notes the v2 port started from.

## Disclaimer

Unofficial, community-built software. Not affiliated with, endorsed by, or
supported by Panasonic, Innova, or SolutionTech. Use at your own risk.

## License

[MIT](LICENSE)
