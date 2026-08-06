# Field Notes — M1 hardware bring-up

**Everything learned on the real rig that cannot be discovered from a development
container.** Recorded August 2026, Raspberry Pi 5 8GB, Raspberry Pi OS Trixie.

This document is evidence, not instruction. Where it contradicts an assumption in
SPEC.md, `install.sh` or an existing ADR, the contradiction is the point — resolve it and
record the resolution as an ADR.

---

## 1. HEADLINE: the mount link works

**M1's highest-risk unknown is closed. RESULT: PASS.**

The Sky-Watcher Wave 150i connects to `indi_eqmod_telescope` over **direct USB serial,
with no SynScan bridge**. The WiFi fallback is not needed.

Evidence, read back from the controller board:

```
MOUNTINFORMATION.MOUNT_TYPE       = CUSTOM
MOUNTINFORMATION.MOTOR_CONTROLLER = 033b
MOUNTINFORMATION.MOUNT_CODE       = 0x45
STEPPERS.RASteps360               = 3878400
STEPPERS.DESteps360               = 3525120
STEPPERS.RAStepsWorm              = 14000000
STEPPERS.DEStepsWorm              = 14000000
EQUATORIAL_EOD_COORD.DEC          = 90          (parked, pointing at the pole)
```

The step counts differ between axes, as a strain wave mount's differing reductions
require. These were read from hardware; a driver inventing values would not produce them.

`MOUNT_TYPE=CUSTOM` is expected. Mount code `0x45` is not in eqmod's table of known
models, so the driver reads parameters from the mount rather than a lookup table. This is
preferable — the values are authoritative.

Full property dump: `wave150i-properties.txt` (commit it as a test fixture).

---

## 2. Connection facts that contradict our assumptions

### 2.1 The mount is CDC-ACM, not a USB-serial bridge

SPEC.md `equipment.yaml` specifies `port: "/dev/ttyUSB0"`. **This is wrong.**

The Wave 150i presents an STM32 virtual COM port:

```
/dev/serial/by-id/usb-STMicroelectronics_STM32_Virtual_ComPort_8F8B50B10E31-if00
  -> ../../ttyACM0
```

There is no FTDI, CH340 or CP210x chip. The kernel binds `cdc_acm`. Any documentation,
detection logic or diagnostic that looks for `ttyUSB*` or names bridge chips will miss
this device.

**Actions:**
- Update the `equipment.yaml` default and its schema documentation
- Config validation should accept `ttyACM*` as well as `ttyUSB*`, and should prefer a
  `/dev/serial/by-id/` path over either
- The bench test script's device-detection guidance needs the same correction

The `by-id` path is stable across reboots and device ordering; `ttyACM0` is not. Use the
former everywhere, including in generated config.

### 2.2 Baud rate is 115200; the driver defaults to 9600

`equipment.yaml` already specifies 115200 — correct. Worth noting that the driver starts
at 9600, so the value must be set explicitly on every connection, before `CONNECT`.

### 2.3 The driver auto-populates the port

`indi_eqmod` fills `DEVICE_PORT.PORT` with the correct `by-id` path without being told.
The executor should read this rather than assume it, and treat a configured port as an
override rather than the primary source.

### 2.4 `dialout` group membership is required

`/dev/ttyACM0` is `root:dialout`. Both the operator's user and `nocturne-dev` need to be
in that group, and **the session must be restarted** for it to take effect. A permission
failure here produces an opaque connection error, so the preflight check should verify
group membership explicitly and say so by name.

---

## 3. Two safety-relevant discoveries

### 3.1 `indi_eqmod` has its own horizon-limit subsystem

```
HORIZONLIMITSDATAFILE.HORIZONLIMITSFILENAME  = ~/.indi/HorizonData.txt
HORIZONLIMITSONLIMIT.HORIZONLIMITSONLIMITTRACK = On
HORIZONLIMITSLIMITGOTO.HORIZONLIMITSLIMITGOTOENABLE = On
HORIZONLIMITSMANAGE.*                        (add / delete / clear points)
HORIZONLIMITSFILEOPERATION.*                 (read / write the file)
```

The driver supports a file of alt/az limit points, enforced against both goto and
tracking, at a level **below anything Nocturne writes**.

This does **not** replace the safety governor — §9 remains authoritative, and the
governor must continue to reject unsafe commands before they reach the executor. But it
is a genuine second layer of defence against the tripod collision problem.

**Recommended:** when the operator completes the meridian calibration of SPEC §9.1, the
derived limits should be written to **both** `safety.yaml` and the driver's
`HorizonData.txt`. Defence in depth, at no cost. Record the decision as an ADR, including
how the two are kept in sync and what happens if they disagree.

### 3.2 Slew rate defaults to maximum

```
TELESCOPE_SLEW_RATE.SLEW_MAX = On
SLEWSPEEDS.RASLEW = 800
SLEWSPEEDS.DESLEW = 800
```

The driver starts at 800× sidereal on every connection. `equipment.yaml` specifies
`slew_rate_max_deg_s: 3.0` deliberately, for a portable setup with no permanent pier.

**This must be applied actively at connection time, and re-applied after any driver
restart.** It is not a one-off configuration. A reconnection that silently restores 800×
is a safety regression, and should be covered by a test.

---

## 4. Mount properties worth knowing

Behaviour and calibration values observed on the reference rig:

| Property | Value | Note |
|---|---|---|
| `TELESCOPE_MOUNT_TYPE` | `EQ_GEM=On` | Correct — meridian flip logic applies |
| `TELESCOPE_HOME.FIND` | present | The home indexer added in INDI 2.2.3 for this mount |
| `TELESCOPE_PARK_POSITION` | RA 8388608, DEC 9269888 | Encoder counts at park |
| `CURRENTSTEPPERS` | matched park position | Mount was parked during the test |
| `TELESCOPE_PIER_SIDE` | `PIER_WEST=On` | |
| `GUIDE_RATE` | 0.5 both axes | Starting point for guide calibration |
| `PULSE_LIMITS.MIN_PULSE` | 10 ms | Floor for pulse guiding |
| `PULSE_LIMITS.MIN_PULSE_TIMER` | 100 | |
| `BACKLASH` | 10 / 10, `USEBACKLASH` off | Strain wave — backlash compensation likely unnecessary, but verify |
| `ST4_GUIDE_RATE_*` | index 2 selected | ST4 not used; guiding is via pulse over serial |
| `GEOGRAPHIC_COORD` | all zero | Nocturne must set site coordinates on connect |
| `TIME_UTC` | correct, `OFFSET=2.00` | NTP working |
| `ALIGNDATAFILE` | `~/.indi/AlignData.xml` | Alignment persists here — relevant to the portable-setup workflow |
| `SIMULATION` | `DISABLE=On` | Confirms this was real hardware |

**Open question for M2 — tracked as [issue #3](https://github.com/alexcatesp/nocturne/issues/3),
with the bench observations that would close it:** `RASTATUS.RARunning=Busy` and
`DESTATUS.DERunning=Busy` were reported while `TELESCOPE_TRACK_STATE.TRACK_OFF=On`. This is most likely how the driver
encodes strain wave motors holding position, but it has not been confirmed. Do not treat
`RARunning` as a reliable indicator of physical motion until its semantics are
established — it is the kind of assumption that produces a watchdog that never fires.

---

## 5. Build findings — `install.sh` will fail without these

### 5.1 Nothing usable ships in Trixie

```
libindi-dev / indi-bin    1.9.9+dfsg-3+b5     (need ≥ 2.2.4)
kstars                    5:3.6.2-2+b5        (need ≥ 3.8.3)
libstellarsolver-dev      2.6-1+b1            (need 2.8)
```

Everything is built from source. **ADR 0006's assertion should be updated from "unverified"
to this measured result.**

There is no apt repository for INDI on Debian ARM. The INDI PPA is Ubuntu-only and adding
it to Debian breaks the system. This should be stated in the installer's error text if a
user asks why it compiles rather than installing packages.

**Critically:** Debian's `indi-bin` 1.9.9 predates Wave 150i support. Any bench test run
against it would fail for reasons unrelated to the hardware and could wrongly trigger the
WiFi fallback decision. The installer must not offer the packaged INDI as a shortcut.

### 5.2 `-Werror` blocks the build, and `CMAKE_CXX_FLAGS` cannot override it

INDI v2.2.4 predates Trixie's GCC. The newer compiler emits warnings the project treats
as fatal.

**Core (`indi`)** — one site, `cmake_modules/CMakeCommon.cmake` line 70:
```cmake
SET(COMP_FLAGS "${COMP_FLAGS} -Werror")
```

Observed failure: `indi_astrotrac_telescope`, `astrotrac.cpp:1326` and `:1397`,
`-Werror=stringop-overread` on an `snprintf` bound. A driver for hardware we do not own —
but with `-Werror` it kills the entire build at 32 %.

**`indi-3rdparty`** — **four** sites, lines 86, 100, 108, 116:
```
86:  set(COMP_FLAGS "${COMP_FLAGS} -Werror")
100: set(COMP_FLAGS "${COMP_FLAGS} -Werror=stringop-truncation")
108: set(COMP_FLAGS "${COMP_FLAGS} -Werror=unused-parameter")
116: set(COMP_FLAGS "${COMP_FLAGS} -Werror=unused-but-set-variable")
```

Lines 29, 96, 106 and 112 in the same file are `check_c_compiler_flag(...)` capability
probes. **Leave them alone** — patching them changes what the build detects, not what it
enforces.

**`-DCMAKE_CXX_FLAGS="-Wno-error"` does not work.** The project appends `-Werror` after
user flags, so its setting wins. The flag must be removed at source, or `CXXFLAGS="-w"`
passed through the environment.

**The `build` directory must be deleted before reconfiguring.** CMake's cache retains the
old flags and reconfiguring over it does not reliably clear them. This cost a full failed
build cycle to discover.

### 5.3 Build and install must be chained

Running `cmake --build` and `sudo cmake --install` as separate commands means a failed
build is followed by an install that reports a missing binary:

```
file INSTALL cannot find ".../indi_astrotrac_telescope": No such file or directory
```

This error names the install step and hides the real failure. Always
`cmake --build build -j2 && sudo cmake --install build`.

### 5.4 Only three third-party components are needed

`libasi` (with `-DBUILD_LIBS=1`, mandatory), `indi-asi`, `indi-eqmod`. The full
`indi-3rdparty` checkout is 204 MB and building all of it wastes hours and gigabytes.
`libasi` must be installed first or the drivers cannot find the ZWO SDK headers.

### 5.5 Version pinning verified

The `indi-3rdparty` v2.2.4 tag resolved to commit
`64fbe2e2dcded132e107d764d4965e034b810a3f` — **exactly the SHA recorded in
`versions.lock`**. The pinning mechanism from ADR 0006 works end to end.

---

## 6. Storage and memory

Reference rig runs from **32 GB microSD**, not NVMe. Budget deferred; NVMe remains
required from M3 per SPEC.

Space consumed: ~5.2 GB after OS install, ~6.9 GB after INDI core, ~8.1 GB after
third-party drivers. Roughly 19 GB remained before StellarSolver and KStars. **KStars
with Qt6/KF6 development packages is expected to be the tightest point.** Build
directories must be deleted after each `make install`.

**Add a startup check** that reports the root filesystem device type and free space, and
**refuses to start a stacking job on removable flash storage** with an explicit message.
A clear refusal at M3 is better than a corrupted card mid-stack.

Swap is disabled to protect the card (`dphys-swapfile`), which removes the compiler's
safety net. **zram** restores it without writing to storage. If a build dies without an
error message, check `dmesg` for the OOM killer and fall back to `-j1`.

---

## 7. Still outstanding

| Item | Blocking | Owner |
|---|---|---|
| KStars 3.8.3 tag + commit SHA | KStars build, therefore all Ekos work | Operator |
| Qt6/KF6 package names verified against Trixie | `install.sh` preflight | Operator |
| Camera, EFW, EAF, guide camera bench tests | M1 HITL completion | Operator |
| `install.sh` end-to-end run | Never executed; expect failures | Operator |
| Ekos DBus method names | Issue #2 — still guesses | Claude Code, once KStars builds |
| `RARunning` semantics while not tracking | Watchdog correctness | M2 — [issue #3](https://github.com/alexcatesp/nocturne/issues/3) |

---

## 8. What this changes in the repository

Suggested, not prescriptive — decide and record as ADRs:

1. **`equipment.yaml`**: default mount port to the `by-id` CDC-ACM path; schema accepts
   `ttyACM*`; document that the driver auto-detects.
2. **`install.sh`**: patch `-Werror` at both source trees (1 site in core, 4 in
   3rdparty); delete `build` before configuring; chain build and install; build only the
   three needed third-party components; add the free-space and OS preflight; state why
   packaged INDI is refused.
3. **ADR 0006**: replace the unverified distribution claim with the measured Trixie
   versions.
4. **New ADR**: the `-Werror` incompatibility, with the specific file, lines and failing
   translation unit, so it is not rediscovered.
5. **New ADR**: dual enforcement of meridian limits in `safety.yaml` and the driver's
   `HorizonData.txt`, including sync and disagreement handling.
6. **Executor**: apply the slew rate limit on every connection and after every driver
   restart, with a test.
7. **Test fixtures**: commit `wave150i-properties.txt` as the reference property set for
   `indi_eqmod` with this mount. Note that it is real hardware output, unlike
   `fake_kstars.py`, which shares an author with the bridge it tests.
