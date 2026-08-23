# 0007 — M1 leaves pointing ungated, and how that is contained

Status: **Closed 2026-08-22** — the limits landed as the first work of M2.
Originally: Accepted (as a recorded limitation) · 2026-08-05 · Milestone M1
Tracking: https://github.com/alexcatesp/nocturne/issues/1 (closed)

> **This limitation no longer exists.** `COMMAND_RULES[SetProperty]` now carries
> the pointing rules of `nocturne/safety/rules.py`, and a write to a coordinate
> vector is validated against the altitude window, the Sun and the meridian hour
> angle before it reaches the transport — proved against a real driver in
> `backend/tests/integration/test_pointing_gate_simulator.py`. While the
> meridian limits are uncalibrated, every pointing command is refused outright
> (ADR 0019), which is stricter than the "what closes it" section below
> anticipated. The `xfail(strict=True)` module this ADR relied on did its job:
> the altitude case started passing the moment the rules were registered, and
> the suite went red until the markers were removed on purpose. It has been
> deleted, and its cases live in `backend/tests/unit/test_safety_pointing.py`
> as tests that pass.
>
> One item of "what closes it" was deliberately not done: **no `SlewTo` command
> was introduced.** Nothing performs one yet — the typed slew arrives with the
> mount path that also settles `ON_COORD_SET` and pier side — and a command the
> executor cannot carry out is a partially implemented branch, which CLAUDE.md
> §2 forbids. The governor's default-deny is what holds that position: a
> `SlewTo` added later without rules is rejected, not passed through. The rules
> are written against a *pointing intent* rather than against `SetProperty`, so
> binding them to it is one line and a test.
>
> The rest of this document is kept as written, because how a known hole was
> held open safely for a milestone is the part worth being able to re-read.

## Context

CLAUDE.md invariant 1 says every command reaching the executor has passed
`safety.validate()`. It does. But passing through the governor and being
*constrained* by it are different things, and in M1 they come apart.

`COMMAND_RULES` maps `SetProperty` to an empty tuple: the command is registered,
so the governor's default-deny does not reject it, and no rule refuses it
either. A raw property write therefore reaches the instrument unchecked:

```python
await executor.set_property(
    "Telescope Simulator", "EQUATORIAL_EOD_COORD", {"RA": 0.0, "DEC": -85.0}
)
```

On the real rig that is a slew, with nothing consulting the altitude floor, the
meridian hour angle or the Sun. With a 200PDS and no tripod extension it is the
collision that `docs/meridian-calibration.md` exists to prevent.

This was found while proving the invariant during M1 review, not by an incident.

## Options considered

1. **Enforce the limits now.** They do not exist to enforce.
   `meridian.calibrated` is `false` on this rig and both hour angle limits are
   `null` until the operator completes the calibration procedure; altitude and
   Sun rules need the site ephemeris and target model that arrive with M2. A
   rule written against absent data would either be permissive — worse than
   none, because it would read as protection — or would refuse every write,
   including the non-pointing ones M1 exists to exercise.
2. **Refuse all `SetProperty` in M1.** M1's entire purpose is reading and
   writing device properties (SPEC section 14, M1 AUTO). This would refuse the
   milestone.
3. **Special-case the coordinate vectors and refuse only those.** Closer, but a
   half-rule: it would refuse `EQUATORIAL_EOD_COORD` while leaving
   `TELESCOPE_PARK`, `TELESCOPE_MOTION_NS` and the rest open, and would give the
   appearance of pointing safety without the substance. Partial safety is the
   thing CLAUDE.md section 2 forbids ("no partially implemented branches").
4. **Record it, contain it, and make it impossible to forget.**

## Decision

Option 4.

- **Recorded** here and in issue #1, with the specific remedy.
- **Contained**: M1 ships no slew command, no session state machine and no
  agent. Nothing in the codebase issues a pointing write. The hazard is an open
  door, not traffic through it.
- **Not forgettable**: `backend/tests/unit/test_safety_limits_deferred.py`
  asserts that out-of-limits coordinate writes are refused, marked
  `xfail(strict=True)` against issue #1. It reports as an expected failure in
  every run. When M2 registers the rules and it starts passing, `strict=True`
  fails the whole suite until the marker is removed deliberately — so the limits
  cannot land half-finished and the marker cannot rot in place.

CLAUDE.md section 3 forbids xfail *without* an issue link explaining it. This is
the case it permits.

## What closes it, and when

In M2, **before any slew-issuing code is written**:

1. Rules registered against `SetProperty` in `COMMAND_RULES`: altitude floor and
   ceiling, Sun avoidance and the Sun-altitude gate, and the meridian hour angle
   via `MeridianLimits.effective_hour_angle_limits_deg()` — which already
   applies measured-minus-margin and already raises while uncalibrated.
2. Recognition of the coordinate-bearing vectors, so a write to
   `EQUATORIAL_EOD_COORD`, `EQUATORIAL_COORD` or `TELESCOPE_PARK` is validated as
   pointing rather than as an opaque property write.
3. The same rules bound to whatever explicit `SlewTo` command M2 introduces.
   Registering a command without its rules is already refused by the governor's
   default-deny, so this ordering is enforced rather than remembered.

## Consequences

- **Until M2, do not point the rig at the sky under Nocturne's control.** M1 is
  bring-up: connect, read, write non-pointing properties. This is stated in
  `README.md` and in `docs/hardware-setup.md`, whose procedure is a bench test
  with no OTA fitted and no slew.
- The autonomy gate is unaffected and remains enforced: `supervised` and
  `autonomous` are refused while uncalibrated. It does not, however, constrain a
  direct API call at `advisory`, which is what this ADR is about.
- Every run of the suite prints the deferred limits as expected failures. That
  is intentional noise.
