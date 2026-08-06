# Nocturne

**Agent-orchestrated deep-sky astrophotography controller for Raspberry Pi 5.**

Nocturne runs an unattended deep-sky imaging session on a portable amateur rig. An LLM
agent makes the judgement calls — what to image, in what order, when to give up, how to
react to changing conditions — while deterministic software makes every control decision:
guiding, focusing, slewing, and safety limits.

The agent never touches hardware and never sees pixels. It reads scalar telemetry and
calls a constrained tool API, every call validated by a non-bypassable safety layer.

> ⚠️ **Status: pre-alpha.** Milestone M1 (instrument control) passes its automated
> criteria against the INDI simulators; the hardware test on the real rig is still
> outstanding — see [`docs/hardware-setup.md`](docs/hardware-setup.md). Nothing
> beyond M1 exists yet: no plate solving, no guiding, no telemetry, no web app, no
> agent. This software moves a telescope. Do not run it unattended until you have
> completed the meridian calibration procedure for your own mount and tripod.

---

## What it does

- Plans a night's session from a target list, altitude windows, moon separation and
  existing integration history
- Drives polar alignment, plate solving, autofocus, guiding, dithering and meridian
  handling through KStars/Ekos
- Reduces every sub to ~40 scalar metrics (HFD, FWHM, eccentricity, guiding RMS,
  transparency, background rate) and rejects bad frames deterministically
- Computes optimal sub exposure from the measured sky background and the sensor's read
  noise — arithmetic, not guesswork
- Reacts in-session to cloud, dew, focus drift and degrading seeing
- Produces multiple stacks under different frame-rejection criteria with comparative
  metrics, so you choose the winner
- Serves a mobile-first PWA (night mode, EN/FR/ES) for planning, live monitoring,
  reviewing agent decisions and browsing the archive

## What it deliberately does not do

- **No post-processing.** Stacking happens on the Pi; stretching, gradient removal and
  colour work belong on your desktop.
- **No planetary or lunar capture.** Deep-sky only. No SER video, no lucky imaging.
- **No reimplementation of instrument control.** Ekos does that, and does it well.
- **No weather protection.** There is no observatory in this design. Rain is the
  operator's responsibility.

---

## Architecture

```
Clients (PWA · Telegram · Samba)
        ↓ REST + WebSocket
Orchestrator (FastAPI)  ←→  Agent (Anthropic API)
        ↓                        ↓
   Safety Governor  ← every command passes through here
        ↓ DBus
   KStars/Ekos headless + StellarSolver
        ↓ INDI
   Mount · Camera · Focuser · Filter wheel · Guide camera
```

Full detail in [`SPEC.md`](SPEC.md).

## Reference hardware

Developed against: Sky-Watcher 200PDS · ZWO ASI533MM Pro · ZWO EFW 8×1.25" · ZWO EAF ·
ZWO 30F4 + ASI120MM Mini · Sky-Watcher Wave 150i · Raspberry Pi 5 8GB.

Nothing is hardcoded to this rig — all equipment parameters live in `config/equipment.yaml`.
Any INDI-supported combination should work, but only the above is tested.

## Requirements

- Raspberry Pi 5 (8 GB recommended) with **NVMe storage** — an SD card will not survive
  the stacking write load
- Raspberry Pi OS 64-bit, **Trixie (Debian 13) or later** — KStars 3.8.x is
  Qt6/KF6, and Bookworm ships KF5 only. The installer refuses to start on an
  older release rather than fail an hour into a compile
  ([ADR 0008](docs/decisions/0008-raspberry-pi-os-trixie.md))
- INDI ≥ 2.2.3, KStars/Ekos, Siril
- An Anthropic API key (optional — the system runs autonomously on deterministic rules
  without it)

## Installation

```bash
git clone https://github.com/alexcatesp/nocturne.git
cd nocturne
./scripts/install.sh
```

Native install on Raspberry Pi OS 64-bit, no Docker. It builds INDI, the ZWO
drivers, StellarSolver and KStars from source and downloads astrometry.net
indices sized for the 39-arcminute field — one to three hours on a Pi 5, and safe
to interrupt and re-run.

Then edit `config/equipment.yaml` and `config/safety.yaml`, and check them:

```bash
.venv/bin/nocturne check-config    # fails loudly, naming the offending field
./scripts/install.sh --check       # verifies the installation itself
```

The whole test suite runs against the INDI simulator drivers, with no hardware:

```bash
.venv/bin/pytest
```

## ⚠️ Safety

This software commands a motorised mount carrying several kilograms of glass.

**Before enabling any unattended mode you must complete the meridian calibration
procedure** in [`docs/meridian-calibration.md`](docs/meridian-calibration.md). Without a
tripod extension, many Newtonian setups will strike the tripod legs near the meridian.
Nocturne ships with `meridian.calibrated: false` and will refuse autonomous operation
until you have measured your own limits.

The safety governor enforces altitude floors, sun avoidance, meridian limits, cooling
ramp rates and abort conditions. It cannot be overridden by the agent, by the API, or by
configuration at runtime. If you find a path that bypasses it, that is a critical bug —
please open an issue.

## Licence

GPL-3.0. Nocturne links GPL components (INDI, KStars, Siril).

## Acknowledgements

Built on the work of the [INDI](https://indilib.org),
[KStars/Ekos](https://kstars.kde.org) and [Siril](https://siril.org) projects.
