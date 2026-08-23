# 0022 — Which of the Ekos mount module's calls Nocturne makes, and which it refuses

Status: **Superseded by [ADR 0023](0023-the-whole-mount-interface-bounded.md)**,
2026-08-23 · Milestone M2
Affects SPEC §3, §4, §9.1, §9.2, §10.1. Extends ADR 0001, 0019 and 0020.

> **Superseded the same day, by the operator's decision.** The five methods this
> ADR refused are implemented, each with a rule of its own rather than a
> refusal — ADR 0023 has the rules and what had to be built to make them
> checkable. This document is kept because its argument is the one each of those
> rules had to answer: read in order, the two explain why the rules are what they
> are rather than looking arbitrary. What survives unchanged is the reasoning for
> `park`, `unpark` and `abort` carrying no rules at all.

## Context

`/KStars/Ekos/Mount` is the first Ekos module Nocturne drives. It exports nine
methods, recorded in `backend/tests/fixtures/hardware/kstars-ekos-running.xml`
and, for the units and the frame, in `kstars-mount-dbus-units.txt`:

| Method | What it does |
|---|---|
| `slew(RA, DEC)` | Goto an equatorial position, RA in hours, JNow |
| `sync(RA, DEC)` | Move the mount's *model* of where it is to that position |
| `gotoTarget(name)` | Goto a target KStars resolves by name |
| `syncTarget(name)` | Sync to a target KStars resolves by name |
| `park()` | Drive to the park position |
| `unpark()` | Release from the park position |
| `abort()` | Stop the mount moving |
| `resetModel()` | Discard the mount's alignment model |
| `setMeridianFlipValues(on, hours)` | Configure Ekos' own automatic meridian flip |

The safety governor validates *commands*, and it can only validate what it can
read. A command that names a place on the sky is checked against the altitude
window, the Sun and the meridian band (ADR 0019, ADR 0020). A command that
changes where the mount *believes* it is changes the frame all three of those
are measured in, and is not the same kind of thing at all. A command that names
a place by a string KStars will resolve privately gives the governor nothing to
check.

Nine methods, therefore, needing three different answers — and the failure mode
if this is decided implicitly is a bridge that grows a convenience wrapper for
`gotoTarget` one evening because it is shorter to call.

## Options considered

1. **Wrap the whole interface and gate each call.** Symmetrical, and complete
   from day one. But three of the nine have no defensible gate: the governor
   cannot check a target name, and it cannot check a sync at all without knowing
   where the mount currently is, which it deliberately does not (it is a pure
   function of configuration, site, clock and command).
2. **Wrap only `slew` and gate it; leave the rest for later.** Leaves the rig
   unable to park or stop, which contradicts SPEC §8.5 — every degradation path
   ends at parking — and would make the safe state unreachable through the one
   door the executor offers.
3. **Wrap the four that Nocturne can justify, and refuse the other five by
   name**, with the reason for each recorded next to the list.

## Decision

**Option 3.**

`nocturne.executor.ekos` calls exactly four methods —
`REQUIRED_MOUNT_METHODS = {slew, park, unpark, abort}` — and names the other
five in `FORBIDDEN_MOUNT_METHODS` with a one-line reason each.
`test_ekos_recorded_interface.py` asserts that the two sets partition the
interface: **a method in neither fails the suite**, so a KStars release that adds
one forces a decision rather than passing unnoticed.

### The four that are called

- **`slew`** carries the full pointing rules, through the `SlewToCoordinates`
  command. It is decided identically to the equivalent `EQUATORIAL_EOD_COORD`
  property write, and that equivalence is asserted directly rather than by
  re-testing each limit: a new command type that could reach the instrument
  where the old one could not would be the second door CLAUDE.md invariant 1
  forbids.
- **`park`** and **`abort`** carry **no** rules. This is the substantive part of
  this ADR. Parking is the safe state SPEC §8.5 degrades to, and aborting is
  what an operator or a watchdog reaches for when something is wrong. A rule
  able to refuse either would put the safe state out of reach at exactly the
  moment it is wanted. ADR 0019 already carved parking out of the pointing
  classification for the same reason; this extends it to the Ekos path and to
  abort.
- **`unpark`** carries no rules either, and this one is a genuine judgement
  rather than an obvious call. Unparking names no destination: it releases the
  mount where it already stands, at a position the operator chose as a safe
  resting place. What it does *enable* is tracking, and a target that tracking
  carries into the meridian band forty minutes later is not something a
  destination check would have caught anyway — that is the continuous watch ADR
  0019 records as belonging to the session layer and not built.
  The alternative considered was to validate an unpark against the mount's
  currently reported coordinates. It was rejected here because it would make the
  governor a function of live telemetry, which is a different object from the
  one specified in SPEC §9: reproducible from its log line, holding no state
  that can change under a running session.

### The five that are refused

- **`gotoTarget`** and **`syncTarget`** take a name KStars resolves internally.
  The governor never sees coordinates, so it cannot check any. Nocturne resolves
  its own targets and slews by coordinate; the catalogue layer that does the
  resolving is a later milestone and does not change this.
- **`sync`** moves the mount's model of where it is. Every limit in §9.1 and
  §9.2 is expressed in that frame, so a sync is a change to the measuring
  instrument rather than to the measurement. Plate-solve syncs are how alignment
  actually works, and they arrive with the align module, where the solved
  position and the mount's reported position are both in hand and a tolerance
  can be enforced between them. Until then this matches what the INDI path
  already does: `SYNCMANAGE`, `STANDARDSYNCPOINT` and their siblings are in
  `UNVALIDATABLE_MOTION_PROPERTIES` and are refused.
- **`resetModel`** discards the alignment model outright, which is the same
  objection at larger scale.
- **`setMeridianFlipValues`** would make Ekos a third actor deciding when to
  flip, beside the governor and the driver-level limits of ADR 0010 — two of
  which do not yet agree on paper, let alone three. The flip belongs to the
  session layer, in the milestone that builds it.

## Consequences

- **The mount can be commanded, and only four ways.** `Executor.slew`, `.park`,
  `.unpark` and `.abort_motion` are the whole surface, each built on a command
  object the governor sees before anything reaches DBus.
- **A refused slew reaches nothing.** `test_mount_commands_end_to_end.py` puts a
  real bridge on a real session bus and asserts, after each refusal, that the
  stub on the far end recorded no call at all — the difference between a gate
  and a log line.
- **The units are recorded, not assumed.** The introspection XML says `slew`
  takes two doubles and stops there. Hours versus degrees is a factor of fifteen
  between the target and somewhere else, and no signature check would ever catch
  it, so the doxygen comments from the pinned KStars commit are checked in
  alongside the XML and asserted against the bridge's own documentation.
- **Attaching a module is a wait, not a lookup.** The module objects do not exist
  until `Ekos.start` has run — five exported paths before, twenty-four after, in
  the same process — so `attach_modules()` is separate from `connect()`, polls,
  and has a timeout from `EkosSettings` rather than a number typed into a loop.
  A KStars restart drops the handle: a proxy to an object in a dead process is
  worse than none, because it looks attached.
- **What is not decided here.** Nothing about *when* to slew, when to flip, or
  what to do when a target drifts into the band while tracking. Those are the
  session layer's, and this ADR deliberately does not anticipate them.
- **Reversible in one direction only.** Adding a method to
  `REQUIRED_MOUNT_METHODS` requires a command type, a rule decision and a row in
  the registry; the partition test makes that a wall rather than a slope. Moving
  one out of `FORBIDDEN_MOUNT_METHODS` is a new ADR.
