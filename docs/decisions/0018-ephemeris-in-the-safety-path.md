# 0018 — The safety layer computes its own ephemeris; astropy is a test oracle

Status: Accepted · 2026-08-22 · Milestone M2
Affects SPEC sections 4, 9.1, 9.2. Adds a development dependency and no runtime one.

## Context

The pointing limits of SPEC §9.1 and §9.2 are stated in quantities the
configuration does not contain and no module could compute:

- **altitude**, for the floor and the ceiling, from right ascension,
  declination, the site and the time;
- **hour angle**, for the meridian limits, from the same;
- **angular distance from the Sun**, and the **Sun's own altitude**, for
  avoidance and the twilight gate.

Every one of them is a function of *when*. None existed anywhere in the package
at the end of M1. Something has to produce them before a single limit can be
enforced, and that something sits inside the safety path — the most important
code in the repository (CLAUDE.md §2).

SPEC §4 already lists `astropy` in the stack, for image analysis in M3. So the
obvious move is to import it here too, and the obvious move is worth examining
before it is taken, because the safety path has properties the image analysis
path does not.

## Options considered

1. **`astropy` at runtime, in the governor.** Reference-quality accuracy,
   nothing to write, nothing to get wrong. Three costs. It imports numpy and a
   large package graph into a process whose job is to answer a yes/no question
   in microseconds, on a Raspberry Pi. Its Earth-orientation machinery
   (`astropy.utils.iers`) will, by default, **fetch a table over the network**
   when it wants one it does not have, and fall back with a warning when it
   cannot — inside a control path, at night, on a rig whose design principle 4
   is that control paths do not depend on the network. That behaviour is
   configurable, which means the safety of the arrangement rests on a global
   flag being set correctly by whoever imports it first.
2. **A small ephemeris in the safety package, checked against astropy in the
   tests.** About 150 lines: Julian date, mean sidereal time, the rotation from
   equatorial to horizontal coordinates, a truncated solar series, and an
   angular separation. No dependency, no network, no import cost, and every
   number reproducible from the arguments alone.
3. **A lighter third-party ephemeris** (`skyfield`, `pyephem`). Still a
   dependency, still a data file — `skyfield` wants a JPL kernel — and still an
   ADR. The dependency buys precision this application cannot use.

## Decision

**Option 2.**

`backend/nocturne/safety/sky.py` holds the arithmetic. It is pure: no clock, no
configuration, no I/O, no state. The instant arrives as an argument, because the
governor owns the clock and reads it once per decision.

`astropy` is added to the **development** extras and is imported by
`backend/tests/unit/test_safety_sky.py` and nothing else. It is the oracle:
every angle the module produces is compared against it over swept dates,
latitudes, declinations and right ascensions. Two tests assert the arrangement
holds in both directions — astropy present in the dev extras, absent from the
runtime dependencies — because a comparison against a package nobody installed
compares nothing, and a runtime import would silently undo this decision.

### The precision budget, and why it is enough

| Source of error | Magnitude | Reason it is acceptable |
|---|---|---|
| UT1 versus UTC in sidereal time | ≤ 0.9 s of time = 13.5″ | Fixing it needs a table that expires; the smallest limit here is 5° |
| Truncated solar series | ~0.01° quoted, 0.011° measured | The Sun avoidance circle is 30° |
| Nutation, polar motion, aberration | < 1″ | Four orders below any limit |
| Refraction | not modelled, ~30′ at the horizon | The limits are geometric: refraction moves the image, not the tube |

Measured, not asserted: over 200 random instants between 2000 and 2024, across
all latitudes and the whole sky, the worst disagreement with astropy is **18
arcseconds in altitude** and **40 arcseconds in the Sun's position**. The
tightest number any of it is compared against is the 5° meridian safety margin.

### The frame

Coordinates are **equinox of date, apparent place** — what `EQUATORIAL_EOD_COORD`
carries and what the mount means. The tests compare against astropy's `TETE`
frame for that reason. Comparing against `ICRS` would have shown about 0.3° of
disagreement at this epoch, all of it precession, and the natural reading of
that would have been "our arithmetic is wrong".

## Consequences

- The safety layer gains ~150 lines of spherical trigonometry that this project
  now maintains. That is the real cost, and it is bounded: the functions are
  small, total, and pinned to an independent implementation by property tests.
- No network, no data file, no expiry, and no global flag anyone has to set.
  What the governor computes tonight it will compute identically in five years,
  offline, which is what SPEC §8.5 (degradation) assumes.
- The constants in `sky.py` are astronomical, not operational: the length of the
  sidereal day, the obliquity of the ecliptic, the terms of a solar series.
  They are **not** magic numbers in the sense CLAUDE.md §6 forbids — that rule
  is about thresholds an operator tunes, which still come from validated YAML —
  and moving them into a configuration file would invite an edit that can only
  make them wrong.
- M3 will import astropy for image analysis, as SPEC §4 says. It must not import
  it into `nocturne/safety/`; a test fails if it does.
- If a later milestone needs precision this cannot give — solar system bodies,
  satellite avoidance, sub-arcsecond pointing models — that is a different
  problem and gets its own decision. It is not this one.
