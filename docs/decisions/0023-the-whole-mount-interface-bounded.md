# 0023 — The whole mount interface, each call bounded rather than refused

Status: Accepted · 2026-08-23 · Milestone M2
**Supersedes [ADR 0022](0022-the-ekos-mount-module-surface.md).**
Decided by the operator, 2026-08-23. Affects SPEC §1.2, §3, §4, §9.1, §9.2, §10.1.

## Context

ADR 0022 divided the Ekos mount module's nine methods into four Nocturne called
and five it refused by name: `sync`, `syncTarget`, `gotoTarget`, `resetModel` and
`setMeridianFlipValues`. The argument for refusing was that none of the five
names a destination, so none could be measured against the limits of SPEC §9.1
and §9.2, and a call the governor cannot measure is a call it should not permit.

The operator's instruction was that all five be implemented.

That instruction is reasonable and this ADR does not argue with it. Every one of
the five is needed for M2: plate-solve alignment *is* a sync, the startup
alignment is a named sync, "go to M 31" is what an operator asks for, a mount
whose model has gone wrong has to be resettable, and the meridian flip is on
M2's acceptance criteria. ADR 0022 deferred all of that to "the align module,
later" without saying what would make it safe then that was not safe now.

So the question this ADR had to answer is not *whether* but **what can be
measured about each of them** — because "cannot be measured against the pointing
limits" is not the same as "cannot be measured".

## Options considered

1. **Implement the five ungated**, registered with empty rule tuples like
   `ParkMount`. Simple, and it is what "implement them" could be read to mean.
   It would also put five commands through the governor that the governor has
   nothing to say about, which is the appearance of a gate without one — the
   failure mode CLAUDE.md §2 is written against.
2. **Implement them under the pointing rules**, since four of the five carry
   coordinates somewhere. Wrong for the syncs specifically: a sync reports where
   the instrument *is*, so refusing one because the answer is unwelcome leaves
   the governor measuring against a frame it already knows to be wrong. It would
   also make plate-solve recovery impossible in exactly the situation that needs
   it.
3. **Give each one a rule derived from what it actually endangers**, and add the
   configuration and the state each rule needs to be checkable.

## Decision

**Option 3.** `REQUIRED_MOUNT_METHODS` is now the whole recorded interface —
`test_ekos_recorded_interface.py` asserts equality, not containment, so a KStars
release that adds a method still fails the suite. What each of the five carries:

| Call | Rule | What it is protecting |
|---|---|---|
| `sync` | `SYNC_RULES` | The sync must be within `limits.targeting.sync_tolerance_deg` of where the mount says it is |
| `syncTarget` | `RESOLVED_SYNC_RULES` | The same, plus the resolution checks below |
| `gotoTarget` | `RESOLVED_POINTING_RULES` | The full pointing limits on the resolved coordinates, plus the resolution checks |
| `resetModel` | `MODEL_RULES` | The mount must be parked |
| `setMeridianFlipValues` | `FLIP_RULES` | Ekos must flip before the governor's keep-out band, never after |

### The state travels with the command

Three of those rules need something the governor does not have and must not
start holding: where the mount says it is, and whether it is parked. A governor
that read live state would stop being a function of what it was built with, and
SPEC §9's decision-that-can-be-replayed-from-its-log-line would go with it.

So the executor reads the state when it builds the command, and the command
carries it: `MountReport(ra_hours, dec_deg, parked)`. The governor validates the
pair. The decision remains reproducible, and what it was reproduced *from* is in
the command that was logged.

The same shape solves the by-name calls. `ResolvedTarget` carries the
coordinates KStars resolved, the type of object, and **KStars' own clock at the
moment it resolved** — so a name becomes an ordinary pointing decision before
the governor ever sees it.

### Three findings that changed the design

**`setMeridianFlipValues`' argument is misnamed in KStars' own DBus interface.**
The adaptor XML declares `<arg name="hours" type="d">`. It is degrees:
`mount.cpp` names the parameter `degrees`, writes it into
`meridianFlipOffsetDegrees`, and `meridianflipstate.cpp` divides by fifteen when
it uses it. This repository trusts the recorded introspection XML almost
everywhere and by design; this is the one place that trust would have been
misplaced, and by a factor of fifteen in the direction that flips too late. The
excerpts are in `kstars-mount-dbus-units.txt` and asserted in CI, and Nocturne's
field is named `offset_deg` so the unit is in the name.

**The flip offset this rig needs is negative.** Under ADR 0020 the keep-out band
brackets the meridian, so the tube enters the forbidden region *before*
culmination — at −30° of hour angle with the shipped margin. Ekos is configured
in "degrees past the meridian" and every example of it in the wild is a small
positive number, which on this mount would have Ekos still waiting while the
tube was inside the band. `flip_before_the_keep_out_band` computes the required
value from the enforced band rather than trusting a number typed in.

**KStars resolves names against its own simulated clock**, and reports it only
as a Julian date printed to three decimal places. A stopped clock produces
coordinates that look entirely normal and are precessed to the wrong instant, so
`refuse_a_stale_resolution` compares it with the governor's. The three-decimal
formatting quantises that clock to 86.4 seconds, which is why
`resolver_max_clock_skew_s` has a floor of 120 in the schema and ships at 300: a
check that fires when nothing is wrong is a check that gets relaxed until it is
not a check.

### What is still refused, and it is not a method

`MOVING_OBJECT_TYPES`. A name may resolve to a planet, and a position validated
now for a body that moves is a position validated for an instant that has passed
by the time the mount arrives. SPEC §1.2 puts planetary and lunar work out of
scope outright, so this is not a gap to be filled later — it is that scope,
enforced at the one place a name could otherwise smuggle it back in.

## Consequences

- **Nine calls, nine rules or a reasoned absence.** `park`, `unpark` and `abort`
  still carry no rules, for the reasons ADR 0022 gave and this ADR keeps. The
  other six all carry something. No command is registered with an empty rule
  tuple and no entry in the reasoned-absence list.
- **Two new configuration values**, both in `limits.targeting`, both with the
  reasoning in the shipped file rather than only here.
- **A sync is permitted where the identical slew is refused**, deliberately, and
  there is a test that asserts exactly that pair. It is the most surprising
  consequence of the decision and the one most likely to be "fixed" by someone
  who has not read this.
- **A bug was found in the bridge doing this.** `_snake()` inserted an
  underscore before every capital, where dbus-next keeps runs of capitals
  together — so `getObjectDataXML` resolved to an accessor that did not exist
  and the bridge reported the method missing from a KStars that has it. Every
  member the bridge had called until now happened to have no capital run. The
  conversion is now checked against dbus-next's own over every member name in
  the recording.
- **ADR 0022 is superseded, not deleted.** Its argument for refusing is the
  argument each rule here had to answer, and reading the two in order is how the
  rules stop looking arbitrary.
- **Not decided here:** when to sync, when to flip, or what to do about a target
  that tracking carries into the band. Those still belong to the session layer,
  which is still not built. What has changed is that the calls it will need now
  exist and are bounded.
