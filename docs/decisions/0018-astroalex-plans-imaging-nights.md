# 0018 — Astroalex plans imaging nights; Nocturne executes them

Status: Accepted · 2026-11-10 · Decided by the operator after M1, before M2
Affects SPEC sections 8.1, 11.4 and 10.3, and how Addendum A §A.1 is consumed
outside this repository. No section is amended by this ADR. Each amendment
lands with the milestone that builds it.

## Context

The operator also maintains **Astroalex** (`alexcatesp/astroalex`), a desktop
application (FastAPI + Next.js) that guides a night from planning to the final
image. It provides weather and ephemerides, target recommendation with
visibility curves, a test-frame exposure analysis, and acquisition plans
exported to N.I.N.A. and ASIAIR. Its roadmap (`docs/ROADMAP.md` there,
2026-11-10) moves its post-processing onto Siril, GraXpert and StarNet. It is
used by people who do not run Nocturne.

The two projects overlap:

| Concern | Nocturne | Astroalex |
|---|---|---|
| Initial plan for an imaging night | Agent (§8.1); Plan view (§11.4) | Steps 1–3, implemented |
| Planning a photometry series | Agent, from event ephemerides (Addendum A §A.7.6) | Not covered |
| Sensor characterisation | Full photon transfer curve, ≥ 12 levels, linearity, dark current, with σ (Addendum A §A.1) | Two bias and two flats, no uncertainties |
| Optimal exposure | Live, per frame (§8.1) | One test frame |
| Post-processing | Excluded (§1.2) | Its roadmap phases 5–6 |

Post-processing stays off the Pi. The operator confirmed the reason on
2026-11-10: storage. Stacking alone is why NVMe is required (§2.2), and
post-processing writes and computes more than stacking does.

## Options considered

1. **Merge both into one application.** Rejected. It would bring a desktop,
   multi-rig application under this repository's safety-critical process, and
   contradict §1.2.
2. **Nocturne plans everything; Astroalex only processes.** Leaves two
   planners in the wild, because Astroalex must still plan for N.I.N.A. and
   ASIAIR users.
3. **Both plan imaging nights; the operator chooses.** Two planners to maintain
   and keep consistent.
4. **Astroalex plans imaging nights before they start; Nocturne executes and
   reallocates during them.**

## Decision

Option 4, for **imaging sessions only**.

- **The initial plan of an imaging night comes from Astroalex.** It lists
  targets, filters, integration goals and priorities. Nocturne receives it
  through `POST /api/v1/plan` as a proposal like any other. The proposal passes
  `safety.validate()`, and the operator approves it per the current autonomy
  level. Nocturne never depends on Astroalex at run time. The plan is a file
  delivered before the night, so a missing Astroalex cannot degrade a session.
- **The agent keeps in-session judgement.** Reordering under changing
  conditions, skipping a target, declaring a target done and ending the session
  stay with the agent (§8.1). Building the initial imaging plan does not.
- **Without an imported plan, the operator picks targets by hand** in a minimal
  Plan view (catalogue search, visibility, framing). The view is not a second
  planner. This is the conservative fallback and is flagged for the operator.
- **Photometry sessions are unchanged.** Addendum A §A.7 keeps their planner
  and the agent's target selection from event ephemerides. Astroalex has no
  equivalent and does not get one through this ADR.
- **Sensor characterisation flows from Nocturne to Astroalex, not the other
  way.** Addendum A §A.1 is far stricter than Astroalex's step 2.
  `config/sensor-profile.yaml` becomes something Astroalex reads. Astroalex
  uses it instead of asking for frames whenever it is available. Nothing from
  Astroalex's characteriser is ported here.
- **Exposure stays live in Nocturne.** The per-frame solver (§8.1) supersedes
  Astroalex's single test frame whenever Nocturne is the capture system.
- **Hand-off back to Astroalex.** The §10.3 output is unchanged: calibrated,
  registered, stacked 32-bit FITS per variant. It is exported with the
  per-frame records (§7.2, with the uncertainty fields of Addendum A §A.8.3)
  and each stack's manifest (§A.8.2). Astroalex post-processes on the PC and
  does not re-judge frames Nocturne already judged.

## Consequences

- The imaging plan file format must be defined before M4. **Nocturne owns its
  schema** (JSON Schema in this repository), because Nocturne is the consumer
  that must validate it. Astroalex generates files against it.
- `config/sensor-profile.yaml`, the §7.2 record and the stack manifest become
  contracts read by a second program. Each needs a version field. Changing one
  needs a note here.
- SPEC amendments, per milestone:
  - M4: §11.4 Plan view reduced to import, approve and manual pick for imaging
    nights. The photometry planner of §A.7 is not affected.
  - M5: §8.1 removes initial imaging-plan building from the agent's role.
- The Siril stacking templates of M3 (four variants) and Astroalex's roadmap
  phase 4 are the same work. Write them once here, where the tests are
  stricter. Astroalex is MIT and this repository is GPL-3.0; the operator owns
  both, so the templates need a licence Astroalex can also ship. Decide it
  when M3 starts.
