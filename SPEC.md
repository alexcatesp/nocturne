# Nocturne — Technical Specification

**Agent-orchestrated deep-sky astrophotography controller for Raspberry Pi 5.**

Version 1.0 · Specification document for implementation by Claude Code.

---

## 0. How to use this document

This is the authoritative specification. Implementation proceeds **spec-driven**: every
milestone in §14 has explicit acceptance criteria, split into:

- **AUTO** — verifiable by the agent against INDI simulator drivers, in CI, without hardware.
- **HITL** — hardware-in-the-loop, verifiable only by the human operator on the real rig.

Do not proceed to milestone N+1 until all AUTO criteria of milestone N pass. HITL criteria
gate deployment, not development.

When this document is ambiguous or silent, **stop and ask** rather than inventing behaviour.
Record the resolution in `docs/decisions/` as an ADR (Architecture Decision Record).

**This document has an addendum, and it is authoritative.**
[`docs/SPEC-ADDENDUM-A.md`](docs/SPEC-ADDENDUM-A.md) — *scientific characterisation and
data provenance* — specifies what turns pleasing images into publishable measurements. It
merges into this document as §16–§18 with a revised §14 **before M3**, and until then it
lives beside it (ADR 0016). Read it before designing anything that stores a measurement:
§A.0 warns that provenance cannot be retrofitted onto data already collected, and §A.7.1
lists five defaults specified here that it contradicts on purpose. Where the two disagree,
the addendum wins on scientific requirements and this document wins on everything else.

---

## 1. Goal and scope

### 1.1 What this is

A self-hosted system that runs an unattended deep-sky imaging session on a portable
amateur rig, where an LLM agent makes the *judgement* decisions (what to image, in what
order, when to give up, how to react to changing conditions) and deterministic software
makes every *control* decision (guiding, focusing, slewing, safety limits).

### 1.2 What this is not

- **Not a post-processing suite.** Stacking happens on the Pi; gradient removal,
  colour calibration, stretching and cosmetics happen off-device on the operator's PC.
- **Not a planetary/lunar capture system.** No SER video, no lucky imaging. Deep-sky only.
- **Not a replacement for instrument-control software.** KStars/Ekos does that. Nocturne
  orchestrates Ekos; it does not reimplement it.
- **Not automatic collimation.** The collimation assistant (§10.4) measures and displays;
  the operator turns the screws. Neither mirror cell is motorised and neither will be.
- **Not weather-safe.** There is no observatory and no roof. Rain avoidance is the
  operator's responsibility (see §9.4).

### 1.3 Design principles

1. **The agent never touches hardware directly.** It calls a constrained tool API. Every
   call is validated by the safety layer before reaching Ekos.
2. **The agent never sees pixels.** It sees scalar telemetry. Images are reduced to
   numbers by deterministic code (§7).
3. **Human authority is absolute.** Any operator action immediately demotes the agent to
   advisory mode (§8.4).
4. **Fail safe, not fail open.** Loss of network, agent, or API budget degrades to
   autonomous rule-based operation or to parking — never to uncontrolled motion.
5. **Everything the agent decides is logged with its rationale.** Sessions must be
   auditable after the fact.

---

## 2. Hardware inventory

### 2.1 Optical train

| Item | Model | Key parameters |
|---|---|---|
| OTA | Sky-Watcher 200PDS | 200 mm aperture, f/5, 1000 mm nominal FL, Newtonian |
| Coma corrector | Baader MPCC Mark III | **Not yet owned.** ~1.0×, requires 55 mm backfocus |
| Main camera | ZWO ASI533MM Pro | IMX533 mono, 3008×3008, 3.76 µm, 14-bit, TEC cooled |
| Filter wheel | ZWO EFW 8×1.25" | Populated: Dark, L, R, G, B. Positions 6–8 empty |
| Focuser | ZWO EAF | Absolute positioning, temperature sensor |
| Guide scope | ZWO 30F4 | 120 mm FL, f/4 |
| Guide camera | ZWO ASI120MM Mini | 1280×960, 3.75 µm |
| Mount | Sky-Watcher Wave 150i | Strain wave, EQ mode, USB serial |

Derived plate scales (nominal — **must be superseded by plate-solve-derived values**):

- Imaging: 206.265 × 3.76 / 1000 ≈ **0.776 ″/px**, FOV ≈ 38.9′ × 38.9′
- Guiding (guide scope): 206.265 × 3.75 / 120 ≈ **6.45 ″/px**
- Guiding (future OAG at f/5): ≈ **0.77 ″/px**

**Migration note:** the operator intends to replace the guide scope with an OAG using the
same ASI120MM Mini. Guide scale, guide FOV and star-detection thresholds must be
configuration values, never constants. See `equipment.yaml` in §5.

### 2.2 Compute and infrastructure

| Item | Status | Notes |
|---|---|---|
| Raspberry Pi 5, 8 GB | Owned | Currently booting from SDXC |
| NVMe HAT + 500 GB SSD | **Required purchase** | SDXC will not survive stacking write loads |
| Ethernet to router | Planned | Direct cable. Primary link |
| 12 V DC supplies | Owned, 3× 5 A | Mount, camera TEC, EAF/EFW/accessories |
| Secondary dew heater + PWM | **Required for M6** | Blocking for unattended operation (§9.3) |
| Flat panel | Owned (light table) | Manual placement, not telescope-mounted |
| Counterweight | Owned, not mounted | |
| GPS receiver + PPS | **Fitted, verified** | 1 Hz pulse into GPIO4; disciplines the clock to ±155 ns (Addendum A §A.8.4) |
| AHT20 (T / RH) | **Fitted, verified** | I²C `0x38`. The authoritative air temperature and humidity — dew point derives from these (§9.3) |
| BMP280 (P) | **Fitted, verified** | I²C `0x77`. **Pressure only** — it self-heats and reads ~1 °C high |

Everything in the lower three rows was brought up in August 2026 and is recorded in
[`docs/FIELD-NOTES-TIMING.md`](docs/FIELD-NOTES-TIMING.md), with two decisions it forced
in ADR 0014 and ADR 0015. Neither subsystem is used by any code yet.

**GPIO and I²C allocations are reserved, not incidental.** GPIO4 carries PPS specifically
so that **GPIO18/PWM0 stays free for the secondary dew heater**; GPIO2/GPIO3 are the I²C
bus; GPIO14 (UART TX) is **deliberately left unconnected** so that nothing on the Pi can
write to the receiver (ADR 0015). Do not reassign any of them. The GPS is on
`/dev/ttyAMA0` and never `/dev/serial0`, which on the Pi 5 is the debug UART on the 3-pin
header rather than the GPIO header.

### 2.3 Observing site

Fixed site, portable setup: residential terrace, Tudela de Duero, Valladolid, Spain.
Roughly 42° N, 700 m elevation. Bortle ~5–6. Restricted horizon (buildings, walls). The
mount is set up and torn down per session; polar alignment is performed each night.

**Precise coordinates are deliberately not in this repository.** A residential site to
four decimal places is a home address to within metres, and this document sits beside an
inventory of equipment left outside overnight and a schedule of when nobody is watching
it. `equipment.yaml` ships a generic placeholder; the operator supplies the real values
in an untracked `equipment.local.yaml` (ADR 0013), and Nocturne warns at every startup
until they do. **A GPS receiver is not an exemption from this rule** — a measured position
is filed the same way as a position read off a map, and no document in this repository
reproduces it.

**How to obtain them, since a wrong value here fails silently.** A single GPS fix is not
good enough: the one taken on the reference rig had HDOP 2.51 and landed 1.18 km from the
coordinates then configured, of which the longitude component alone was 1.5 s of local
sidereal time — 22″ of RA. Use a multi-hour average taken at the position the mount
actually occupies. **Elevation is metres above the geoid, not above the ellipsoid**; INDI
and KStars expect the former, at this site the two differ by 51.7 m, and confusing them
produces no error anywhere. See [`docs/FIELD-NOTES-TIMING.md`](docs/FIELD-NOTES-TIMING.md)
§6.1, and §5 there for why an averaging tool's reported standard error is a lower bound.

**Critical constraint:** no tripod extension is fitted. The OTA will physically collide
with the tripod legs near the meridian. This is a hard safety concern, not a preference.
See §9.1.

---

## 3. Architecture

```
┌────────────────────────────────────────────────────────────┐
│  Layer 5 · CLIENTS                                         │
│  PWA (mobile/desktop) · Telegram notifier · Samba share    │
└──────────────────┬─────────────────────────────────────────┘
                   │ REST + WebSocket (token auth)
┌──────────────────▼─────────────────────────────────────────┐
│  Layer 4 · ORCHESTRATOR (FastAPI)  ← the code we write     │
│  Session state machine · planning · telemetry aggregation  │
│  Event bus · calibration library · stacking jobs           │
└──────┬───────────────────────────────────┬─────────────────┘
       │ agent tool API (§8)               │
┌──────▼──────────────────┐   ┌────────────▼─────────────────┐
│  Layer 3 · AGENT        │   │  Layer 2 · SAFETY GOVERNOR   │
│  Claude via API         │──▶│  Hard limits. Rejects any    │
│  Advisory or autonomous │   │  unsafe command. NOT bypassable │
└─────────────────────────┘   └────────────┬─────────────────┘
                                           │ DBus
                              ┌────────────▼─────────────────┐
                              │  Layer 1 · EXECUTOR          │
                              │  KStars/Ekos headless        │
                              │  + PHD2-equivalent internal  │
                              │  + StellarSolver             │
                              └────────────┬─────────────────┘
                                           │ INDI protocol
                              ┌────────────▼─────────────────┐
                              │  Layer 0 · DRIVERS           │
                              │  indi_eqmod · indi_asi_ccd   │
                              │  indi_asi_focuser · _wheel   │
                              └──────────────────────────────┘
```

**Rationale for Ekos as executor:** polar alignment, V-curve autofocus, guiding,
dithering, meridian handling and failure recovery are solved problems with years of field
testing behind them. Reimplementing them is weeks of work in exactly the code where a bug
costs an entire night. Nocturne writes planning, telemetry, agent integration and UI.

**Licensing:** INDI, KStars/Ekos and Siril are GPL and are used directly. StellarMate OS
and its web UI are proprietary and are **not** used; the equivalent layer is what Nocturne
implements. INDI Web Manager (LGPL) may be used for driver lifecycle management.

---

## 4. Technology stack

| Concern | Choice | Notes |
|---|---|---|
| OS | Raspberry Pi OS 64-bit, **Trixie (Debian 13) or later** | Native install, **no Docker** — USB passthrough friction outweighs benefits. Trixie, not Bookworm: KStars 3.8.x is Qt6/KF6 and Debian 12 ships KF5 only (ADR 0008) |
| Instrument control | INDI ≥ 2.2.3, KStars/Ekos headless | 2.2.3 added Wave 150i home indexer support in indi-eqmod |
| Plate solving | StellarSolver + local astrometry.net indices | Offline. Index range sized for 39′ FOV |
| Backend | Python 3.11+, FastAPI, uvicorn | |
| Ekos bridge | `dbus-next` (async DBus) | |
| Direct INDI access | `pyindi-client` | Only for properties Ekos does not expose |
| Image analysis | astropy, photutils, numpy | Star detection, HFD/FWHM/eccentricity, background |
| Stacking | Siril CLI (`siril-cli -s script`) | |
| Persistence | SQLite (WAL mode) + FITS on disk | |
| Frontend | React 18 + TypeScript + Vite, PWA | Served statically by FastAPI |
| i18n | `react-i18next` | en, fr, es (§12) |
| Agent | Anthropic API, tool use | Runs on the Pi as an API client |
| Notifications | Telegram Bot API | |
| Remote access | Tailscale | No port forwarding |
| File export | Samba share | Bulk transfer to operator PC |
| Tests | pytest, pytest-asyncio, hypothesis; Vitest + Playwright | |

**Language policy:** all code, identifiers, comments, commit messages, docstrings,
API field names and documentation in **English**. User-facing strings are translation
keys only — never literals in components.

---

## 5. Configuration

All configuration is YAML under `config/`, loaded and validated by Pydantic models at
startup. **No magic numbers in code.** Startup fails loudly on invalid config.

### 5.1 `equipment.yaml`

```yaml
site:
  # PLACEHOLDER. Replace before the first session — see §2.3. Nocturne warns at
  # every startup while these are still the shipped values, because nothing
  # about wrong coordinates fails loudly: ephemeris, altitude windows and
  # meridian timing are all derived from them.
  name: "PLACEHOLDER — replace with your site"
  latitude: 45.0
  longitude: 0.0
  elevation_m: 100
  timezone: "UTC"

optical_trains:
  - id: "native"
    active: true
    focal_length_mm: 1000        # nominal; overridden by plate solve
    aperture_mm: 200
    # Secondary minor axis. The collimation assistant (§10.4) predicts the
    # obstruction disc from it and refuses to run when it is null, which is
    # what an unobstructed train — a refractor — sets it to.
    central_obstruction_mm: 58
    corrector: null
  - id: "mpcc"
    active: false
    focal_length_mm: 1000
    aperture_mm: 200
    central_obstruction_mm: 58
    corrector: "Baader MPCC Mark III"
    backfocus_mm: 55

imaging_camera:
  indi_driver: "indi_asi_ccd"
  indi_device_name: "ZWO CCD ASI533MM Pro"
  name: "ZWO CCD ASI533MM Pro"
  pixel_size_um: 3.76
  width_px: 3008
  height_px: 3008
  bit_depth: 14
  mono: true
  cooled: true
  cooling:
    target_c: -10
    ramp_c_per_min: 2.0          # never exceed; TEC longevity
    settle_tolerance_c: 0.5
    settle_timeout_s: 600
  # Applied to CCD_CONTROLS on connect and after every reconnection. The 533MM
  # ships at Gain 200, Offset 1 — driver defaults, not configuration (§11).
  offset: 1
  gain_profiles:                 # measured values; see docs/sensor-characterisation.md
    - name: "hcg"
      gain: 100
      read_noise_e: 1.5
      full_well_e: 21000
      e_per_adu: 0.32

filter_wheel:
  indi_driver: "indi_asi_wheel"
  indi_device_name: "ZWO EFW"
  # Verified physically 2026-08-07 by opening the wheel — docs/FIELD-NOTES-M1.md
  # §10.1. The earlier order here was wrong in every slot. Nocturne WRITES these
  # names into the driver on connect and never reads the driver's own, which are
  # ZWO factory defaults matching nothing in the wheel (§10.2).
  slots:
    1: { name: "L",     type: "luminance",  offset_steps: 0 }
    2: { name: "R",     type: "red",        offset_steps: 0 }
    3: { name: "G",     type: "green",      offset_steps: 0 }
    4: { name: "B",     type: "blue",       offset_steps: 0 }
    5: { name: "Dark",  type: "dark",       offset_steps: 0 }
    6: { name: null,    type: "empty" }
    7: { name: null,    type: "empty" }
    8: { name: null,    type: "empty" }

focuser:
  indi_driver: "indi_asi_focuser"
  indi_device_name: "ZWO EAF"
  # The EAF ships FOCUS_BACKLASH_STEPS=180 with compensation off. 180 is a ZWO
  # default, not a measurement — and neither was the 0 previously specified here,
  # which was worse because it looked like one (docs/FIELD-NOTES-M1.md §12).
  backlash_steps: null           # measure in M2; distinguish from the default
  step_size_um: null             # measured
  # Unset means "whatever the driver reports", which is authoritative: the EAF
  # reports FOCUS_MAX.FOCUS_MAX_VALUE=100000 from the hardware. The 60000
  # previously specified here had no provenance. Set this only to impose a
  # TIGHTER limit; a looser one is refused at bring-up, never clamped.
  max_position: null
  temperature_compensation:
    enabled: false               # enable only after coefficient measured
    coefficient_steps_per_c: null
  refocus_triggers:
    delta_temperature_c: 1.5
    hfd_drift_percent: 15
    elapsed_minutes: 60
    on_filter_change: true

guiding:
  # These values change entirely when migrating to OAG. Never hardcode.
  mode: "guidescope"             # "guidescope" | "oag"
  camera:
    indi_driver: "indi_asi_ccd"
    indi_device_name: "ZWO CCD ASI120MM Mini"
    name: "ZWO CCD ASI120MM Mini"
    pixel_size_um: 3.75
    width_px: 1280
    height_px: 960
    gain: 50                     # applied on connect; the driver default is also 50
    offset: 0
  focal_length_mm: 120
  exposure_s: 2.0
  calibration_step_ms: 1500
  dither:
    enabled: true
    pixels: 3.0
    settle_tolerance_px: 1.5
    settle_timeout_s: 120
    every_n_frames: 1
  thresholds:
    rms_warn_arcsec: 1.2
    rms_abort_arcsec: 2.5
    max_consecutive_lost_frames: 5

mount:
  indi_driver: "indi_eqmod_telescope"
  indi_device_name: "EQMod Mount"   # what the DRIVER calls it, not what you do
  device_label: "Wave 150i"
  connection: "serial"           # USB serial preferred over WiFi
  # CDC-ACM, not a USB-serial bridge: the Wave 150i is an STM32 virtual COM
  # port and enumerates as ttyACM, never ttyUSB. Measured on the reference rig,
  # docs/FIELD-NOTES-M1.md §2.1. Omit the key to use the port the driver
  # reports; it populates DEVICE_PORT itself and is usually right.
  port: "/dev/serial/by-id/usb-STMicroelectronics_STM32_Virtual_ComPort_<serial>-if00"
  baud: 115200                   # the driver starts at 9600; set before CONNECT
  slew_rate_max_deg_s: 3.0       # deliberately below the mount's 7.5; portable setup
  counterweight_fitted: false

# The collimation assistant — §10.4. Attended only; §9.6 refuses it otherwise.
#
# NOT IN THE SHIPPED FILE YET, and neither is central_obstruction_mm above. The
# schema is extra="forbid", so these keys and their Pydantic models land together
# with the implementation in M4 (ADR 0017). Adding the YAML ahead of the models
# would fail validation at startup.
collimation:
  star:
    # Magnitudes, so min is the BRIGHTER bound. Anything in this band gives a
    # usable donut in well under a second at f/5.
    min_magnitude: 0.0
    max_magnitude: 3.0
    # In addition to the §9.2 altitude floor, never instead of it. A Newtonian's
    # primary flops with altitude, so a reading taken at 70° does not describe
    # the tube at 30°: prefer a star near where the first target will be.
    min_altitude_deg: 30
    prefer_near_first_target: true
  defocus:
    direction: "inward"          # "inward" | "outward"; the donut inverts between them
    # Steps are DERIVED, not configured: §10.4.1 gives the diameter formula and
    # focuser.step_size_um closes it. Until that is measured (M2), the assistant
    # ramps away from focus in `direction` until the donut reaches
    # min_diameter_px, and reports the steps it used.
    target_diameter_px: 140
    min_diameter_px: 60          # below this it reports NoMeasurement, never a number
    max_diameter_px: 400
  loop:
    roi_px: 512
    binning: 2
    exposure_target_peak_adu: 9000   # of 16383; auto-tuned within the bounds below
    exposure_min_s: 0.02
    exposure_max_s: 2.0
    min_update_hz: 2.0           # a requirement, not a preference — §10.4.3
    smoothing_frames: 10         # median over this window; seeing is not miscollimation
    lost_star_frames: 15         # consecutive misses before the loop stops and says so
    # The working area is the middle of the frame, not the frame: past this the
    # donut carries field aberrations rather than collimation and the assistant
    # refuses (§10.4.3). PROVISIONAL — validated on the rig in M4 (§15).
    max_offaxis_arcmin: 5.0
    drift_warn_minutes: 10       # below this, the UI recommends polar aligning first
  thresholds:
    error_good: 0.05             # normalised decentre — §10.4.1
    error_acceptable: 0.12
    converged_frames: 20         # sustained below error_good before "converged"
  screws:
    # The PRIMARY's three collimation bolts — §10.4.2. The secondary is a bench
    # job with a Cheshire and is a prerequisite of this assistant, not something
    # it can measure. Labels only: the mapping from an on-screen direction to one
    # of these is MEASURED (§10.4.4) and lives in the database, never in this
    # file, because nothing in nocturne writes configuration (ADR 0013).
    names: ["A", "B", "C"]
  history:
    prompt_after_sessions: 5     # sessions since the last check before the UI asks
```

### 5.2 `safety.yaml`

**This file governs the safety layer (§9). Values here are enforced, not advisory.**

```yaml
limits:
  altitude_min_deg: 25           # below this, do not slew or track
  altitude_max_deg: 88           # zenith avoidance
  sun_avoidance_deg: 30          # angular distance from Sun, always
  sun_altitude_max_deg: -12      # no imaging above nautical twilight

  # MERIDIAN / TRIPOD COLLISION — see §9.1. Calibrated, not guessed.
  meridian:
    calibrated: false            # implementation MUST refuse unattended mode while false
    calibration_date: null
    hour_angle_east_limit_deg: null   # measured by procedure in §9.1
    hour_angle_west_limit_deg: null
    safety_margin_deg: 5
    flip_strategy: "flip"        # "flip" | "stop"
    flip_settle_s: 30
    require_solve_after_flip: true

  cooling:
    max_delta_from_ambient_c: 35
    abort_if_power_draw_exceeds_percent: 95

abort_conditions:
  guiding_lost_s: 180
  plate_solve_failures_consecutive: 3
  cloud_star_count_drop_percent: 60
  cloud_confirm_frames: 2
  mount_communication_loss_s: 30
  disk_free_min_gb: 20

on_abort:
  action: "park"                 # stop tracking, park, warm TEC on ramp, close session
  notify: true
  notify_severity: "alarm"

agent:
  max_tokens_per_session: 400000
  max_calls_per_hour: 60
  on_budget_exhausted: "autonomous"   # continue on deterministic rules, no agent
  on_api_unreachable: "autonomous"
```

### 5.3 `agent.yaml`

Model selection, system prompt path, autonomy level, event subscription list, poll
interval, and the tool allow-list per autonomy level.

---

## 6. Repository layout

```
nocturne/
├── SPEC.md                        # this document
├── README.md
├── LICENSE                        # GPL-3.0 (links GPL components)
├── docs/
│   ├── decisions/                 # ADRs
│   ├── hardware-setup.md
│   ├── meridian-calibration.md    # §9.1 procedure, operator-facing
│   ├── sensor-characterisation.md
│   └── api.md                     # generated from OpenAPI
├── config/
│   ├── equipment.yaml
│   ├── safety.yaml
│   ├── agent.yaml
│   └── schemas/                   # Pydantic models
├── backend/
│   ├── nocturne/
│   │   ├── main.py
│   │   ├── api/                   # FastAPI routers
│   │   ├── ws/                    # WebSocket hub
│   │   ├── executor/              # Ekos DBus bridge + INDI direct
│   │   ├── safety/                # governor — §9
│   │   ├── session/               # state machine, scheduler
│   │   ├── telemetry/             # frame analysis, aggregation
│   │   ├── catalog/               # object catalogue, ephemeris, framing
│   │   ├── calibration/           # darks/flats library, flat exposure solver
│   │   ├── stacking/              # Siril job runner
│   │   ├── agent/                 # tool surface, prompt, budget guard
│   │   ├── notify/                # Telegram
│   │   └── store/                 # SQLite models
│   └── tests/
│       ├── unit/
│       ├── integration/           # against INDI simulators
│       └── fixtures/              # synthetic FITS generators
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── views/                 # Plan · Live · Agent · Archive
│   │   ├── locales/{en,fr,es}/
│   │   └── theme/                 # night mode — §11.5
│   └── tests/
├── scripts/
│   ├── install.sh
│   ├── siril/                     # stacking templates
│   └── systemd/
└── .github/workflows/ci.yml
```

---

## 7. Telemetry

### 7.1 Principle

Deterministic code reduces every frame to scalars. The agent consumes only scalars.
A 18 MB frame becomes ~40 numbers (~120 tokens).

### 7.2 Per-frame record

Computed immediately after each sub completes, stored in SQLite, emitted on WebSocket.

```json
{
  "frame_id": 141,
  "session_id": "2026-08-12",
  "target": "NGC 7000",
  "filter": "L",
  "exposure_s": 300,
  "gain": 100,
  "offset": 30,
  "binning": 1,
  "started_utc": "2026-08-12T23:14:02Z",

  "sensor_temp_c": -10.1,
  "ambient_temp_c": 18.4,
  "focuser_position": 24310,
  "focuser_temp_c": 17.9,

  "hfd": 3.42,
  "fwhm_arcsec": 2.58,
  "eccentricity": 0.31,
  "star_count": 847,
  "star_count_delta_percent": -3.2,

  "background_adu": 1240,
  "background_rate_adu_s": 4.1,
  "noise_adu": 11.2,
  "p50": 1251, "p95": 2410, "p99": 3980, "p999": 18400,
  "saturated_fraction": 0.0004,
  "clipped_black_fraction": 0.0,

  "guide_rms_arcsec": [0.48, 0.61],
  "guide_rms_total_arcsec": 0.78,
  "guide_peak_arcsec": 1.9,
  "guide_dropped_frames": 0,

  "solve_ok": true,
  "solve_error_arcsec": 3.2,
  "position_angle_deg": 89.7,
  "altitude_deg": 62.4,
  "azimuth_deg": 118.0,
  "hour_angle_deg": -31.2,
  "airmass": 1.13,
  "moon_separation_deg": 84.0,
  "moon_illumination": 0.22,

  "snr_estimate": 42.1,
  "transparency_index": 0.94,

  "verdict": "PASS",
  "reject_reasons": []
}
```

**Never send histograms.** Percentiles plus saturation/clipping fractions carry the same
information in six numbers.

### 7.3 Frame verdict (deterministic, not agent)

Rules, all configurable in `session.yaml`:

- `REJECT` if `eccentricity > 0.60`
- `REJECT` if `hfd > 1.4 × rolling_baseline_hfd`
- `REJECT` if `guide_dropped_frames > 0` or `guide_peak_arcsec > 3 × guide_rms_total`
- `REJECT` if `saturated_fraction > 0.02` (satellite/aircraft trail heuristic)
- `REJECT` if `star_count_delta_percent < -50` (cloud)
- `WARN` (kept, flagged) for values within 80–100 % of any threshold
- else `PASS`

Rejected frames are **moved, never deleted**, to `rejected/` within the session directory.

### 7.4 Block summary (what the agent actually receives)

Emitted every N frames or on event. Aggregates and trends, never series:

```json
{
  "event": "block_complete",
  "target": "NGC 7000", "filter": "Ha", "frames": 20, "duration_min": 100,
  "hfd": {"start": 3.42, "end": 4.10, "slope_per_min": 0.008},
  "temp_delta_c": -2.3,
  "transparency": {"start": 0.94, "end": 0.71, "slope_per_min": -0.0023},
  "guide_rms_mean_arcsec": 0.51, "guide_rms_stability": "stable",
  "rejects": 2, "reject_reasons": {"eccentricity": 1, "guide_peak": 1},
  "integration_total_min": {"L": 100, "R": 40, "G": 40, "B": 40},
  "altitude_deg": 38.0, "altitude_trend": "descending",
  "minutes_until_altitude_limit": 47,
  "minutes_until_meridian_limit": 112,
  "minutes_until_astronomical_dawn": 168
}
```

### 7.5 Detail on demand

Tool `get_frame_detail(frame_ids: list[int], fields: list[str])` for diagnosis. Expected
to be rarely called, but it is the difference between diagnosing and guessing when
something goes wrong.

---

## 8. Agent integration

### 8.1 Division of responsibility

**Deterministic code owns anything with an objective threshold:**

- Frame verdicts (§7.3)
- **Optimal exposure calculation.** Pure arithmetic: measure real `background_rate_adu_s`,
  convert to e⁻/s with `e_per_adu`, solve for the exposure at which sky shot noise
  dominates read noise by the configured factor (default: background ≥ 10 × RN²). Clamp
  to `[min_exposure, max_exposure]` and to guiding capability. **The agent must never
  estimate this.**
- Refocus triggering, dithering, meridian handling, TEC ramping, emergency parking
- Flat exposure solving (§10.2)

**The agent owns judgement:**

- Initial session plan: target selection, ordering, filter allocation given altitude
  windows, moon separation, transparency and existing integration history
- Reallocation under changing conditions
- Diagnosis of uncatalogued anomalies
- Deciding when a target is "done" (accumulated SNR vs. continued integration)
- Deciding whether to end the session early

### 8.2 Tool surface

Read-only tools (always available):

`get_session_state` · `get_block_summary` · `get_frame_detail` · `get_target_visibility`
· `get_integration_history` · `get_weather` · `get_catalog_search` · `get_sky_conditions`
· `get_collimation_status`

`get_collimation_status` returns the last measurement's scalars and its age in sessions
(§10.4.6). There is no corresponding action tool and there will not be one: collimation
needs a hand on the telescope, so the agent may observe and remark, never start it.

Action tools (gated by autonomy level, always validated by §9):

`propose_plan(plan)` · `set_target(target, filter, exposure_s, count)` ·
`request_refocus(reason)` · `request_meridian_decision(action)` · `skip_target(reason)` ·
`end_session(reason)` · `notify_operator(severity, message)`

Every action tool call requires a `rationale: str` argument. It is stored and shown in the
UI. No silent decisions.

### 8.3 Invocation model — pull, not push

The agent is **not** called per frame. It is woken by:

- **Scheduled poll** — configurable, default every 15 min
- **Events** — the following codes trigger an immediate call:

| Code | Meaning |
|---|---|
| `BLOCK_COMPLETE` | Planned block of frames finished |
| `TARGET_LIMIT_APPROACHING` | Altitude or meridian limit within N minutes |
| `FOCUS_DRIFT` | HFD trend exceeded threshold after a refocus |
| `TRANSPARENCY_DROP` | Sustained transparency decline |
| `GUIDING_DEGRADED` | RMS above warn threshold for M consecutive frames |
| `REJECT_RATE_HIGH` | Reject rate above threshold in the last block |
| `SOLVE_FAILED` | Plate solve failure not recovered automatically |
| `TARGET_COMPLETE` | Planned integration reached |
| `SESSION_START` / `SESSION_END` | |
| `SAFETY_ABORT` | Post-hoc notification only — the abort already happened |

`SAFETY_ABORT` is informational. The safety layer never waits for the agent.

Expected budget: a 6-hour session ≈ 50 calls, 30–60 k tokens total.

### 8.4 Autonomy levels

| Level | Behaviour |
|---|---|
| `observer` | Read-only. Comments, does not act. |
| `advisory` | Proposes actions; each requires operator approval in the UI. **Default.** |
| `supervised` | Acts autonomously; operator notified of each action, can veto within a timeout |
| `autonomous` | Acts freely within safety limits. Requires `meridian.calibrated: true` |

**Human precedence rule:** any operator action through the UI immediately demotes the
agent to `advisory` and emits `OPERATOR_INTERVENTION`. Re-promotion is explicit only.

### 8.5 Degradation

If the API is unreachable, the budget is exhausted, or the agent returns invalid tool
calls three times consecutively: log, notify, and continue in **autonomous rule-based
mode** — finish the current target, apply deterministic triggers, park at the altitude
limit or at dawn. The night is never lost because the agent is unavailable.

---

## 9. Safety governor

**Layer 2 sits between the agent/API and Ekos. It is not bypassable. Every command
passes through `safety.validate(command) -> Ok | Rejected(reason)`.**

### 9.1 Meridian and tripod collision — CRITICAL

Without a tripod extension, the 200PDS **will strike the tripod legs** near the meridian.
This is the highest-severity risk in the project.

Requirements:

1. `safety.yaml` ships with `meridian.calibrated: false` and null limits.
2. **The system must refuse `supervised` and `autonomous` modes while `calibrated` is
   false.** Hard failure with an explicit message, not a warning.
3. `docs/meridian-calibration.md` documents an operator procedure: with the OTA mounted in
   its normal configuration and the mount powered but not tracking, slew slowly in RA
   through the meridian region in both declination extremes, recording the hour angle at
   which clearance falls below a hand's width. Repeat for the declination range actually
   used. Record the most restrictive values.
4. Measured values minus `safety_margin_deg` become the enforced hard limits.
5. Limits are enforced **in the governor**, not in Ekos and never in the agent. The agent
   physically cannot emit a slew that violates them — the tool call is rejected before
   reaching the executor.
6. Approaching the limit triggers the configured `flip_strategy`. After any flip:
   tracking resumes **only** after a successful plate solve confirming position, if
   `require_solve_after_flip` is true. Failure to solve → park.
7. If `counterweight_fitted` changes, limits are invalidated and recalibration is required.

### 9.2 Other hard limits

Altitude floor and ceiling · sun avoidance and sun-altitude gate · TEC ramp rate and
delta-from-ambient ceiling · disk space floor · mount communication watchdog. Any
violation → reject the command; any breach detected in state → abort per §9.4.

### 9.3 Dew — blocking for unattended operation

The Newtonian secondary is the classic unattended failure mode: it fogs around hour 3,
HFD rises gradually with no sharp symptom, and the operator wakes to half a night of
veiled frames.

**M6 (unattended) must not be enabled until a secondary dew heater with INDI-controllable
PWM is fitted.** The implementation must:

- Refuse `autonomous` mode if no dew heater device is present in config
- Modulate PWM from ambient temperature and dew point (weather API or local sensor)

**The local sensor now exists, and it is the preferred source.** An AHT20 is fitted
(§2.2), so dew point can be computed on the rig rather than fetched from a weather API —
which removes a network dependency from a control path, per design principle 4. Two
constraints on using it, both measured:

- **Temperature and humidity come from the AHT20, never from the BMP280.** The BMP280
  self-heats and read +0.96 °C against the AHT20 on the same board. That biases dew point
  by about a degree, and against a "keep the secondary above dew point" control target it
  under-heats the mirror in precisely the marginal conditions where the heater matters.
  Any sensor pair fitted later gets checked the same way — this is a general property of
  combined pressure/temperature parts, not a defect of this unit.
- **A CRC failure is a gap, not a value.** The AHT20 has no register map, only command
  sequences with a CRC8 on every read. A failed CRC means bus corruption; log the gap and
  propagate nothing.

Outdoors the sensor needs a **radiation shield**. Unshielded in sunlight it measures its
own solar heating — several degrees, ten times its specification.
- Detect the dew signature independently: slow monotonic HFD rise **with** rising
  background **and** falling star count, without temperature change → `DEW_SUSPECTED`

### 9.4 Weather

Explicit scope decision by the operator: **rain is monitored by the human, not the
system.** There is no observatory; parking does not protect the equipment. Therefore:

- The system refuses to *start* an unattended session if the precipitation probability
  from the weather API exceeds the configured threshold
- Cloud detection is software-only: star-count collapse + SNR drop + guiding loss,
  cross-checked against the weather API, confirmed over `cloud_confirm_frames`
- On any rapid-degradation signal: park, close the session, and send a Telegram alert
  with `severity: alarm` intended to wake the operator
- The documentation must state this limitation prominently

### 9.5 Watchdog

An independent process monitors the orchestrator's heartbeat. On loss: stop tracking,
park, ramp the TEC down, notify. It must not depend on the FastAPI process.

### 9.6 Collimation — the operator is touching the telescope

`COLLIMATE` (§10.4) is the only state in which a person is expected to have both hands on
the moving instrument. Everything else in §9 protects the equipment from the software;
this protects the operator from it, and it is enforced in the governor:

1. **While the session is in `COLLIMATE`, the governor refuses every mount command except
   sidereal tracking.** No slew, no park, no pulse guide, no motion vector, from any
   source — operator, agent or deterministic trigger. A queued slew does not fire on the
   way out; leaving the state is an explicit operator action and nothing else resumes.
2. **Entering `COLLIMATE` is refused unless the autonomy level is `observer` or
   `advisory`.** Hard failure with an explicit message, never a warning and never a
   demotion-and-proceed. In `supervised` and `autonomous` there is nobody at the tube by
   definition, so the state has no meaning; the assistant is not a thing to be run
   unattended, it is a thing that cannot be.
3. **The agent has no tool that enters, leaves or drives it.** §8.2 exposes the result
   and nothing else. This is enforced by the tool surface, not by prompting.
4. **The one slew the assistant needs — to the chosen star — happens before the state is
   entered**, as an ordinary slew through `safety.validate()`: altitude floor and ceiling,
   Sun avoidance, meridian limits, slew rate. The assistant gets no exemption and creates
   no new pointing path.
5. Centring during the loop is done by moving the **region of interest in software**, not
   the mount (§10.4.3).

---

## 10. Session workflow

### 10.1 State machine

```
IDLE → SETUP → COOLING → [COLLIMATE] → POLAR_ALIGN → FOCUS_INITIAL → PLAN
     → SLEW → SOLVE_CENTER → FOCUS → GUIDE_CALIBRATE → IMAGING
     ⇄ (REFOCUS | DITHER | MERIDIAN_FLIP | TARGET_CHANGE)
     → CALIBRATION_FRAMES → WARMUP → STACKING → COMPLETE
                                   ↘ ABORTED
```

`COLLIMATE` (§10.4) is bracketed because it is optional and operator-entered. It needs
tracking, not an accurate polar alignment, so it sits early, where the operator is already
standing at the telescope — but a roughly aligned mount walks the star out of the usable
on-axis zone in minutes, so it is equally enterable **after** `POLAR_ALIGN`, which removes
the drift problem outright. §10.4.3 gives the numbers and the assistant recommends between
them from the drift it measures. It may be re-entered from `PLAN` at any time while
attended, and it is refused outright in `supervised` and `autonomous` (§9.6).

Every transition is logged with timestamp, trigger source (`operator` | `agent` |
`deterministic` | `safety`) and rationale.

### 10.2 Calibration frames

**Darks:** library indexed by `(gain, offset, sensor_temp_c, exposure_s, binning)`, with
configurable expiry. Captured automatically using the Dark filter slot. The system checks
library coverage at session start and reports gaps.

**Flats:** the panel is a manual light table, so the system runs a **guided assistant** at
session end:

1. Prompt the operator to place the panel (UI + Telegram)
2. For each filter: take a test frame, measure median ADU, scale the exposure toward the
   target (default 50 % of full well), iterate up to 3 times, converge
3. Capture N flats per filter (default 20)
4. Immediately capture matching dark-flats using the Dark filter
5. Store indexed by `(filter, optical_train_id, date, exposure_s, gain)`

Flat validity is bound to `optical_train_id` — switching to the MPCC invalidates them.

### 10.3 Stacking

Siril CLI jobs, queued after session close (or on demand), running at low priority.

The system produces **multiple stacks under different rejection criteria** and reports
comparative metrics so the operator — or the agent — can choose:

| Variant | Frame selection |
|---|---|
| `all` | Every `PASS` frame |
| `strict` | Top 70 % by FWHM |
| `best_fwhm` | Top 40 % by FWHM |
| `best_guiding` | Top 60 % by guide RMS |

For each variant, report: frames used, total integration, background noise σ, measured
FWHM of the stack, and SNR on a fixed reference region. **Selecting the winner is a
judgement call — present the metrics, let the agent or operator decide.**

Output: calibrated, registered, stacked 32-bit FITS. No stretching, no colour calibration,
no cosmetics. Those belong on the operator's PC.

### 10.4 Collimation assistant

The 200PDS is a Newtonian on a portable mount. It is transported, assembled, and pointed
at a different part of the sky every night, and its primary mirror moves in its cell as
the tube tips. Checking collimation is among the **first things the operator does**, ahead
of polar alignment and long before the first sub — get it wrong and every frame of the
night carries the error, and no amount of stacking removes it.

A Cheshire and a laser get the mirrors close, indoors, in the light. What they cannot do
is confirm the result through the actual imaging train, at the altitude the telescope will
work at, with the camera that will take the pictures. That is what this assistant is for.

**It measures; the operator turns the screws.** There is no motorised collimation on this
rig and none is planned, so the assistant closes its loop through a human: Nocturne shows
the current error, the operator turns a screw, the number moves within a second.
Everything specified below exists to protect that one second.

#### 10.4.1 Method — the defocused star

A bright star, defocused, images the pupil: a bright disc with the shadow of the secondary
inside it. Perfect collimation puts the shadow concentric within the disc; miscollimation
displaces it, and the direction of the displacement is the direction of the error.

Per frame, on the region of interest:

1. Subtract the background, threshold at a fraction of the peak, and take the **outer
   disc**: its flux-weighted centroid `c_outer` and radius `R_outer`.
2. Inside it, take the **obstruction**: the connected dark region, its centroid `c_inner`
   and radius `R_inner`.
3. The error is the decentre, **normalised by the annulus width**:

   ```
   error = |c_inner − c_outer| / (R_outer − R_inner)
   angle = position angle of (c_inner − c_outer), in the displayed image,
           0° up, increasing clockwise
   ```

Normalising is what makes the number usable. A raw pixel offset scales with how far the
operator happened to defocus, so it changes when nothing about the telescope has; the
normalised value is very nearly invariant to defocus, binning and plate scale, which is
why the thresholds in §5.1 are dimensionless and survive the OAG and MPCC migrations
untouched.

The **angle is reported in the displayed image**, not on the sky. The operator is looking
at a phone, not at a star chart, and the arrow has to point the way the screen does.

The required defocus follows from the geometry rather than from a magic number — the donut
diameter is the projected aperture at the defocused plane:

```
diameter_px  =  defocus_mm × 1000 / (f_ratio × pixel_size_um)   [unbinned]
```

so a 140 px donut on the ASI533MM at f/5 needs ≈ 2.6 mm of defocus, and
`focuser.step_size_um` converts that to EAF steps. That value is measured in M2; until it
exists the assistant ramps away from focus, in the configured `defocus.direction`, until
the donut first exceeds `defocus.min_diameter_px`, and reports the steps it used.

The obstruction is likewise predicted, not assumed: `central_obstruction_mm / aperture_mm`
gives the expected `R_inner / R_outer` — 0.29 on this tube — and a measured ratio far from
it means the detector has found something that is not a donut. See §10.4.5.

#### 10.4.2 Which mirror this corrects

A Newtonian has two adjustable mirrors and the star test does not treat them equally. The
secondary has three tilt screws around a central bolt that also sets its rotation and its
position under the focuser. The primary has three collimation bolts, each with a locking
screw behind it. Six screws, two jobs, and only one of them is what this assistant is
about.

**With the star on the optical axis, the shadow's decentre is dominated by tilt of the
primary.** Those are the three screws the arrow refers to and the three that
`collimation.screws.names` labels (§5.1).

The secondary is a **prerequisite, not an alternative**. Getting it centred under the
focuser, correctly rotated and correctly tilted is what a Cheshire or a sight tube is for,
on the bench, in the light, before the telescope goes outside. A misaligned secondary
barely moves an on-axis shadow; what it does is push the coma-free point away from the
centre of the field, so its signature is at the **edges** of the frame, asymmetrically,
exactly where an on-axis measurement is blind by construction. Separating that signature
from sensor tilt is the analysis deferred in §15.

So the honest boundary is this: the assistant tells the operator how to move the primary,
and tells them **when the primary is not the answer** — if the on-axis error cannot be
brought below `error_acceptable` however the primary is moved, the check ends with
`suspect_not_primary` rather than inviting another twenty minutes on the wrong screws. It
does not attempt to say *which* other thing it is.

**Locking changes what was just measured.** The primary's locking screws move the mirror
as they are tightened — a well-known property of this cell and not a defect in it — so the
sequence does not end at the last adjustment. It ends: lock the screws, re-measure,
and **the post-lock measurement is the one stored** (§10.4.6). A check recorded before the
locks were tightened has measured a state the telescope will not be in for the rest of the
night.

#### 10.4.3 The loop, and how long the star stays usable

The whole value of this feature is latency. A collimation screw is turned in small
fractions of a turn, and a loop that takes five seconds to answer turns a two-minute job
into a twenty-minute one, in the cold, in the dark.

- **Region of interest**, `loop.roi_px` square, binned `loop.binning`, centred on the
  star. Not the full 3008² frame: a 512 px ROI at bin 2 is ~0.1 % of the pixels and that
  is the entire reason the loop can run at video rate on a Pi.
- **Exposure auto-tuned** to bring the annulus peak into a band around
  `exposure_target_peak_adu`, clamped to `[exposure_min_s, exposure_max_s]`. If it cannot
  reach the band, that is a `NoMeasurement`, not a measurement taken anyway.
- **Cadence: at least `loop.min_update_hz` sustained, measured end to end** — shutter to
  pixels on the operator's phone. This is an acceptance criterion (§14, M4), not an
  aspiration.
- **Smoothing.** Report the median over `loop.smoothing_frames` together with its spread.
  Seeing moves the centroid frame to frame and is not miscollimation; a number that
  twitches invites the operator to chase it. The spread is displayed, because a large one
  means "wait for the seeing", not "turn something".
- **Convergence** is declared only after `thresholds.converged_frames` consecutive frames
  below `error_good`. One good frame is a gust of good seeing.
- **Recentring** tracks the donut between frames and shifts the ROI to follow it. The
  mount is not commanded (§9.6). If the star is lost for `loop.lost_star_frames`
  consecutive frames the loop stops and says so.
- **Preview** is the ROI through the §11.3 pipeline — small, so it is cheap at video
  rate. The overlay (arrow, circles, centroids) is drawn client-side from the measurement,
  never burnt into the JPEG.


##### How long the star stays where it is needed

The star has to stay near the optical axis for the whole check, and two things move it:
the sky, and the mount's inability to follow it exactly. Tracking about a polar axis
misaligned by θ walks the star across the field at roughly θ × 15°/hour:

| Polar misalignment | Drift | Time to cross a 5′ on-axis zone from its centre |
|---|---|---|
| 10′ — a solved alignment | 2.6″/min | ~1 hour |
| 1° | 16″/min | ~9 min |
| 3° — pointed north by eye | 47″/min | ~3 min |

Three minutes is enough for one adjustment and not for three, and the operator is looking
at a mirror cell, not at a clock. Hence:

- The assistant **measures the drift from the star's own track** and displays the time
  remaining in the on-axis zone. It does not infer it from a polar alignment nobody told
  it about.
- **The ROI follows the star in software**, never the mount (§9.6), and only out to
  `loop.max_offaxis_arcmin`. Past that the donut carries field aberrations rather than
  collimation, so the result is `star_off_axis`: a `NoMeasurement`, never a number
  (§10.4.5). The full 39′ frame is not the working area — the middle few arcminutes are.
- **Re-centring therefore ends the check.** The loop stops, the operator takes their hands
  off the telescope, and the slew happens outside `COLLIMATE`. This is deliberate: a
  system that quietly re-centres is a system that moves a telescope somebody is holding.

Which settles where `COLLIMATE` belongs in §10.1. It needs *tracking*, not accuracy — a
mount pointed north by eye buys minutes, and the countdown is on screen — so it is
enterable straight after `COOLING`, where the operator is already at the tube. But
entering it **after `POLAR_ALIGN`** removes the problem outright for the cost of the two
or three minutes Ekos takes, so it is enterable from there too, and the UI recommends it
when the measured drift would empty the zone within `loop.drift_warn_minutes`.

**Transport.** Ekos first, per ADR 0001: if the Ekos DBus interface exposes ROI and fast
readout, drive it. Otherwise the camera properties are written directly through INDI —
which today is an ungated `SetProperty` path (ADR 0007, issue #1). **That is a hard
prerequisite: the assistant is not implemented until `SetProperty` is gated**, and its
writes are then restricted to an allowlist of camera and focuser properties containing no
mount property at all. A feature whose purpose is to let a person stand next to the
telescope must not be the thing that reopens an unchecked path to the mount.

#### 10.4.4 Which screw is that arrow?

The arrow points in image coordinates. Which of the **primary's** three collimation bolts
(§10.4.2) corresponds to that direction depends on camera rotation, the focuser's
clocking, the mirror cell and which side of the tube the operator is standing on. Deriving
it means modelling all four and being wrong about one of them silently.

So it is measured. The assistant asks the operator to turn one named screw a small amount,
records the displacement vector that results, and repeats for a second screw; two vectors
determine the mapping and the third screw is inferred. Thereafter the arrow is **labelled
with the screw name and the direction to turn it**.

The mapping is measurement data, so it lives in **SQLite with a timestamp and an
`optical_train_id`**, never in `config/` — nothing in Nocturne writes configuration
(ADR 0013). It is invalidated by a change of optical train, and the UI shows its age so a
rotated camera cannot quietly keep an old mapping alive.

Until a mapping exists, the arrow is drawn unlabelled. An unlabelled arrow is still the
whole job; the labels only save the first thirty seconds of trial and error.

#### 10.4.5 What it refuses to measure

**A collimated telescope and a broken detector produce the same picture: a number near
zero.** This is the failure mode CLAUDE.md §2 is about, in its most dangerous form,
because here "nothing found" is also the desired outcome.

The result type therefore has two shapes and no third:

```
Measured(error, angle_deg, spread, diameter_px, obstruction_ratio, frames)
NoMeasurement(reason)
```

**No code path converts the second into the first with `error = 0`.** The UI renders them
as different things — a measurement, or an explanation — and a `NoMeasurement` never
counts toward convergence, is never stored as a check, and never satisfies the history
prompt of §5.1.

`reason` is a closed vocabulary (ADR 0004): `no_star`, `multiple_stars`, `too_faint`,
`saturated`, `donut_too_small`, `donut_too_large`, `obstruction_not_found`,
`obstruction_ratio_implausible`, `star_lost`, `star_off_axis`, `exposure_out_of_range`,
`unobstructed_train`.

The tests that guard this are positive controls, not absence checks:

- Synthetic donuts with an **injected decentre** of known magnitude and angle, swept
  across diameter, obstruction ratio, seeing, SNR and background gradient. The measured
  error must match the injected one within tolerance, and the measured angle within a few
  degrees.
- A **concentric** donut must read below `error_good` — asserted only alongside the
  injected cases, because "reads zero" is meaningless from a detector not simultaneously
  shown to read non-zero when it should.
- Every degenerate input — empty field, two stars, a saturated blob, a donut below
  `min_diameter_px`, an in-focus star, a star past `max_offaxis_arcmin`, a refractor
  train — must return the matching `NoMeasurement` reason. Asserting the reason, not
  merely that it failed.

#### 10.4.6 The measurement record

One record per check, persisted and returned by `get_collimation_status`:

```json
{
  "collimation_check_id": 12,
  "session_id": "2026-08-12",
  "utc": "2026-08-12T21:41:08Z",
  "optical_train_id": "native",
  "star": {"name": "Vega", "magnitude": 0.03},
  "altitude_deg": 61.2, "azimuth_deg": 104.5,
  "ambient_temp_c": 18.4, "focuser_temp_c": 17.9,
  "focuser_position": 24310, "defocus_steps": 1180,
  "donut_diameter_px": 138, "obstruction_ratio": 0.30,
  "offaxis_arcmin": 1.4, "drift_arcsec_per_min": 3.1,
  "error": 0.031, "error_spread": 0.008, "angle_deg": 212.4,
  "frames": 20,
  "start_error": 0.184, "duration_s": 214,
  "locks_tightened": true,
  "verdict": "GOOD", "suspect_not_primary": false
}
```

`locks_tightened` is not decoration: a record with it false was taken mid-adjustment and
is not the state the telescope observed in (§10.4.2). Only a post-lock record satisfies
the history prompt of §5.1.

`verdict` is `GOOD` | `ACCEPTABLE` | `POOR` against `thresholds`, computed
deterministically. **Altitude and azimuth are part of the record because the reading is
only comparable to another taken near the same place in the sky** — mirror flop is not
noise, and a history chart that ignores it invents a drift that is not there. The UI plots
altitude alongside error for exactly this reason.

Nothing here is a per-frame telemetry field: collimation is checked a few times a night at
most, so it is its own record and does not enlarge §7.2.

#### 10.4.7 What this is not

- **Not a replacement for a Cheshire or a laser.** Those get the mirrors close with the
  tube horizontal and the lights on. This confirms the result on the sky, through the
  imaging train, and finds the error the bench tools left behind.
- **Not automatic.** Nothing turns a screw. See §9.6 for why it also cannot be attempted.
- **Not a tilt or field-curvature analysis.** Off-axis star shapes carry information about
  sensor tilt and about the coma-free point, and separating those two from each other is a
  real problem that this section does not attempt. §15 records it as deferred.
- **Not applicable to an unobstructed train.** With `central_obstruction_mm: null` there
  is no shadow to centre; the assistant returns `unobstructed_train` and says so plainly
  rather than measuring noise.

---

## 11. Web application

### 11.1 Delivery

React PWA, built to static assets, served by FastAPI. Installable, full-screen. Primary
access over Ethernet/LAN; Tailscale for remote; Pi WiFi AP as fallback so the operator
retains control on the terrace if the house network fails.

### 11.2 API contract

REST for actions, WebSocket for streams.

```
GET    /api/v1/status
GET    /api/v1/equipment
POST   /api/v1/session/start | /stop | /abort
GET    /api/v1/session/{id}
GET    /api/v1/session/{id}/frames
GET    /api/v1/session/{id}/preview/{frame_id}     # JPEG, see §11.3
POST   /api/v1/targets/search
GET    /api/v1/targets/{id}/visibility             # altitude curve, moon, windows
POST   /api/v1/targets/{id}/framing                # FOV overlay + rotation
POST   /api/v1/plan                                # submit or approve a plan
GET    /api/v1/agent/log
POST   /api/v1/agent/autonomy
POST   /api/v1/agent/approve/{proposal_id}
POST   /api/v1/agent/message                       # operator → agent chat
POST   /api/v1/calibration/flats/assistant
GET    /api/v1/calibration/library
POST   /api/v1/collimation/start | /stop           # §10.4; refused unless attended
GET    /api/v1/collimation/status
GET    /api/v1/collimation/history?optical_train=
POST   /api/v1/collimation/screw-calibration       # §10.4.4
POST   /api/v1/stacking/jobs
GET    /api/v1/archive?session=&target=&filter=

WS     /ws/telemetry     # frame records, state transitions, events
WS     /ws/agent         # agent messages, proposals, approvals
WS     /ws/collimation   # measurement records + ROI preview frames, §10.4.3
```

Auth: bearer token, even on LAN. An unauthenticated `POST /abort` is a lost night.

### 11.3 Preview pipeline

**Never send FITS to the client.** Server-side: downsample to ~1024 px, autostretch
(midtone transfer function), encode JPEG q80. 18 MB → ~150 KB, instant over LAN.

### 11.4 Views

1. **Plan** — catalogue search (local Messier/NGC/IC, no internet), tonight's altitude
   curve, moon separation, and framing: the ASI533 FOV rectangle overlaid on a chart
   rendered from a local star catalogue, with adjustable rotation. Select one target or
   hand several to the agent to prioritise.
2. **Live** — latest preview, telemetry charts (HFD, guide RMS, temperature, background,
   transparency), current state, next planned action, countdown to limits.
3. **Agent** — decision log with rationales, pending proposals with approve/reject,
   free-text chat, autonomy control, token budget consumed.
4. **Archive** — browse by session/target/filter, thumbnails, reject marking, per-file
   download, and **bulk transfer via the Samba path** (not HTTP).
5. **Collimate** — §10.4. The ROI preview full-bleed, the arrow and circles overlaid, the
   error large enough to read at arm's length with the phone propped against something,
   and the history plotted against altitude. Three controls — start, stop, calibrate
   screws — because the operator has one hand free and is wearing gloves. **Verdict is
   never carried by colour:** night mode is red on black (§11.5), so there is no green to
   go good with. A filled bar against threshold marks, and a translated word.

### 11.5 Night mode

Non-negotiable default: red on black, no white surfaces, no bright accents, large touch
targets. At 03:00, cold and dark-adapted, a white UI destroys half an hour of night
vision. Implement as the default theme, with a day theme available for planning.

---

## 12. Internationalisation

`react-i18next`, locales `en` (source), `fr`, `es`. All user-facing strings are keys.
CI fails if any locale has missing keys relative to `en`, or if a literal string appears in
a rendered component. Includes: units, date/time formats, number formats, and Telegram
notification templates. Astronomical object designations are not translated; common names
are.

---

## 13. Development methodology

### 13.1 Spec-driven with TDD against simulators

INDI ships simulator drivers (`indi_simulator_ccd`, `indi_simulator_telescope`,
`indi_simulator_focus`, `indi_simulator_wheel`, `indi_simulator_guide`). The entire test
suite runs against these — **no hardware required for development or CI.** This is
mandatory given that no human reviews the code line by line.

Workflow per feature:

1. Write or update the spec section
2. Write failing tests encoding the acceptance criteria
3. Implement until green
4. Run the full suite
5. Commit with a message referencing the spec section

### 13.2 Test requirements

- **Unit**: pure functions — exposure solver, verdict rules, flat exposure convergence,
  safety limit arithmetic, telemetry reduction. Property-based tests (hypothesis) for the
  safety governor.
- **Integration**: full session against simulators, including induced failures — mount
  disconnect, solve failure, guiding loss, disk full, cloud simulation.
- **Safety**: an explicit suite that attempts to violate every limit in §9 through every
  entry point (agent tool, REST API, direct executor call) and asserts rejection. **This
  suite must be exhaustive.**
- **Agent**: mocked API responses including malformed tool calls, timeouts, budget
  exhaustion — asserting correct degradation.
- Synthetic FITS fixtures with known star fields to validate HFD/FWHM/eccentricity
  measurement against ground truth.

### 13.3 CI

GitHub Actions: lint (ruff, mypy strict, eslint), unit + integration on simulators,
i18n completeness check, frontend build. All must pass before merge.

---

## 14. Milestones and acceptance criteria

### M1 — Instrument control

Bring up INDI + Ekos headless; connect all five devices.

- **AUTO**: orchestrator connects to all five simulator drivers, reads and writes
  properties, survives driver restart, reconnects automatically.
- **HITL**: `indi_eqmod_telescope` connects to the Wave 150i over USB serial **without
  the SynScan app acting as a bridge**. Slew, track, sync and pulse guide all verified.
  ASI533MM cools and captures; EFW cycles all 8 positions; EAF moves absolute and reports
  temperature; ASI120MM Mini streams.

> **This is the highest-risk item in the project.** Some NINA users report the Wave 150i
> only connecting through the SynScan serial bridge. If direct connection fails, fall back
> to the WiFi driver and record the outcome as an ADR before proceeding. **Do M1 first.**

### M2 — ASIAir parity, attended

Polar alignment, plate solve and centre, V-curve autofocus, guiding with dithering,
sequenced capture, meridian flip.

- **AUTO**: a full simulated sequence completes — align, solve, focus, guide, capture 20
  subs across 4 filters, dither, flip, resume, park.
- **HITL**: a real attended session produces usable subs. Focuser backlash measured and
  recorded. Meridian limits calibrated per §9.1 and committed to `safety.yaml`.

### M3 — Telemetry and stacking

Frame analysis pipeline, SQLite persistence, calibration library, Siril multi-variant
stacking.

- **AUTO**: HFD/FWHM/eccentricity measured on synthetic fixtures within 5 % of ground
  truth. Exposure solver produces correct results across a sky-brightness sweep. Flat
  assistant converges within 3 iterations across a 100× brightness range. Four stack
  variants produced with comparative metrics. **Collimation measurement core (§10.4.1):**
  injected decentre recovered within 10 % in magnitude and 5° in angle across diameter,
  obstruction ratio, seeing, SNR and gradient sweeps; a concentric donut reads below
  `error_good`, asserted in the same suite as the injected cases; every degenerate input
  returns its specific `NoMeasurement` reason and never a zero error.
- **HITL**: measured values on real subs agree with Ekos/PixInsight within 10 %.

### M4 — Web application

Full PWA, all four views, i18n, night mode, Samba share.

- **AUTO**: Playwright coverage of all views; API contract tests; i18n completeness.
  **Collimation assistant (§10.4):** end-to-end against the camera simulator sustaining
  `loop.min_update_hz` shutter-to-client; entering `COLLIMATE` refused in `supervised` and
  `autonomous`; every mount command except tracking refused while in `COLLIMATE`, proved
  through each entry point, agent tool surface included; screw mapping stored in SQLite
  and invalidated by an optical-train change; a star past `max_offaxis_arcmin` returns
  `star_off_axis` rather than a number, and re-centring ends the check instead of slewing.
  Prerequisite: `SetProperty` gated (issue #1).
- **HITL**: usable one-handed on the phone, on the terrace, at night, in Spanish. On the
  200PDS, with the secondary already squared with a Cheshire: a deliberately introduced
  small **primary** miscollimation is detected, the labelled arrow names the bolt that
  corrects it and the direction to turn, and the error returns below `error_good` and
  **stays there after the locks are tightened** — with the phone propped at the tube, in
  the dark, in under five minutes. Separately: `max_offaxis_arcmin` is confirmed or
  corrected by walking a star out of the zone and watching where the number stops tracking
  the primary.

### M5 — Agent, advisory mode

Tool surface, event bus, budget guard, decision log.

- **AUTO**: agent completes a simulated session in `advisory`. Every action requires
  approval. Safety suite passes with the agent as the entry point. Budget exhaustion,
  API failure and malformed tool calls all degrade correctly.
- **HITL**: agent proposals over a real session are sensible; operator approves each.

### M6 — Unattended

`supervised` then `autonomous`, cloud detection, dew management, watchdog.

- **Prerequisites**: `meridian.calibrated: true`; dew heater fitted and INDI-controllable;
  NVMe storage; Ethernet link.
- **AUTO**: simulated cloud, dew, guiding loss and comms loss each trigger the correct
  response. Watchdog parks on orchestrator kill.
- **HITL**: three consecutive supervised sessions with no safety intervention, then one
  unattended session with the operator monitoring remotely.

---

## 15. Open items

- Sensor characterisation (read noise, e⁻/ADU, full well per gain) must be measured, not
  assumed from datasheets. Procedure in `docs/sensor-characterisation.md`.
- EAF backlash and step size: measured in M2.
- Focuser temperature-compensation coefficient: measured after several sessions; disabled
  until then.
- OAG migration: config change only, but guide-star availability with the ASI120MM Mini's
  4.8 × 3.6 mm sensor at f/5 will require careful prism placement. Not a software problem.
- MPCC Mark III: adding the `mpcc` optical train requires re-deriving focal length from
  plate solves and invalidating the flat library.
- **Merge Addendum A into this document before M3** — as §16–§18 with a revised §14, per
  ADR 0016. Sensor characterisation ceases to be an open item at that point; §A.1 takes it
  over as a hard M3 prerequisite.
- **Timing has no offline path yet.** PPS marks the instant within a second but does not
  say *which* second, and that label currently comes from internet NTP. gpsd's
  shared-memory refclock is the fix and has not been brought up. Not blocking at the home
  site, which has wired Ethernet; blocking for any deployment without network
  (`docs/FIELD-NOTES-TIMING.md` §2.9).
- Environmental sensors are fitted and read by nothing. Adding the AHT20 to
  `equipment.yaml`, and reading timing state through `chronyc -c tracking` rather than
  `/dev/pps0` — which is root-only, and the backend stays unprivileged — are M3/M6 work.
- The GPS receiver is re-marked silicon of unknown vendor, not the u-blox NEO-6M its
  label claims. A genuine NEO-M8N is on order; `docs/FIELD-NOTES-TIMING.md` §7 lists what
  carries over and what must be re-measured.
- Collimation defocus in EAF steps is derived from `focuser.step_size_um` (§10.4.1), which
  is measured in M2. Until then the assistant ramps to the donut diameter empirically.
- `collimation.loop.max_offaxis_arcmin` ships at 5′ as a **provisional** bound on where an
  on-axis interpretation of the shadow still holds. It is a property of this telescope's
  field, not a measurement, and M4's HITL confirms or corrects it (§14).
- **Field-wide coma and tilt analysis is deferred.** Star elongation across the frame
  carries both the displacement of the coma-free point (collimation) and sensor tilt, and
  the two are not cleanly separable from a single frame. It would run passively on every
  sub at no extra cost in observing time, which makes it attractive; it is out of §10.4
  because a metric that cannot say which of two causes it saw would be read as if it
  could. Revisit with real data from M3 onward.
- Whether KStars 3.8.3's own collimation aids are reachable over DBus headless is not
  recorded (ADR 0017, docs/FIELD-NOTES-M1.md §26). Introspect before implementing §10.4.
