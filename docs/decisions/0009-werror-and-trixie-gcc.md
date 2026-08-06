# 0009 — INDI's forced `-Werror` is stripped before building on Trixie

Status: Accepted · 2026-08-06 · Milestone M1
Evidence: docs/FIELD-NOTES-M1.md section 5.2 (measured on the reference rig).

## Context

INDI v2.2.4 forces `-Werror` into its compile flags. It was released before the
GCC that Raspberry Pi OS Trixie ships, and the newer compiler emits warnings the
older code does not clear. The project turns those warnings into errors, so the
build stops.

Measured failure, on the real rig:

```
indi_astrotrac_telescope — astrotrac.cpp:1326 and :1397
error: '__builtin___snprintf_chk' ... [-Werror=stringop-overread]
```

That is a driver for an AstroTrac mount. Nobody on this project owns one, it is
not in `config/equipment.yaml`, and Nocturne will never load it. With `-Werror`
it nonetheless kills the entire build at 32 %, taking the ZWO and EQMod drivers
down with it.

**`-DCMAKE_CXX_FLAGS="-Wno-error"` does not fix this.** The project appends
`-Werror` to `COMP_FLAGS` *after* the user's flags, so the last word is the
project's. This was established by trying it.

The flag is set in two source trees, and each one also contains
`check_c_compiler_flag(...)` lines that mention the same flag names. Those are
capability probes: they ask the compiler whether it understands a flag. Patching
one changes what the build *detects*, not what it *enforces*, and silently
alters behaviour elsewhere.

At v2.2.4, measured:

| Tree | File | Forced flags | Probes to leave alone |
|---|---|---|---|
| `indi` | `cmake_modules/CMakeCommon.cmake` | 1 (line 70) | line 29 |
| `indi-3rdparty` | `cmake_modules/CMakeCommon.cmake` | 4 (lines 86, 100, 108, 116) | lines 96, 106, 112 |

A related finding, from the same build: **CMake's cache keeps the old flags.**
Reconfiguring over an existing `build` directory does not reliably pick up the
patch. This cost a full failed build cycle — roughly 40 minutes on a Pi 5 — to
discover.

## Options considered

1. **Pass `-Wno-error` through CMake flags.** Does not work, for the reason
   above. Tried on the rig, not reasoned about.
2. **Pass `CXXFLAGS="-w"` through the environment.** Works, but silences *every*
   warning in both trees, including ones that would be worth seeing if a build
   started failing for a real reason. It also hides the flag's removal from
   anyone reading the installer.
3. **Wait for an upstream release built against the newer GCC.** Unbounded, and
   it blocks M1 entirely. INDI 2.2.4 is the newest tag with a matching
   `indi-3rdparty` pair.
4. **Build only the drivers we need, so the failing translation unit is never
   compiled.** This works for `indi-3rdparty`, where each subdirectory is a
   standalone CMake project — and is done for other reasons (section 5.4 of the
   field notes: 204 MB of checkout, hours of compiling firmware for cameras
   nobody here owns). It does **not** work for the core, where
   `indi_astrotrac_telescope` is part of the one build.
5. **Strip the flag from the source before configuring.**

## Decision

Option 5, plus option 4 where it applies.

`scripts/install.sh` gains `strip_werror <source_dir> <expected_count>`, which
runs after the checkout and before the first `cmake` invocation:

- It edits only lines matching `^\s*set(COMP_FLAGS.*-Werror`, case-insensitively
  — the core spells it `SET(...)`. `check_c_compiler_flag(...)` lines can never
  match that pattern and are left byte-for-byte identical.
- It removes only the fatal part. `-Wall` and `-Wextra` on the same line survive:
  the warnings are still printed, they simply no longer stop the build.
- It is told **how many sites to expect** — 1 for the core, 4 for
  `indi-3rdparty`. If it finds a different number, it refuses and names the file
  and the `grep` command to investigate with. A future tag that moves the flag
  somewhere else will stop the installer in the first seconds rather than
  produce a build with `-Werror` still in it.
- It re-checks after patching and refuses if any forced flag survives.

`cmake_build_install()` now deletes the build directory before configuring, and
deletes it again after installing — the second for space, on a 32 GB card.

Build and install are chained with `&&`. Run as separate statements, a failed
build is followed by an install that reports `file INSTALL cannot find
".../indi_astrotrac_telescope"`, an error that names the install step and hides
the real failure (field notes section 5.3).

`backend/tests/unit/test_install_werror_patch.py` extracts the shell function
from the shipped installer and runs it against fabricated `CMakeCommon.cmake`
files shaped like both upstreams. It asserts the forced flags go, the probes do
not, `-Wall` survives, and a wrong expected count refuses *without modifying the
file*.

## Consequences

- Nocturne builds a **patched** INDI. `versions.lock` records the upstream tag
  and SHA; it does not record that two lines were edited. Anyone reproducing the
  build must use this installer, or apply the same patch by hand — the procedure
  is in `docs/installation.md` section 6.
- Warnings scroll past during the build. That is the intended state and the
  installer says so; a silent build here would mean option 2 had been taken.
- The expected-count check will fire the first time INDI is bumped past v2.2.4.
  That is a deliberate cost: a refusal at the top of the build is cheaper than
  discovering at 32 % that the patch found nothing.
- If a future INDI release drops the forced `-Werror`, `strip_werror` refuses
  (0 found, 1 expected) and this ADR is superseded rather than quietly bypassed.
- `indi_astrotrac_telescope` and its neighbours are still compiled in the core
  build, warnings and all. Removing them would mean patching the driver list,
  which is a larger and less reversible edit to somebody else's tree.
