# 0017 — A defocused-star collimation assistant that measures, while the operator turns the screws

Status: Accepted — implement in M4 · 2026-08-18 · Milestone M4
Requested by the operator. Affects SPEC sections 1.2, 5.1, 8.2, 9.6, 10.1, 10.4, 11, 14, 15.

## Context

SPEC.md was silent on collimation. It is not a small silence: the OTA is a
Sky-Watcher 200PDS (SPEC §2.1), a Newtonian on a portable strain-wave mount that
is carried out, assembled and pointed somewhere new every night. Its primary
moves in its cell as the tube tips. Collimation is the one optical property of
this rig that degrades on its own, between sessions and during them.

The operator, who does this by eye today, asked for it and placed it: **it is
among the first things done before a session**, ahead of polar alignment. That
placement is the requirement. A collimation tool that lives in a settings screen
and takes a minute to answer would not be used; one that shows a number that
moves while a screw is being turned replaces a judgement of a shape with a
measurement.

Nothing about this is safety-critical in the §9 sense — no limit is being
enforced — but it introduces something no other feature does: **a state in which
a person is standing at the telescope with both hands on it.** Every other part
of Nocturne assumes the instrument is alone.

## Options considered

1. **Drive whatever KStars already has.** KStars is reported to ship collimation
   overlays in its live view, and the Focus module is reported to carry an
   "Aberration Inspector". SPEC §1.2 says Nocturne orchestrates Ekos rather than
   reimplementing it, so this is the default answer and it deserves to win if it
   works. It cannot be chosen today: **neither has been introspected**, and
   docs/FIELD-NOTES-M1.md §26 is explicit that the Ekos module objects do not
   exist until Ekos is started and none has been examined live. Choosing it now
   would repeat exactly the mistake of field notes §21 — a bridge written against
   a guessed interface, with a stub that shared the guess, and a green suite.
2. **The in-focus diffraction star test.** Concentric Airy rings, examined at
   high magnification. It is the most sensitive test there is, and it needs
   seeing this site does not reliably have; at f/5 on a Pi it would spend most
   nights reporting turbulence.
3. **Field-wide star elongation on ordinary subs.** Free — the M3 pipeline
   already detects stars and measures eccentricity — continuous, and needs no
   observing time at all. It also cannot separate a displaced coma-free point
   from sensor tilt, and a metric that cannot say which of two causes it saw gets
   read as though it could.
4. **A defocused-star donut assistant, closing its loop through the operator.**

## Decision

Option 4, specified in SPEC §10.4, with option 1 kept as an explicit
precondition: **introspect the KStars/Ekos collimation aids before implementing,
and record what is found in the field notes.** If Ekos exposes a usable ROI and
fast-readout path over DBus, §10.4.2 drives it rather than writing camera
properties directly.

The measurement is the normalised decentre of the secondary's shadow within the
defocused disc, `|c_inner − c_outer| / (R_outer − R_inner)`. Normalising by the
annulus width is the substantive part of the choice: a raw pixel offset varies
with how far the operator happened to defocus, so it moves when the telescope has
not, and it would have to be recalibrated at the OAG and MPCC migrations. The
normalised value is very nearly invariant to defocus, binning and plate scale, so
the thresholds in `equipment.yaml` are dimensionless and survive both.

Three consequences of the decision are load-bearing enough to be part of it:

- **The result type is `Measured | NoMeasurement(reason)`, with no path from the
  second to `error = 0`.** A collimated telescope and a broken detector produce
  the same near-zero number. This is CLAUDE.md §2's failure mode in its worst
  form, because here "found nothing" is also the outcome the operator is hoping
  for, so it would never be questioned. The tests are positive controls —
  synthetic donuts with injected decentre — and the concentric case is asserted
  only in the same suite as the injected ones.
- **`COLLIMATE` freezes the mount** (SPEC §9.6). While in the state the governor
  refuses every mount command except sidereal tracking, from every source. The
  operator's hands are on the tube.
- **The screw mapping is measured and stored in SQLite, not derived and not
  written to config.** Which screw an on-screen arrow corresponds to depends on
  camera rotation, focuser clocking, the mirror cell and where the operator is
  standing; modelling all four means being silently wrong about one. And nothing
  in Nocturne writes configuration (ADR 0013), so a measured mapping could not
  live there even if it were derivable.

## Consequences

- **`SetProperty` gating (issue #1, ADR 0007) becomes a prerequisite, not a
  backlog item.** The assistant needs camera ROI and focuser moves; if those go
  through direct INDI they go through the ungated path. A feature that exists so
  a person can stand next to the telescope must not be what reopens an unchecked
  route to the mount. Its writes are restricted to an allowlist of camera and
  focuser properties containing no mount property at all.
- Work splits across two milestones: the measurement core is pure array
  arithmetic and lands in **M3** with the rest of the frame analysis, tested
  against synthetic donuts; the loop, the transport, the state and the view land
  in **M4**, where there is a UI to hold them. Neither starts before M2 does.
- `equipment.yaml` gains a `collimation` block and `optical_trains[].central_obstruction_mm`
  (SPEC §5.1). **Neither is in the shipped file yet.** The schema is
  `extra="forbid"`, so the keys and their Pydantic models land together with the
  implementation; adding the YAML now would fail validation at startup, and
  adding placeholder numbers now would invent measurements the way the focuser
  values criticised in field notes §12 did.
- The state machine gains an optional `COLLIMATE` state and the API gains four
  endpoints and a WebSocket. The agent gains one read-only tool,
  `get_collimation_status`, and no action tool.
- Night mode forces a UI consequence worth recording: with red on black there is
  no green to signal "good", so the verdict is carried by a bar against threshold
  marks and a translated word, never by colour.
- Field-wide coma and tilt analysis (option 3) is **deferred, not rejected** —
  SPEC §15. It costs no observing time and would run passively on every sub; the
  reason to wait is that separating collimation from tilt needs real data from
  M3 onward to be worth attempting.

## What would reverse this

Introspection showing that Ekos exposes a complete collimation assistant over
DBus. In that case §10.4 keeps its safety rules, its record and its view, and
delegates the measurement — SPEC §1.2 is not negotiable on this point.
