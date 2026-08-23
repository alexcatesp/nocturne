# 0019 — Nothing is pointed while the meridian limits are uncalibrated

Status: Accepted · 2026-08-22 · Milestone M2
Decided by the operator, 2026-08-22. Affects SPEC §9.1, §9.2, `docs/meridian-calibration.md`.

## Context

SPEC §9.1 requires that `supervised` and `autonomous` be refused while
`meridian.calibrated` is false. That has been enforced since M1. It says nothing
about `observer` and `advisory`, and the natural reading — the one
`docs/meridian-calibration.md` wrote down — is that the attended levels carry on
as normal:

> `observer` and `advisory` still work, so you can plan and image attended while
> you get round to this.

When the pointing rules were written, that reading turned into a question that
had to be answered before a line of the meridian rule could be typed: **with no
measured limits, what may an attended session point at?**

There is no partial answer available. The measured values are `null` until the
operator runs the daylight procedure. A rule written against absent data has
exactly two forms, and ADR 0007 §Options already named the problem: it either
permits everything, which is worse than no rule because it reads as protection,
or it refuses everything.

A third form was considered and offered to the operator: enforce a **provisional
exclusion zone** around the meridian — say ±40° of hour angle, from a named
configuration key — wide enough to be more restrictive than any limit this rig
is likely to measure, so that attended imaging away from the meridian could
continue. It is defensible. It is also a number nobody measured, enforced
against a tripod nobody looked at, and read by an operator as "the software is
watching the meridian for me".

## Options considered

1. **Refuse every pointing command while uncalibrated**, at every autonomy
   level. Costs the operator the ability to point under Nocturne's control until
   the daylight procedure is done — which is already an M2 HITL criterion
   (SPEC §14, M2) — and contradicts one sentence of the calibration document.
2. **Provisional exclusion zone from a new configuration key.** Keeps attended
   imaging available. Enforces an unmeasured number, and teaches the operator to
   trust it.
3. **Altitude and Sun only; the meridian ungated at `advisory`.** Leaves open
   exactly the collision the section exists to prevent, in the one mode where
   the operator is most likely to be running long sequences unattended in
   practice ("attended" is a category, not a promise about where the person is).

## Decision

**Option 1**, chosen by the operator.

`enforce_meridian_limits` refuses any command that names a place on the sky
while `meridian.calibrated` is false, with a message that names the procedure
and the file the numbers go in. Not a warning. Not a demotion-and-proceed. Not
conditional on the autonomy level.

What remains available while uncalibrated: everything that moves nothing —
connecting devices, cooling, filters, focus, reading every property — and
**parking**, which is the safe state and the abort action (SPEC §9.4). A rig
that cannot park is a rig that cannot be made safe, so the park path is
deliberately outside the pointing classification and is tested for it.

`docs/meridian-calibration.md` is corrected in the same change. A document that
promises a capability the code refuses is worse than one that says nothing.

## Consequences

- **The daylight calibration is now on the critical path for M2**, not a
  formality to be done before unattended operation. Until it is done and the
  numbers are in `config/safety.local.yaml`, Nocturne will not slew.
- The refusal is a hard failure with an explicit message rather than a silent
  no-op, so the first time it happens it explains itself. It is the message an
  operator reads at night on a terrace, so it names the document and the file.
- Test configurations must be calibrated to exercise anything downstream of
  pointing. `backend/tests/fixtures/observing.py` provides one, with limits that
  are visibly test values.
- The exclusion-zone option stays available if the operator later wants attended
  imaging before calibrating. It would be a new ADR, a new configuration key and
  a rule, and it would not weaken this one: it would replace refuse-everything
  with refuse-more-than-necessary. Nothing about this decision has to be undone
  to get there.
