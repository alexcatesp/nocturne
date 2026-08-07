# 0012 — Which of the driver and the configuration is authoritative, per value

Status: Accepted · 2026-08-07 · Milestone M1
Evidence: docs/FIELD-NOTES-M1.md sections 10–12 (measured on the reference rig),
and `backend/tests/fixtures/hardware/devices-properties.txt`.

## Context

Bringing the four ZWO devices up produced four values where `equipment.yaml` and
the driver disagreed, and the right answer was different in each case:

| Value | Driver says | Configuration said | Who is right |
|---|---|---|---|
| `FILTER_NAME` | ZWO factory names | the wheel's real order | **configuration** |
| `CCD_CONTROLS.Gain` | 200 (533MM) | 100 (gain profile) | **configuration** |
| `FOCUS_MAX.FOCUS_MAX_VALUE` | 100000 | 60000 | **the driver** |
| `FOCUS_BACKLASH_STEPS` | 180 | 0 | **neither** |

"The driver is authoritative" and "configuration is authoritative" are both
wrong as blanket rules. Applied uniformly, either one produces a silent error:

- Trust the driver on filter names and B frames get filed as H-Alpha, because
  the wheel's stored slot 4 reads `H_Alpha` and holds B. Nothing in the image
  looks wrong.
- Trust the configuration on the focuser's travel limit and Nocturne believes it
  may only reach 60000 when the hardware reaches 100000 — or, had the numbers
  been the other way round, it would drive the focuser past its stop.

The distinction that decides it is **what kind of fact the value is**.

## Decision

Three categories, and each value belongs to exactly one.

### 1. Facts about the world outside the device — configuration wins, and writes

What filter is in slot 4 is a fact about the operator's filter wheel drawer, not
about the EFW. The driver has a place to store it and no way to know it; what is
in that place is whatever was last written, which on a new wheel is a factory
default that means nothing.

So Nocturne **writes** these on every connect and never reads them back into a
decision. A difference is not a conflict to reconcile — configuration wins and
overwrites. It is logged, because a wheel someone has physically rearranged
shows up here first and should leave a trace.

`FILTER_NAME` is the case that motivated this, and
`test_safety_boundaries.py` enforces that no module reads it.

### 2. Operating points — configuration wins, and writes

Gain, offset, slew ceiling. The driver's values are defaults chosen by the
manufacturer; ours are chosen for this rig, and in the imaging camera's case the
gain is the number SPEC section 15's exposure solver divides by. A driver
default of 200 against a configured profile of 100 makes every computed exposure
wrong by a stop, silently.

Same treatment as category 1: written on connect, re-applied on every
reconnection (see `nocturne/executor/link.py`).

### 3. Facts about the device itself — the driver wins, and configuration may only tighten

`FOCUS_MAX.FOCUS_MAX_VALUE` is the focuser's travel, which is a property of the
hardware. The driver reads it from the device; `equipment.yaml` cannot know it,
and the 60000 previously configured had no provenance at all.

So configuration does not *set* this. Unset means "the driver's value". A
configured value is accepted only if it is **tighter** than the driver's, as an
operator-imposed restriction — a shorter focuser travel because of a physical
obstruction, say.

**A looser value is refused at bring-up, not clamped.** Clamping would silently
grant travel the operator believed they had forbidden, and silently is the
problem. The refusal names both numbers.

### The fourth case: neither, so write nothing

`FOCUS_BACKLASH_STEPS` reads 180 on the EAF, with compensation disabled. That is
a ZWO default, not a measurement of this focuser. The 0 that `equipment.yaml`
previously carried was worse than the driver's 180, because it looked like a
measurement and was not.

`backlash_steps` is therefore **unset** until M2 measures it, and unset means
Nocturne writes nothing — the driver keeps what it has. Writing an unmeasured
number over an unmeasured number replaces one guess with another and produces a
config file that lies.

## Consequences

- Each new configuration value needs its category decided when it is added.
  That is a real cost, and it is smaller than the alternative: the categories
  differ by which failure they cause, and both failures are silent.
- `FILTER_NAME` is unreadable by construction, enforced structurally with a
  positive control (CLAUDE.md section 2). The one module allowed to name it —
  `nocturne.executor.instruments` — reads it only to log what it overwrote.
- The focuser's travel limit is not known until the device is connected, so
  anything that needs it must ask after bring-up rather than from configuration.
- **The M2 backlash procedure must distinguish the default from a measurement.**
  Whoever performs it will find 180 already in the driver, and 180 is exactly
  the sort of plausible number that gets recorded as a result. The procedure
  has to state that the starting value is a factory default and must be
  measured, not confirmed.
- Nothing here changes the mount. Its slew ceiling was already category 2, and
  the generalisation in `link.py` is what makes that visible rather than
  incidental.
