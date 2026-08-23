# 0024 — Solving is a read, and what follows it is a command

Status: Accepted · 2026-08-23 · Milestone M2
Affects SPEC §1.2, §3, §4, §9.1, §10.1. Extends ADR 0023.

## Context

The align module is the first thing Nocturne drives that can move the instrument
**without being asked to**. `captureAndSolve()` exposes a frame and solves it;
what happens next is decided by a mode set on some earlier call, and
`Align::executeGOTO()` acts on that mode with nothing of Nocturne's in the path:

```cpp
void Align::executeGOTO()
{
    if (m_SolveFromFile)                          // loadAndSlew sets this
    {
        m_DestinationCoord = m_AlignCoord;
        m_TargetCoord = m_AlignCoord;
        SlewToTarget();                           // regardless of the mode
    }
    else if (m_CurrentGotoMode == GOTO_SYNC)  Sync();
    else if (m_CurrentGotoMode == GOTO_SLEW)  SlewToTarget();
}
```

So a plate solve is a mount command wearing a camera's clothes. Set the mode
wrong once — or let something else set it — and every solve afterwards syncs or
slews, with the safety governor seeing nothing at all. That is the second door
CLAUDE.md invariant 1 exists to forbid, and it opens by accident rather than by
anyone deciding to open it.

Three further facts, all from the source at the pinned commit and all recorded
in `kstars-mount-dbus-units.txt`, shaped what could be done about it:

- **`loadAndSlew` sets `m_SolveFromFile` *and* `m_CurrentGotoMode = GOTO_SLEW`.**
  It slews whatever the mode was, and leaves the mode on "slew" afterwards. One
  call to it turns the *next* ordinary solve into a slew too.
- **`getSolutionResult()` returns three doubles, not two, and the first is not
  what its own documentation says.** The implementation returns
  `sOrientation << sRA << sDEC`; the doxygen comment says "First item is RA in
  degrees. Second item is DEC in degrees." A caller following the comment reads
  the field orientation as a right ascension and the right ascension as a
  declination — a position that is not anywhere, syncable into the mount's
  model, with nothing in the `ad` signature to object.
- **The solution is J2000 and the mount is not.** `align_solver.cpp` stores it
  with `setRA0(ra / 15.0)` and then calls `apparentCoord()` before using it,
  because KStars knows the mount works in the equinox of date. About 0.3 degrees
  apart at this epoch — six per cent of the shipped meridian margin, and it
  would be written into the mount's model by every sync that followed a solve.

## Options considered

1. **Use the align module the way Ekos intends**: set the solver action to
   "slew to target" or "sync", and let Ekos centre the telescope. This is what
   every Ekos script does and it is the shortest path to a working plate solve.
   It also puts Ekos in charge of two mount motions the governor never sees, and
   the meridian band, the altitude floor and the Sun circle stop applying to
   them. Rejected on invariant 1.
2. **Set the mode to "nothing" once, at startup, and trust it.** Cheaper, and
   wrong for a reason the source states rather than a hypothetical: Ekos changes
   the mode itself. Its polar-alignment path calls `setSolverAction(GOTO_NOTHING)`
   and `loadAndSlew` sets `GOTO_SLEW`, so "the mode we set an hour ago" is not a
   fact about the current state.
3. **Solve in the mode where Ekos does nothing, read the answer, and decide.**

## Decision

**Option 3.**

`Executor.solve()` sets `setSolverAction(GOTO_NOTHING)` and *then* calls
`captureAndSolve()`, as one command, **every time**. In that mode
`executeGOTO()` does nothing at all, so solving is a read of the sky through the
camera. The answer comes back through `getSolutionResult()`, and what to do
about it is an ordinary governed command:

- a `SyncToCoordinates`, bounded by `limits.targeting.sync_tolerance_deg` — the
  tolerance ADR 0023 built, which is exactly the check Ekos' own sync does not
  make;
- or a `SlewToCoordinates`, bounded by the pointing rules.

Centring is therefore solve → sync → slew → repeat, assembled from pieces the
governor sees, rather than one Ekos operation it does not. The loop that repeats
it belongs to the session layer and is not built.

### What this cost, and what it bought

**Precession, in the safety package.** `precess_j2000_to_date` and its inverse,
the IAU 1976 series, checked against astropy over the whole sky and a
twenty-five-year sweep — worst disagreement 0.073 arcseconds. It is here because
the frame boundary is unavoidable once the align module is in use, and half a
conversion is worse than none. The executor converts in one place going each
way, and a round-trip test asserts the two are inverses.

**`loadAndSlew` is not called.** It is the one method on this interface whose
destination cannot be known before it acts, and the goto mode does not restrain
it. `FORBIDDEN_ALIGN_METHODS` carries it with that reason; the partition test
that ADR 0023 removed for the mount is back here, for one entry, because here
there is genuinely something to refuse. The capability it offers — re-frame on a
previous night's image — is available safely as: solve at the current position,
compare with the recorded coordinates, and slew to them through the governor.

**`SetAlignTarget` carries the pointing rules**, although nothing goes to that
coordinate while the goto mode holds. Two locks on one door, and the second
costs an arithmetic comparison: if the mode discipline ever regressed, the
target Ekos would slew to would still have been checked against the limits.

### What is asserted structurally rather than tested behaviourally

That the mode discipline holds is a claim about *every* call site, including
ones no test exercises, so it is checked over the source:
`GOTO_MODE_SLEW` and `GOTO_MODE_SYNC` appear nowhere outside the constant
definitions; `setSolverAction` precedes `captureAndSolve` in the one method that
starts a solve; there is exactly one such method; and no module outside the
bridge names a moving mode. Each of those has a positive control beside it.

## Consequences

- **The align module is driven, and solving moves nothing.** Twelve of its
  thirteen methods are called. The mount stub in the end-to-end suite records
  every call it receives, and after a solve that record is empty.
- **A solve is permitted where almost nothing else is** — uncalibrated, with no
  site, at any hour. That is deliberate and it matters: a rig that cannot solve
  until it is calibrated cannot *be* calibrated, because both the meridian
  procedure and the polar alignment want to know where the telescope points.
- **Two KStars defects are now recorded rather than remembered**, both of the
  kind no signature check could catch: the solution's element order and the
  meridian flip offset's units (ADR 0023). The pattern is worth naming — where
  this repository trusts the introspection XML, it should keep trusting it, and
  where the XML is silent about meaning, the source goes in the fixture.
- **What is not decided here:** when to solve, how many iterations to centre in,
  what counts as centred, and what to do when the solver fails. All of that is
  the session layer's, which is still not started. M2's acceptance criterion is
  a full simulated sequence, and this is one more primitive it will need rather
  than the sequence itself.
