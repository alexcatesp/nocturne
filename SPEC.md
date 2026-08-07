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

### 2.3 Observing site

Fixed site, portable setup: residential terrace, Tudela de Duero, Valladolid, Spain.
Roughly 42° N, 700 m elevation. Bortle ~5–6. Restricted horizon (buildings, walls). The
mount is set up and torn down per session; polar alignment is performed each night.

**Precise coordinates are deliberately not in this repository.** A residential site to
four decimal places is a home address to within metres, and this document sits beside an
inventory of equipment left outside overnight and a schedule of when nobody is watching
it. `equipment.yaml` ships a generic placeholder; the operator supplies the real values
locally and Nocturne warns at every startup until they do.

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
    corrector: null
  - id: "mpcc"
    active: false
    focal_length_mm: 1000
    aperture_mm: 200
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

---

## 10. Session workflow

### 10.1 State machine

```
IDLE → SETUP → COOLING → POLAR_ALIGN → FOCUS_INITIAL → PLAN
     → SLEW → SOLVE_CENTER → FOCUS → GUIDE_CALIBRATE → IMAGING
     ⇄ (REFOCUS | DITHER | MERIDIAN_FLIP | TARGET_CHANGE)
     → CALIBRATION_FRAMES → WARMUP → STACKING → COMPLETE
                                   ↘ ABORTED
```

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
POST   /api/v1/stacking/jobs
GET    /api/v1/archive?session=&target=&filter=

WS     /ws/telemetry     # frame records, state transitions, events
WS     /ws/agent         # agent messages, proposals, approvals
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
  variants produced with comparative metrics.
- **HITL**: measured values on real subs agree with Ekos/PixInsight within 10 %.

### M4 — Web application

Full PWA, all four views, i18n, night mode, Samba share.

- **AUTO**: Playwright coverage of all views; API contract tests; i18n completeness.
- **HITL**: usable one-handed on the phone, on the terrace, at night, in Spanish.

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
