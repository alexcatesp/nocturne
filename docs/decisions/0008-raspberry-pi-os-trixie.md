# 0008 — Raspberry Pi OS Trixie, not Bookworm

Status: Accepted · 2026-08-05 · Milestone M1
Premise confirmed by measurement on the reference rig, 2026-08-07 — see
Consequences.
**Amended 2026-08-07 after KStars was actually built: having KF6 is necessary
and not sufficient. See "Trixie is not enough on its own".**
Supersedes the OS row of SPEC section 4, which named Bookworm.

## Context

SPEC section 4 originally said "Raspberry Pi OS 64-bit (Bookworm or later)".
That was written before the KStars version was settled, and it does not survive
contact with it.

**KStars 3.8.x is a Qt6/KF6 application.** Bookworm is Debian 12 and ships the
KDE Frameworks 5 development packages; there is no packaged KF6. Building
KStars 3.8.3 there would mean building Qt6 and then the whole KF6 tree from
source on a Raspberry Pi first — days of compilation, on a machine with 8 GB of
RAM and a microSD card, which in practice does not complete. It is not a slow
path to the same place; it is a path that ends in a failed build after a very
long time.

Raspberry Pi OS is now Trixie-based (Debian 13), which ships KDE Plasma 6.3 and
therefore packaged KF6 and Qt6. On Trixie, KStars' toolkit dependencies are an
`apt-get install`.

The version is not optional currency. The **Optical Trains DBus interface**,
which is exactly the surface `backend/nocturne/executor/ekos.py` targets,
arrived in KStars **3.8.2**. Building an older release would leave Nocturne
working around the absence of an interface that exists upstream.

## Options considered

1. **Stay on Bookworm, build Qt6 and KF6 from source.** Days of compilation
   with a low probability of completing, and a machine afterwards carrying a
   hand-built toolkit that no distribution updates. The Pi is an imaging
   computer that has to survive unattended nights, not a build farm.
2. **Stay on Bookworm, build KStars 3.7.x against KF5.** Loses the Optical
   Trains interface and pins the project to a KStars series that upstream has
   moved past. It also defers the same migration to a worse moment — during M2,
   with session code already written against the older interface.
3. **Require Trixie.** One re-image, then packaged Qt6/KF6.

## Decision

Option 3. Raspberry Pi OS 64-bit, **Trixie (Debian 13) or later**, is a
requirement, and `scripts/install.sh` **refuses to start** on anything older.

Two preflight checks run **before any compiler is invoked**, in the same spirit
as the `KSTARS_REF` refusal in ADR 0006 — fail in the first ten seconds with
a specific reason, not an hour into a build:

1. `check_os_release()` — reads `/etc/os-release`. If the OS is Debian or
   Raspbian and its major version is below 13, it stops and explains the KF6
   reasoning. A Debian-derived OS with a different version scheme (Ubuntu's
   `24.04`, for example) is not compared numerically; it warns and defers to the
   package check, which is the real gate.
2. `check_kstars_build_dependencies()` — walks `QT6_BUILD_PACKAGES` and
   `KF6_BUILD_PACKAGES` and sorts each into installed, *available in the
   archive*, or **unknown to apt**. Any package apt has never heard of stops the
   run, listing the names. That single check catches both failure modes: an OS
   that cannot supply KF6 at all, and a package name in this script that is
   wrong for the release in front of it.

The KF6 half of both checks is skipped when `--skip-kstars` is passed. The Qt6
half is not: StellarSolver is a Qt6 library and is built either way.

## Trixie is not enough on its own — `-DBUILD_WITH_QT6=ON`

**This ADR's premise was right and incomplete, and the gap between the two is a
build that cannot complete.** Recorded here rather than as a footnote, because
"Trixie ships KF6, therefore KStars builds" is exactly what this document said
and it is not true.

KStars 3.8.3, `CMakeLists.txt` line 17:

```cmake
option(BUILD_WITH_QT6 "Build using Qt6" OFF)
```

**The default is OFF.** Without the flag, configure looks for Qt5 5.12.7 —
and Trixie ships no Qt5 at all. So on the platform chosen *because* it ships Qt6
and KF6, the KStars build is impossible unless KStars is told to use them.
Having the packages available and having KStars use them are two different
things, and only the first was checked when this decision was written.

`build_kstars()` therefore configures with:

```
-DBUILD_WITH_QT6=ON -DBUILD_TESTING=OFF
```

**And verifies the option exists before passing it.** CMake ignores a `-D` for a
variable no `CMakeLists.txt` declares — it mentions it at the end of the
configure log under "Manually-specified variables were not used by the project",
and nothing fails. If `BUILD_WITH_QT6` is renamed upstream, passing it becomes a
silent no-op and the build falls back to hunting for Qt5: the original failure,
disguised as a flag that was set. `require_qt6_option()` greps the checked-out
tree for the declaration and refuses if it is not there, naming the file and the
grep that would find its replacement.

### Optional dependencies that stay absent

KStars' configure warns about four things it cannot find and builds without them.
All four are for features a headless imaging box does not have:

| Absent | For |
|---|---|
| `Qt6DataVisualization` | 3D charts in the GUI |
| `Qt6Keychain` | credential store for online services |
| `LibXISF` | PixInsight's native image format |
| `Cups` | printing |

**Not installing them is the decision, not an oversight.** A clean configure log
is a tempting thing to chase and chasing this one adds four dependencies to a
machine that has to survive unattended nights, in exchange for nothing. The list
is repeated as a comment beside the package arrays in `install.sh`, which is
where someone would go to add them.

## Consequences

- **The Pi must be re-imaged** if it is currently on Bookworm. That is a
  prerequisite for M1's hardware test, alongside the NVMe purchase in SPEC
  section 2.2, and the two are best done in the same sitting.
- KStars' toolkit dependencies are packaged, so the source build is INDI,
  indi-3rdparty, StellarSolver and KStars only — not the toolkit under them.
- Trixie ships Python 3.13 rather than Bookworm's 3.11.2. That invalidates one
  of the justifications in ADR 0002, which is corrected there rather than left
  standing: `XMLPullParser.flush()` does exist on 3.13. The explicit framing in
  the INDI client stays for reasons that do not depend on the interpreter.
- `apt-cache policy libindi-dev` on Trixie is now worth checking (ADR 0006): if
  Debian 13 ships INDI 2.2.3 or later, most of the source build can be dropped.
- **The premise of this ADR is now measured, not assumed.** Checked on the
  reference rig running Trixie, **2026-08-07**, all from `trixie/main`, arm64:

  ```
  qt6-base-dev              6.8.2+dfsg-9+deb13u2
  extra-cmake-modules       6.13.0-1
  libkf6config-dev          6.13.0-2
  libkf6i18n-dev            6.13.0-1
  libkf6widgetsaddons-dev   6.13.0-1
  ```

  Packaged KF6 6.13 and Qt6 6.8 are present and installable with no source
  build. That is exactly what Bookworm cannot supply, which is the whole reason
  this decision exists — so the reasoning above is confirmed by observation
  rather than resting on the release notes.

- **Those five were a sample, not the list.** The whole list has since been
  checked in one go on the reference rig — `./scripts/install.sh
  --check-packages` printed nothing, which is the all-clear — and KStars 3.8.3
  then built and installed. Every name in the arrays is correct for Trixie.
- **`--check-packages` was checking a list nothing installed.** Reporting the
  Qt6/KF6 names as available is not the same as installing them, and for the
  first end-to-end run of the installer it was mistaken for exactly that: the
  check passed, the compile started, and the Qt6 set was still not on the
  machine. That defect and its fix are in `docs/FIELD-NOTES-M1.md` section 14;
  what it changes here is that both checks described above now read
  `required_packages()`, the same function the install stage installs from.
- **The KF6 packages are a build dependency of KStars, and Qt6 is a build
  dependency of StellarSolver too.** `--skip-kstars` therefore drops KF6 and
  `libopencv-dev` but keeps Qt6 and `wcslib-dev`.
