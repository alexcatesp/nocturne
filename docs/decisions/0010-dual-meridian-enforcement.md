# 0010 — Meridian limits enforced in the governor *and* in the driver

Status: **Proposed** · 2026-08-06 · Milestone M2 (calibration lands there)
Evidence: docs/FIELD-NOTES-M1.md section 3.1 (measured on the reference rig).

> **Nothing in this ADR is implemented.** It is written now, while the evidence
> is fresh, so that the decision is made before the meridian calibration rather
> than during it. It needs the operator's agreement before any code is written,
> because it changes what happens on the night the two layers disagree.

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

## Decision (proposed)

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
(`MountLink`, ADR pending). Never at any other time. Not from the agent, not
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
| The driver refuses a command the governor approved | **Abort the session and park.** The governor believed this was safe and the mount disagreed. That is not a condition to retry through; it means the two models of the geometry have diverged and neither can be trusted until a human looks. |
| The governor refuses a command the driver would have allowed | Normal operation. The governor is stricter by design; this is the expected case and is logged at debug, not as a fault. |
| The driver's limits cannot be written at all | **Refuse to start `supervised` or `autonomous`.** `advisory` may continue with an explicit warning, because a human is watching. |

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

## Consequences

- Two enforcement layers, one truth, one direction of flow. The complexity is
  real and it is concentrated in one place — the bring-up sequence — rather than
  spread through the session.
- A new class of session abort exists: "the driver refused something the
  governor allowed". It should be rare to the point of never, and if it is not
  rare, the derivation is wrong and that is exactly what needs to be visible.
- Nocturne writes a file outside its own tree, `~/.indi/HorizonData.txt`. That
  requires adding the writing module to `MODULES_THAT_MAY_WRITE` in
  `backend/tests/unit/test_safety_boundaries.py`, which is a deliberate act with
  this ADR attached. It does **not** relax the unconditional rule that no module
  may write `config/*.yaml`.
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

## Open questions for the operator

1. Is "abort and park when the driver refuses something the governor allowed"
   the behaviour you want? It is the conservative reading. The alternative — log
   it, stop the current target, continue with the next — keeps more of the night
   but continues operating with two disagreeing models of where the tripod is.
2. Should the driver's limits also be written in `advisory` mode, where you are
   at the keyboard? Writing them is harmless; the argument for not doing so is
   that `advisory` is where you would be experimenting.
3. `HORIZONLIMITSONLIMITTRACK=On` with `SLEW=Off` and `GOTO=Off` is the driver's
   current state, which means it enforces against tracking only. Enabling all
   three is the defence-in-depth position. Confirm that is what you want before
   it is written.
