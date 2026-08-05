# 0005 — Executor transport timings without a fourth YAML file

Status: Accepted · 2026-08-05 · Milestone M1

## Context

CLAUDE.md section 6: "Every threshold, timeout, tolerance and limit comes from
validated YAML config. If you are typing a number into a comparison, it belongs
in a config file."

The executor needs timings that no configuration file in SPEC section 5 covers:
how long to wait for a TCP connection to indiserver, how long to wait for a
driver to define a property, how long to wait for a written vector to leave the
Busy state, and the reconnection backoff schedule. SPEC section 5 defines exactly
three files — `equipment.yaml`, `safety.yaml`, `agent.yaml` — and SPEC section 6
lists exactly those three under `config/`.

`safety.yaml` does carry `mount_communication_loss_s: 30`, but that is an abort
condition for a session in progress, not a socket timeout.

## Options considered

1. **Constants in the code.** Directly against CLAUDE.md section 6.
2. **A fourth file, `config/executor.yaml`.** Satisfies the letter of CLAUDE.md
   section 6, but adds a file to a layout SPEC section 6 enumerates, and asks
   the operator to tune socket timeouts they have no way to reason about.
3. **A validated Pydantic model with documented defaults, constructed in code
   and overridable per instance.** The values are named, range-checked and
   immutable; there is no number typed into a comparison; and no new file is
   added to the specified layout. If a file is wanted later, the model loads
   from a mapping unchanged.

## Decision

Option 3. `backend/nocturne/executor/settings.py` defines `IndiSettings`, a
frozen `StrictModel` with `gt=0` bounds on every duration and a documented
default for each.

## Consequences

- Timings are configuration in every sense that matters — named, validated,
  immutable, overridable — without extending the file layout SPEC section 6
  fixes.
- The test suite substitutes impatient settings, so the suite does not spend real
  seconds waiting for reconnection backoff.
- **If the operator needs to tune these on the Pi**, adding `config/executor.yaml`
  is a loader change and an entry in `NocturneConfig`, not a rewrite. Raise it if
  a night is ever lost to a timeout that is wrong for the real hardware.
