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

---
---

# Field Notes — M1, part two: the four remaining devices

**Recorded 2026-08-07, on the reference rig.** Same standing as part one: evidence,
not instruction. Where it contradicts SPEC.md or a config file, the contradiction is
the point.

Property dumps: `backend/tests/fixtures/hardware/devices-properties.txt` and
`devices-properties-final.txt` (the wheel again, after its positions were cycled).

---

## 9. HEADLINE: M1 hardware is complete

**All five devices pass.** Only the KStars build remains outstanding in M1.

The INDI device names, exactly as the drivers announce them — these are the
`indi_device_name` values:

| Device | `indi_device_name` | Driver | Version |
|---|---|---|---|
| Imaging camera | `ZWO CCD ASI533MM Pro` | `indi_asi_ccd` | 2.7 |
| Guide camera | `ZWO CCD ASI120MM Mini` | `indi_asi_ccd` | 2.7 |
| Filter wheel | `ZWO EFW` | `indi_asi_wheel` | 2.7 |
| Focuser | `ZWO EAF` | `indi_asi_focuser` | 1.2 |
| Mount | `EQMod Mount` | `indi_eqmod_telescope` | 1.4 |

All four ZWO devices connected first time over standard USB. Nothing of the
CDC-ACM kind (part one, §2.1) — that was the mount's peculiarity alone.

Verified against hardware: the 533MM reports 3008x3008 at 3.76 µm; the 120MM Mini
1280x960 at 3.75 µm; the EAF absolute position 828 and 18.44 °C; the EFW cycles
positions and returns.

**Cooling verified.** 16.1 °C to 14.6 °C in 60 seconds, TEC at 20 %, target 5 °C.
The ramp is proportional and slows near setpoint, which is expected behaviour.
SPEC §14's M1 criterion "ASI533MM cools and captures" is met.

---

## 10. The filter wheel is wrong in two separate ways

### 10.1 `equipment.yaml` has the wrong slot order

Verified physically, by opening the wheel and reading the filter markings:

| Slot | Actually fitted | SPEC §5.1 said |
|---|---|---|
| 1 | **L** | Dark |
| 2 | **R** | L |
| 3 | **G** | R |
| 4 | **B** | G |
| 5 | **Dark** | B |
| 6–8 | empty | empty |

**Every slot was wrong.** This is not an off-by-one to patch; it is a different
mapping. `equipment.yaml` and SPEC §5.1 both need correcting.

### 10.2 The driver's stored names are ZWO factory defaults

```
FILTER_NAME.FILTER_SLOT_NAME_1 = Red
FILTER_NAME.FILTER_SLOT_NAME_2 = Green
FILTER_NAME.FILTER_SLOT_NAME_3 = Blue
FILTER_NAME.FILTER_SLOT_NAME_4 = H_Alpha
FILTER_NAME.FILTER_SLOT_NAME_5 = SII
FILTER_NAME.FILTER_SLOT_NAME_6 = OIII
FILTER_NAME.FILTER_SLOT_NAME_7 = LPR
FILTER_NAME.FILTER_SLOT_NAME_8 = Luminance
```

These bear no relation to what is in the wheel, and **that is dangerous quietly**.
Anything reading `FILTER_NAME` from the driver would believe slot 4 is H-Alpha when
it is B, and would file flats and darks against the wrong channel. Nothing in the
resulting image would look wrong.

**Requirement.** Nocturne *writes* filter names to the driver from `equipment.yaml`
on connect. It never reads them as a source of truth. If the driver's names differ
at connect, that is not a discrepancy to reconcile — configuration wins, overwrite.
Log the overwrite, so a wheel that has been physically rearranged leaves a trace.

No code path may read `FILTER_NAME` into a decision.

---

## 11. The slew-rate pattern is general, not the mount's

The mount starts at 800× on every connection (part one, §3.2). The cameras do the
same thing with different values:

```
ZWO CCD ASI533MM Pro.CCD_CONTROLS.Gain   = 200      (equipment.yaml gain profile: 100)
ZWO CCD ASI533MM Pro.CCD_CONTROLS.Offset = 1
ZWO CCD ASI120MM Mini.CCD_CONTROLS.Gain  = 50
ZWO CCD ASI120MM Mini.CCD_CONTROLS.Offset = 0
```

**Driver defaults are not our configuration, on any device.** What `MountLink` does
for the slew ceiling has to become one mechanism covering all five devices: apply
the configured values on connect, re-apply on every disconnected-to-connected
transition, with the same test discipline.

The mount must not be special-cased. If this exists once and covers everything, a
new device gets the protection by construction.

---

## 12. Four smaller corrections

**`FOCUS_MAX.FOCUS_MAX_VALUE` reads 100000**; `equipment.yaml` says 60000. The
driver value is authoritative — it comes from the hardware.

**`FOCUS_BACKLASH_STEPS` reads 180**, with `FOCUS_BACKLASH_TOGGLE` disabled. SPEC
has `backlash_steps: 0`, "measured in M2". 180 is a ZWO default, not a measurement,
and 0 is worse than reading the driver. The M2 backlash procedure must note that a
default is already present and has to be distinguished from a measured value.

**`CCD_INFO.CCD_BITSPERPIXEL` reads 16 on both cameras.** That is the *container*,
not the converter: the IMX533's ADC is 14-bit and SPEC §2.1 is correct as written.
Recorded here so that nobody later "corrects" the SPEC to match the driver.

**The EFW exposes `FILTER_CALIBRATION.CALIBRATE` and
`FILTER_UNIDIRECTIONAL_MOTION`** (currently disabled). Neither is needed now.
Unidirectional motion may matter for repeatability if filter positioning proves
inconsistent — noted, not enabled.

---

## 13. What remains

The KStars build. That is the whole of M1's outstanding work.

Once it builds, [issue #2](https://github.com/alexcatesp/nocturne/issues/2) opens:
introspect the live DBus interface, replace every guessed method name, add the
Optical Trains interface from 3.8.2, and rebuild `fake_kstars.py` from captured
introspection XML rather than from assumptions.

---
---

# Field Notes — M1, part three: the build

**Recorded 2026-08-07, on the reference rig.** Same standing as parts one and two:
evidence, not instruction. Where it contradicts SPEC.md, a config file or an ADR,
the contradiction is the point — and in §15 it contradicts an ADR.

---

## 14. HEADLINE: M1 is complete, and `install.sh` had never really run

**KStars 3.8.3 builds and installs. `--check` passes clean.** Every M1 criterion,
AUTO and HITL, is now met.

The installer ran end to end for the first time in the process, and **failed five
times**. Five failures, one defect: *it verified dependencies without installing
them.* Each stopped a configure step, in this order:

| Missing | Stopped | Why it was missing |
|---|---|---|
| `wcslib-dev` | StellarSolver, then KStars | installed one stage later, inside `build_kstars` |
| `libeigen3-dev` | same | same |
| the Qt6 set | KStars | verified by `--check-packages`, never installed |
| the KF6 set | KStars | same |
| `libopencv-dev` (>= 4.6.0, ~500 MB) | KStars | named nowhere at all |

The shape of it is the part worth keeping. The Qt6 and KF6 names were in
`QT6_BUILD_PACKAGES` and `KF6_BUILD_PACKAGES`, which `--check-packages` read and
reported clean on — a genuine, correct, useful check. Nothing then ran `apt-get
install` on them at a point where it mattered. **Verifying a dependency is not
installing it**, and a check that passes is easily mistaken for a machine that is
ready.

`wcslib-dev` and `libeigen3-dev` are the same defect from the other direction:
they were installed, but from a second list, inline inside `install_kstars_
dependencies()`, which `--check-packages` could not see and which ran a stage
*after* StellarSolver needed them. Two lists, and they had diverged.

`libopencv-dev` was in neither. There is no check that would have caught it,
because it was not a name anything knew about.

**What changed.** One array set at the top of the script, one function
(`required_packages()`) that reads them, and three consumers — the preflight
gate, `--check-packages`, and a single install stage that runs before any
compiler. "Checked but not installed" is no longer a thing the script can
express. `backend/tests/unit/test_install_packages.py` runs the real check and
the real install against a stubbed apt and compares the two sets; its positive
control is a variant of the script where an install stage smuggles in a package
of its own, which is the original bug exactly.

An hour of compilation is a slow way to find a two-second `apt-get`.

---

## 15. KStars needs `-DBUILD_WITH_QT6=ON`, and ADR 0008 did not know it

**This is the important one.**

KStars 3.8.3, `CMakeLists.txt` line 17:

```cmake
option(BUILD_WITH_QT6 "Build using Qt6" OFF)
```

**Default OFF.** Without the flag, configure looks for Qt5 5.12.7. Trixie ships no
Qt5 at all. So on the platform this project deliberately chose *because it ships
KF6*, the KStars build is impossible unless KStars is told to use it.

What was built by hand, and what the installer now does:

```bash
cmake -B ../kstars-build -S . \
  -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_WITH_QT6=ON -DBUILD_TESTING=OFF
```

**This contradicts the premise of ADR 0008.** That ADR chose Trixie because it
ships packaged KF6 rather than KF5, and that reasoning was right — the packages
are there, measured (0008, Consequences). What nobody checked is that KStars has
to be *told* to use them. The ADR reads as though having KF6 available is
sufficient. It is necessary and not sufficient, and the gap between those two is
a build that cannot complete. ADR 0008 now carries the flag as a decision rather
than a footnote.

**The second-order problem, which is worse than the first.** CMake ignores `-D`
for a variable no `CMakeLists.txt` declares. It notes it at the end of the
configure log, under "Manually-specified variables were not used by the project",
and nothing fails. So if `BUILD_WITH_QT6` is ever renamed upstream, passing it
becomes a silent no-op and the build falls back to looking for Qt5 — the original
failure, now disguised as a flag that was set. `require_qt6_option()` reads the
checked-out tree and refuses if the option is not declared in it.

---

## 16. The KStars pin is now verified from inside the tree

ADR 0006 recorded a caveat: the guard confirms HEAD *is* commit `61d849b0`, but
nothing confirmed that commit *is* 3.8.3. **The checked-out tree declares its own
version**, so the caveat is discharged:

```
CMakeLists.txt:  PROJECT(kstars VERSION 3.8.3 ...)
commit           61d849b0, 2026-06-14, "INDI drivers sync"
```

3.8.3 was released 1 June 2026. The pinned commit is dated **two weeks after**,
and the change it carries is an **INDI driver sync** — which is the single most
relevant post-release fix this project could ask for.

So the pin is: 3.8.3, plus post-release fixes to the stable line, and the fix it
carries is one we want. The cautious framing in ADR 0006 — "3.8.3 plus whatever
the maintainers had added" — was correct, and can now be stated as fact rather
than inference. It is stated as fact in ADR 0006 and the caveat is removed.

---

## 17. Four optional dependencies, deliberately absent

KStars' configure step warns about each of these and builds without them. All
warnings, none blocking, **none needed on a headless imaging box**:

| Absent | What it is for |
|---|---|
| `Qt6DataVisualization` | 3D charts in the GUI |
| `Qt6Keychain` | credential store for online services |
| `LibXISF` | PixInsight's native image format |
| `Cups` | printing |

Recorded because a clean configure log is a tempting thing to chase, and chasing
this one adds four dependencies to an unattended imaging computer in exchange for
nothing. The list is repeated as a comment in `install.sh` beside the package
arrays, where someone would go to add them.

If one ever becomes necessary it belongs in an array with a reason beside it, not
in an `apt-get` somewhere in the body.

---

## 18. Storage

**KStars consumed roughly 9 GB of build artefacts.** After cleanup: **13.9 GB free
of 28.1 GB**, with 39 astrometry index files installed.

A build that runs out of space dies at around 80 %, which is an hour spent to
arrive at a preventable error, with a large tree left behind. The installer now
projects the requirement before the KStars stage and refuses rather than starting:

```
info: KStars needs about 10 GB of build artefacts; 13 GB free under ~/.cache/nocturne-build
```

10 GB, not 9: the ~0.5 GB of `libopencv-dev` is part of the same stage, and the
number should be the one that makes the stage safe rather than the one that was
measured for part of it.

The preflight's `REQUIRED_DISK_GB=12` still gates the whole install up front. The
KStars check is separate because it is the one stage that can exhaust the disk on
its own, and because by the time it runs the earlier stages have spent some.

---

## 19. What `--check` reports on a complete install

Clean, and the three guards all announce themselves rather than staying quiet:

- filter order reads `1:L 2:R 3:G 4:B 5:Dark` — the corrected order from §10.1
- the mount port defers to the driver — `port: null`, as shipped
- **removable-flash stacking refusal**, **meridian not calibrated**, and
  **coordinates still placeholder** all fire

The placeholder-coordinates warning did exactly what it was designed to do: the
shipped `config/equipment.yaml` carries a generic reference site, and the warning
is what stops that being mistaken for a configured one.

---

## 20. What remains

Nothing in M1.

[Issue #2](https://github.com/alexcatesp/nocturne/issues/2) is open: KStars is
installed and its DBus interface can be introspected on live hardware. The source
tree is at `~/.cache/nocturne-build/kstars`.

The capture procedure is `docs/ekos-dbus-capture.md`. Its output belongs in
`backend/tests/fixtures/hardware/`, and `fake_kstars.py` gets rebuilt from it
rather than from assumptions.
