# Aquarea Home for Home Assistant

Home Assistant integration for air-conditioning units managed by the
**Aquarea Home** app (Panasonic) / **Innova** app — including the
**Panasonic RAC Solo**, which is not supported by Comfort Cloud or any
existing Home Assistant integration.

> As far as we know this is the first working integration for the Aquarea
> Home cloud. The protocol (REST + gRPC) was reverse-engineered from the
> official Android app — see [PROTOCOL.md](PROTOCOL.md) for the full
> write-up.

## Supported hardware

| Device | Status |
|---|---|
| Panasonic RAC Solo ("duepuntozero" / Innova 2.0 family) | ✅ tested |
| Innova 2.0 | 🤞 should work (same device family) — reports welcome |
| Aquarea Air fan coils, Loop, Vent, M6/M7, Waterloop | ❌ not yet (protocol differs per family; PRs welcome) |

**Requirement:** the unit must be set up in the Aquarea Home (or Innova)
app with an **email + password** account. If you signed in with
Apple/Google SSO, set a password first (the app's password-reset flow
works for this).

## What you get

- `climate` entity — power, HVAC modes (auto/heat/cool/fan/dry), fan speed
  (auto/low/medium/high), target temperature with the device's real
  min/max/step limits
- Room temperature sensor
- WiFi signal diagnostic sensor
- Cloud polling every 60 seconds (the unit has no local API — a full port
  scan confirms the WiFi module is outbound-only)

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

## Notes & etiquette

- This talks to an **undocumented third-party cloud**
  (`api.aquarea-home.solutiontech.tech`, operated by SolutionTech, the
  developer of the official app). It may break without notice if the
  backend changes.
- The integration polls gently (60 s) and reuses tokens. Don't lower the
  interval out of courtesy to an API we're guests of.
- Temperatures are transmitted in deci-degrees; the protocol details are
  documented in [PROTOCOL.md](PROTOCOL.md).

## Disclaimer

Unofficial, community-built software. Not affiliated with, endorsed by, or
supported by Panasonic, Innova, or SolutionTech. Use at your own risk.

## License

[MIT](LICENSE)
