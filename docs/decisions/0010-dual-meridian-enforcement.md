# 0010 — Meridian limits enforced in the governor *and* in the driver

Status: **Accepted, not yet implemented** · 2026-08-06 · Milestone M2
Evidence: docs/FIELD-NOTES-M1.md section 3.1 (measured on the reference rig).
Approved by the operator 2026-08-06, with two additions in "Disagreement
handling" below: a divergence also invalidates the calibration, and that
invalidation must survive an orchestrator restart.

> **Nothing in this ADR is implemented, and nothing should be until M2 brings
> the numeric limits.** Building the second enforcement layer before the first
> one exists would mean deriving a backstop from limits that are still null.

## Context

SPEC section 9.1 is the highest-severity risk in the project: without a tripod
extension the 200PDS strikes the tripod legs near the meridian. Section 9.1
paragraph 5 is unambiguous about where the limits live:

> Limits are enforced **in the governor**, not in Ekos and never in the agent.

The M1 property dump shows that `indi_eqmod` has a horizon-limit subsystem of
its own, sitting *below* anything Nocturne writes:

```
HORIZONLIMITSDATAFILE.HORIZONLIMITSFILENAME   = ~/.indi/HorizonData.txt
HORIZONLIMITSMANAGE.*                         add / delete / clear points
HORIZONLIMITSFILEOPERATION.*                  read / write the file
HORIZONLIMITSONLIMIT.HORIZONLIMITSONLIMITTRACK   = On
HORIZONLIMITSONLIMIT.HORIZONLIMITSONLIMITSLEW    = Off
HORIZONLIMITSONLIMIT.HORIZONLIMITSONLIMITGOTO    = Off
HORIZONLIMITSLIMITGOTO.HORIZONLIMITSLIMITGOTOENABLE = On
```

It holds a list of alt/az points, persists them to a file, and enforces them
against both goto and tracking — in the driver process, without asking anybody.

That is a second layer of defence against the one failure that breaks glass, and
it is already there. The question is whether Nocturne should populate it.

The reason this needs a decision rather than an obvious yes: **two enforcers with
one truth is a synchronisation problem**, and a synchronisation problem that gets
it wrong at three in the morning is worse than the single layer it replaced. A
stale `HorizonData.txt` from a previous configuration — a different tripod, the
counterweight fitted, an OTA change — would be enforcing yesterday's geometry
while the governor enforces today's.

There is also a units mismatch. `safety.yaml` states meridian limits as **hour
angle in degrees**, east and west, because that is what the SPEC section 9.1
procedure measures. The driver's file is **alt/az points**. The conversion
depends on the site latitude and on the declination being observed, so one hour
angle limit does not map to one horizon point; it maps to a curve.

## Options considered

1. **Governor only.** What SPEC section 9.1 requires and what M2 will build
   regardless. Single source of truth, no synchronisation problem, and no
   defence at all if Nocturne is not the thing issuing the command — a slew
   started from the KStars GUI, from `indi_setprop`, or by Ekos on its own
   initiative during a meridian flip is not seen by the governor.
2. **Driver only.** Moves enforcement below Nocturne entirely. Contradicts SPEC
   section 9.1 paragraph 5, puts the limit in a file the agent's process could
   in principle write, and gives no useful rejection message — the mount simply
   stops, and the session layer has to infer why.
3. **Both, driver written from `safety.yaml` at session start.** Defence in
   depth. Costs a conversion, a sync step, and an answer to "what if they
   disagree".
4. **Both, but written once by the operator during calibration.** Cheapest, and
   wrong: it is exactly the arrangement that produces a stale file, because
   nothing re-derives it when `safety.yaml` changes.

## Decision

**Option 3.** The governor remains authoritative and the driver's horizon limits
become a derived, re-generated backstop.

Concretely:

### Direction of truth

`config/safety.yaml` is the only source. The driver's limits are **derived** and
are never read back into Nocturne's decision-making. Nothing in the governor
consults `HorizonData.txt`, and no operator edit to that file changes what
Nocturne permits. If the two ever disagree, `safety.yaml` is right by
definition — the question is only what to *do* about the disagreement.

### When they are written

At the start of every session, after the mount connects and before the first
slew, in the same bring-up sequence that applies the slew-rate ceiling
(`MountLink`, `backend/nocturne/executor/mount.py`). Never at any other time. Not from the agent, not
from the API, not from a running session.

### Derivation

For each declination in the range the session will use, the two hour-angle
limits from `safety.yaml` (already reduced by `safety_margin_deg`) are converted
to alt/az at the configured site latitude, and the resulting boundary is written
as a point list via `HORIZONLIMITSMANAGE` / `HORIZONLIMITSFILEOPERATION`. The
sampling interval is a configuration value, not a constant.

The derived limits are **at least as restrictive** as the governor's, never
less. Where rounding or sampling makes a point ambiguous, the more restrictive
point is written. A backstop that is looser than the thing it backs up is not a
backstop.

### Disagreement handling

Three distinct cases, three distinct responses:

| Case | Response |
|---|---|
| The driver refuses a command the governor approved | **Abort the session, park, invalidate the calibration, alarm.** See below. |
| The governor refuses a command the driver would have allowed | Normal operation. The governor is stricter by design; this is the expected case and is logged at debug, not as a fault. |
| The driver's limits cannot be written at all | **Refuse to start `supervised` or `autonomous`.** `advisory` may continue with an explicit warning, because a human is watching. |

The first row is the one that needed a decision, and it has three parts, all
required:

1. **Park.** The governor believed the command was safe and the mount
   disagreed. That is not a condition to retry through: the two models of where
   the tripod is have diverged, and neither can be trusted until a person
   looks. `on_abort.action` already covers stopping, parking and warming the
   TEC on its ramp (SPEC section 9.4).

2. **Set `meridian.calibrated` back to `false`.** This is the operator's
   addition, and it follows directly from SPEC section 9.1 paragraph 7, which
   already invalidates the limits when `counterweight_fitted` changes. The
   reasoning is the same and the evidence is stronger: a disagreement between
   two independently derived enforcement layers means *something about the
   geometry moved and nobody knows what*. Once `calibrated` is false,
   `supervised` and `autonomous` are refused with a hard failure until the
   operator recalibrates (CLAUDE.md invariant 3). The night ends; the next one
   does not start on an unexamined assumption.

   Note the consequence for the write path. `safety.yaml` is the one file
   nothing may write — that is CLAUDE.md invariant 2, enforced unconditionally
   in `test_safety_boundaries.py`, and this ADR does not weaken it. The
   invalidation is therefore recorded as **state the governor consults
   alongside the file**: a flag that forces `require_autonomy_level()` to
   refuse regardless of what `safety.yaml` says, held outside the
   configuration, and cleared only by the operator editing `safety.yaml` by
   hand as part of recalibrating. The governor takes the *more* restrictive of
   the two. It can never take the less restrictive one, and that direction must
   be tested.

   **The flag must survive an orchestrator restart.** An in-memory flag is not
   enough, and the failure mode is specific: the process dies after a
   divergence, systemd restarts it, the flag is gone, and `autonomous` is
   silently available again — at the one moment in the project's life when it
   is least safe. A crash is not evidence that the geometry is fine. It is
   arguably evidence of the opposite.

   So it is persisted, the governor loads it **at startup, before it will
   accept any autonomy level at all**, and the restart path gets its own test:
   induce a divergence, kill the process, start it again, assert `supervised`
   and `autonomous` are still refused. Not a test of the flag's setter — a test
   of the process boundary, because the process boundary is what fails.

   Clearing it stays a deliberate human act tied to recalibration. Nothing
   automatic clears it: not a successful slew, not a clean shutdown, not a new
   session, not time passing.

3. **Notify at severity `alarm`.** SPEC section 5.2 already has
   `on_abort.notify_severity`. This is the case the operator has said is worth
   waking them for.

### The stale-file failure mode

This is the failure this ADR exists to prevent, and it must be handled
explicitly rather than by hoping the write always succeeds.

`~/.indi/HorizonData.txt` persists across sessions, across reboots, and across
changes to `safety.yaml` that nothing propagates. A file written when
`counterweight_fitted: false` is wrong the moment the counterweight goes on —
and SPEC section 9.1 paragraph 7 already says that change invalidates the
limits.

Proposed handling:

- Nocturne writes a **provenance line** into the generated file: the SHA-256 of
  the `meridian` block of `safety.yaml`, the `calibration_date`, the
  `counterweight_fitted` flag, and the time of generation.
- At bring-up, before writing, Nocturne loads the file and compares that hash
  against the current configuration. A mismatch is expected and is simply
  overwritten — that is the mechanism working.
- **A file the driver reports as loaded, whose provenance line is absent or
  unparseable, is treated as hostile, not as absent.** It was written by
  something that is not this version of Nocturne — an older release, the KStars
  GUI, a hand edit — and its contents are unknown. The limits are cleared with
  `HORIZONLIMITSLISTCLEAR` and rewritten from scratch. Clearing is not optional:
  a driver holding both yesterday's points and today's would enforce their
  union.
- **If clearing or writing fails, the session does not start** in `supervised`
  or `autonomous`. Not a warning. The whole point of the second layer is that it
  is trustworthy; a second layer in an unknown state is worse than none, because
  it invites the operator to rely on it.
- The provenance line, the hash and the file's modification time are reported in
  the startup summary, so "which limits is the driver actually holding" is a
  question with a visible answer rather than an inference.

### What is deliberately *not* proposed

- Reading the driver's limits back as an input to the governor. That would make
  a file the agent's process can reach into a safety input.
- Writing horizon limits during a session, including after a meridian flip.
- Any code path that continues in `supervised` or `autonomous` when the driver's
  state is unknown.

### Where the flag lives — an ordering problem to settle

The operator's instruction is to persist it in SQLite. SPEC section 4 already
fixes SQLite (WAL) as the persistence technology, so that is the right
destination and there is no argument about it.

The collision is scheduling: **SQLite persistence is an M3 deliverable** (SPEC
section 14, M3 — "Frame analysis pipeline, SQLite persistence, calibration
library"), while this ADR is to be implemented in M2. Written naively, M2 would
need a store that does not exist yet.

Three ways out, and this is flagged rather than decided quietly because it
changes M2's scope:

1. **Bring forward a minimal store into M2** — one SQLite file, one table, one
   row, created and read by the governor, which M3's store later extends rather
   than replaces. **Recommended.** It introduces no technology SPEC does not
   already require, it puts the flag in its final home immediately, and the
   whole of it is smaller than the migration that option 2 eventually needs.
2. **A dedicated state file in M2, migrated to SQLite in M3.** Avoids touching
   M3's design early, at the cost of writing a second persistence format and
   then a migration for one boolean. More total work, and a migration is
   exactly the sort of thing that gets deferred and then forgotten.
3. **Defer the persistence to M3, and in M2 refuse `supervised` and
   `autonomous` for the remainder of the process only.** Rejected: it is the
   hole this section exists to close, and shipping it knowingly for a milestone
   is worse than not having claimed the protection at all.

Option 1 unless the operator says otherwise. Either way, **the restart test is
not optional and does not wait for M3** — whatever holds the flag in M2 must
pass it.

## Consequences

- Two enforcement layers, one truth, one direction of flow. The complexity is
  real and it is concentrated in one place — the bring-up sequence — rather than
  spread through the session.
- A new class of session abort exists: "the driver refused something the
  governor allowed". It should be rare to the point of never, and if it is not
  rare, the derivation is wrong and that is exactly what needs to be visible.
- **That abort costs the following nights, not just the current one.** With the
  calibration invalidated, nothing unattended runs until the operator repeats
  the SPEC section 9.1 procedure. That is the intended price: the alternative
  is continuing to point a 200PDS near a tripod on the strength of a model that
  has just been contradicted.
- Nocturne writes a file outside its own tree, `~/.indi/HorizonData.txt`. That
  requires adding the writing module to `MODULES_THAT_MAY_WRITE` in
  `backend/tests/unit/test_safety_boundaries.py`, which is a deliberate act with
  this ADR attached. It does **not** relax the unconditional rule that no module
  may write `config/*.yaml`.
- The governor gains a dependency on persistent state, which it does not have
  today: at M1 it is a pure function of frozen configuration. That is a real
  loss of simplicity in the most important class in the repository, and it is
  the price of the invalidation surviving a restart. It should be the only such
  dependency, and it should be read once at startup rather than consulted per
  command.
- The conversion from hour angle to alt/az is new arithmetic in a safety path.
  It needs property-based tests (CLAUDE.md section 2): for every declination and
  every hour angle, the derived boundary must be at least as restrictive as the
  governor's.
- This cannot be tested end to end against `indi_simulator_telescope`, which has
  no horizon-limit subsystem. The write path can be tested against the recorded
  `EQMod Mount` fixture; that the driver *acts* on what was written is an HITL
  step, and it involves commanding motion toward a limit with the OTA fitted.
  That is a night's work with the operator present, and it is a prerequisite of
  M6, not of M2.

## Answered

1. ~~Is "abort and park" the behaviour you want?~~ **Yes**, and with the
   calibration invalidated and an `alarm` notification alongside it. Recorded
   above. (Operator, 2026-08-06.)

## Still open, to settle before this is implemented

1. Should the driver's limits also be written in `advisory` mode, where the
   operator is at the keyboard?

   **Operator's preliminary view, 2026-08-06: yes, write them in advisory too.**
   The driver's enforcement is not the agent's authority, so there is no reason
   to tie it to the autonomy level; and two layers that agree at all times are
   simpler to reason about than two layers that agree only sometimes. To be
   confirmed in M2 with the numbers in hand — the thing that could change it is
   if writing the limits turns out to interfere with the calibration procedure
   itself, which is performed in `advisory`.
2. `HORIZONLIMITSONLIMITTRACK=On` with `SLEW=Off` and `GOTO=Off` is the
   driver's current state, so it enforces against tracking only. Enabling all
   three is the defence-in-depth position, and it is what this ADR assumes.
   Confirm before it is written.

Neither blocks anything today: implementation is M2 work and both questions are
answerable then.
