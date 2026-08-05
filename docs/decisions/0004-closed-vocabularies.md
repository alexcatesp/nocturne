# 0004 — Closed enumerations where the specification lists examples

Status: Accepted · 2026-08-05 · Milestone M1

## Context

SPEC section 5 gives the configuration files as worked examples with the
reference rig's values filled in. For several fields the example shows one value
and the specification never enumerates the alternatives:

| Field | Shown in SPEC | Alternatives named |
|---|---|---|
| `filter_wheel.slots[n].type` | dark, luminance, red, green, blue, empty | none |
| `on_abort.action` | `park` | none |
| `on_abort.notify_severity` | `alarm` (section 9.4) | none |
| `agent.on_budget_exhausted` | `autonomous` | none |
| `agent.on_api_unreachable` | `autonomous` | none |

The schema has to decide whether an unrecognised value is an error or is passed
through. CLAUDE.md section 6 requires loud failure at startup on invalid config,
and CLAUDE.md section 5 requires the conservative option when the specification
is silent.

## Options considered

1. **Free strings.** Any future value works without a code change. But a typo —
   `"parc"`, `"Alarm"` — loads cleanly and then behaves in whatever way the
   consuming code's fallback branch happens to behave. For `on_abort.action`
   that is a mount left tracking into a tripod leg.
2. **Closed enumerations of exactly the values SPEC.md names.** A typo is a
   startup failure with the field named. A genuinely new value needs a one-line
   schema change and a test.
3. **Closed enumerations, extended with values the specification implies.**
   Guessing at a vocabulary that has not been specified.

## Decision

Option 2, with one exception.

The exception is `notify_severity`. SPEC section 9.4 names `alarm`, and
SPEC section 8.2 gives the agent a `notify_operator(severity, message)` tool
whose severity argument is plainly not always `alarm`. A single-valued
enumeration there would make the tool unusable. The schema accepts
`info | warning | alarm`, and this is flagged as an assumption: **if the intended
severity vocabulary is different, `config/schemas/safety.py` is where to change
it.**

## Consequences

- Adding a narrowband filter to the wheel is a change to `FilterType` in
  `config/schemas/equipment.py` plus its test, not just a YAML edit. This is the
  known cost of the choice and it is deliberate: the operator's current wheel
  holds Dark, L, R, G, B and three empty slots.
- Any new abort action or degradation policy in M2 or M6 arrives with the code
  that implements it, rather than being configurable before it exists.
