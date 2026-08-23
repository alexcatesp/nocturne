# 0020 — The meridian limits bracket a keep-out band, not a permitted range

Status: Accepted · 2026-08-23 · Milestone M2
Corrects code shipped in M1 and in the M2 pointing gate. Affects SPEC §9.1,
`docs/meridian-calibration.md`, `schemas/safety.py`, `safety/rules.py`.

## Context

`safety.yaml` carries two measured numbers, `hour_angle_east_limit_deg` and
`hour_angle_west_limit_deg`. Every piece of code that has ever touched them read
them as **the edges of a permitted range**: pointing was allowed between them and
refused outside. `MeridianLimits.effective_hour_angle_limits_deg()` narrowed the
range by the safety margin, and `enforce_meridian_limits` refused any hour angle
below the east limit or above the west one.

That is backwards, and with the measured values the reference rig will produce it
would have meant the mount could only ever point within about twenty degrees of
culmination — an hour and a half of sky, once a night, per target.

It was found while writing the derivation that turns marked points into a stored
profile: deciding which of two marks is "more restrictive" forces the question of
which side of the number is the dangerous one, and the answer did not match the
code.

### The evidence

**The operator, asked directly** (2026-08-23), and this is the decisive part
because it is a statement about their own telescope:

> No tiene sentido que el tubo apunte a objetos por debajo del horizonte y el
> límite hacia arriba lo impone que el extremo del tubo donde está la celda del
> espejo primario choque con las patas.

The limit is set by the **primary mirror cell end** of the tube reaching a tripod
leg, and it binds *upwards*. A Newtonian points steeply when a target culminates;
the closed end of the tube — the heavy one, with the cell — swings down as it
does. Culmination is the meridian. So the collision region is the band around
hour angle zero, and the sky outside it is free.

**`docs/meridian-calibration.md` has said so since M1**, in two places, and it
is the operator-facing document:

> A hard limit at hour angle −20° means: stop, or flip, twenty degrees (eighty
> minutes of tracking) before the meridian.

and the procedure itself, which drives *towards* the meridian from the east and
records the hour angle where clearance runs out — a negative number, reached
before the meridian. Everything between that and the meridian is unreachable.

**The schema's own validator** is named `_limits_bracket_the_meridian` and
requires `east < west`. A band brackets something; a range does not.

**SPEC §9.1.6** says approaching the limit triggers the flip. You approach it by
approaching the meridian, and the flip is what restores clearance by putting the
mirror cell end on the other side of the mount. Under the permitted-range
reading, "approaching the limit" would have meant approaching the horizon, where
a flip changes nothing.

### How it survived

SPEC §9.1.4 says "measured values minus `safety_margin_deg` become the enforced
hard limits". Read literally as arithmetic on both numbers it produces a
narrower range, which is the permitted-range reading; read as "more
conservative", which is what it means, it produces a wider band. M1 took it
literally, and every test written afterwards — including the ones written for
the pointing gate last week — encoded the same misreading, because the same
author wrote the code and the tests. This is the second time that has happened
in this repository: `docs/FIELD-NOTES-M1.md` §21 records the first, where the
Ekos DBus stub agreed with a wrong bridge for exactly the same reason.

What broke the loop this time was not a test. It was a document written for the
operator, in the operator's language, describing what they would physically do.

## Decision

**The two limits bracket a forbidden band.** A pointing command is refused when
its hour angle lies strictly between them.

**The safety margin widens that band at both edges**: measured −25 and +20 with
a margin of 5 are enforced as "nothing between −30 and +25". The margin always
costs sky and never lends any, at either edge, and that direction is now
asserted as a property rather than as an example.

The validator that refused a margin larger than the measurement is gone; under
this reading a large margin is simply a strict one. What replaces it is a check
that the widened band does not close the whole circle, which is the only way the
margin can now produce a configuration that refuses every hour angle there is.

`nocturne check-config` prints the band as a refusal — "no pointing between −27.0
and +23.0 deg" — rather than as two numbers. Printing a pair invites the reading
this ADR exists to correct.

### What was withdrawn

An `absolute_hour_angle_bound_deg` key was added to `safety.yaml` an hour before
this was found, as an envelope the calibration tool's measured profiles could
never exceed. It is withdrawn in the same change that corrects the sense, because
it guards the wrong direction: under the keep-out reading, a limit marked too far
from the meridian produces a *wider* forbidden band, which costs sky and endangers
nothing. The dangerous mis-mark is one that is too *narrow* — marking at −2° when
clearance actually ran out at −25° — and no outer bound can catch that. Only the
measurement can.

`latitude_tolerance_deg`, added in the same commit, stays: its reasoning is
independent and unaffected.

## Consequences

- **Nothing was at risk.** The rule has never enforced anything on real hardware:
  `meridian.calibrated` is false on the reference rig, and ADR 0019 refuses every
  pointing command while it is. The inversion would have been discovered the
  first time the mount was asked to point at a rising target, as a refusal.
- Seven tests in `test_safety_pointing.py`, two in `test_config_safety.py` and
  one integration target encoded the old sense and were rewritten. That count is
  the useful measure of how thoroughly a misconception propagates through a suite
  written alongside the code it checks.
- **The shape of the limit is still an open question, and the calibration tool
  must not prejudge it.** What the operator described is a constraint on how high
  the tube may point, expressed through where the mirror cell ends up. Hour angle
  is a proxy for that: altitude is a function of hour angle *and* declination, so
  two targets at the same hour angle and different declinations do not put the
  cell in the same place. The single pair is the conservative simplification
  `docs/meridian-calibration.md` argues for, and it stays — but every mark the
  tool records will carry the hour angle, the declination, the altitude and the
  pier side, so that if the measurements show the band is strongly
  declination-dependent, the evidence to model it properly already exists and
  nobody has to go back to the terrace.
- The operator has not measured the numbers yet and said so plainly. Until they
  do, this ADR changes what the code *would* enforce, not what it enforces.
