# CLAUDE.md — Working instructions for Claude Code

You are the sole developer on this project. **No human reviews your code line by line.**
Act accordingly: prefer boring, verifiable, well-tested constructions over clever ones,
and stop to ask when the specification is silent.

---

## 1. Before anything else

Read [`SPEC.md`](SPEC.md) in full. It is the authoritative specification. This file tells
you *how* to work; SPEC.md tells you *what* to build.

If SPEC.md and this file appear to conflict, SPEC.md wins on substance, CLAUDE.md wins on
process. If a real contradiction exists, stop and raise it.

## 2. The one thing that matters most

This software commands a motorised mount carrying ~10 kg of glass, unattended, at night,
with nobody watching. **A bug in the safety layer breaks equipment. A bug anywhere else
costs a night of data.**

Treat `backend/nocturne/safety/` as the most important code in the repository. It gets:

- Property-based tests (hypothesis), not just examples
- Exhaustive tests that attempt to violate every limit through every entry point
- No shortcuts, no `# TODO`, no partially implemented branches

**Invariants that must never be broken:**

1. Every command reaching the executor has passed `safety.validate()`. There is no other
   path. If you find yourself writing a code path that bypasses it, the design is wrong —
   stop and reconsider.
2. The agent cannot write to `config/safety.yaml`, cannot modify limits at runtime, and
   cannot raise its own autonomy level. These are not policy; they are enforced in code.
3. `meridian.calibrated: false` blocks `supervised` and `autonomous` modes with a hard
   failure and an explicit message. Never a warning. Never a default-to-permissive.
4. Loss of the agent, the API, or the network degrades to deterministic autonomous
   operation or to parking. It never degrades to uncontrolled motion.
5. The watchdog process does not depend on the FastAPI process to function.

## 3. Methodology: spec-driven, TDD against simulators

INDI ships simulator drivers. **The entire test suite runs without hardware**, in CI. This
is mandatory — it is the only reason it is safe for you to work unreviewed.

Per feature, in this order:

1. Confirm the relevant SPEC.md section covers what you are about to build. If it does
   not, **stop and ask** — do not invent behaviour.
2. Write failing tests that encode the acceptance criteria.
3. Implement until green.
4. Run the full suite, not just the new tests.
5. Commit, referencing the spec section.

Do not write implementation before tests. Do not leave tests skipped or marked xfail
without an issue link explaining why.

## 4. Milestone gating

Milestones M1–M6 are defined in SPEC.md §14. Each has **AUTO** criteria (you verify,
against simulators) and **HITL** criteria (only the operator can verify, on real hardware).

- Do not start milestone N+1 until every AUTO criterion of milestone N passes.
- When you reach a HITL criterion, stop, summarise exactly what the operator needs to test
  and what result would constitute a pass, and wait.
- **M1 first, always.** The Wave 150i connecting to `indi_eqmod` over direct USB serial —
  without the SynScan app as a bridge — is the highest-risk unknown in the project. If it
  fails, the fallback is the WiFi driver, and that decision must be recorded as an ADR
  before any further work.

## 5. When the spec is silent

Ask. Do not guess.

If asking is impractical mid-task, choose the most conservative option, implement it
behind a clearly named configuration value, write an ADR in `docs/decisions/`, and flag it
explicitly in your summary. Never silently assume.

ADR format: context, options considered, decision, consequences. Numbered sequentially.

## 6. Code conventions

**Language.** All code, identifiers, comments, docstrings, commit messages, API field
names and documentation in **English**. User-facing strings are i18n keys only — a literal
string in a rendered component is a CI failure. Locales: `en` (source), `fr`, `es`.

**Python.** 3.11+. `ruff` for lint and format. `mypy --strict` — no `Any` without a
comment justifying it. Pydantic for all config and API models. Async throughout the
backend; no blocking calls in request handlers. Type every function signature.

**TypeScript.** Strict mode. No `any`. Functional components, hooks. `react-i18next` for
all text. Night mode (red on black) is the default theme, not an option.

**No magic numbers.** Every threshold, timeout, tolerance and limit comes from validated
YAML config. If you are typing a number into a comparison, it belongs in a config file.

**No hardcoded equipment.** Focal lengths, pixel sizes, plate scales, guide scales — all
config. The operator plans to migrate from guide scope to OAG and to add a coma corrector;
both must be config changes, not code changes.

**Errors.** Fail loudly at startup on invalid config. Fail safe at runtime. Never swallow
an exception in a control path. Log with structured context (session id, frame id, state).

**Dependencies.** Prefer the standard library and the stack already listed in SPEC.md §4.
Adding a dependency requires an ADR.

## 7. Commits and branches

- Conventional commits: `feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`
- Reference the spec section: `feat(safety): enforce meridian limits (SPEC §9.1)`
- One logical change per commit. Working tree green before each commit.
- Branch per milestone: `m1/instrument-control`, `m2/ekos-parity`, …

## 8. What you must not do

- Do not implement post-processing (stretching, gradient removal, colour calibration).
  Out of scope by explicit decision.
- Do not implement planetary or lunar video capture. Deep-sky only.
- Do not reimplement guiding, autofocus, polar alignment or plate solving. Drive Ekos.
- Do not add telemetry fields that require sending image data to the agent. The agent
  reads scalars. Ever-growing payloads are a design failure, not a feature.
- Do not call the agent per frame. Invocation is poll + event, per SPEC §8.3.
- Do not weaken a safety default to make a test pass. Fix the test or raise the conflict.
- Do not enable unattended operation paths while the dew-heater device is absent from
  config (SPEC §9.3).

## 9. Working with the operator

The operator is an experienced astrophotographer and not a programmer. When reporting:

- Lead with what works and what does not, in plain terms
- For HITL steps, give exact commands and the expected observable result
- Flag anything that requires a purchase, a hardware change, or a night of testing
- Do not present untested code as working

Assume they will read your summary on a phone, at night, on a terrace.

## 10. Current state

Milestone: **M1 not started.**

First task: repository scaffolding, `install.sh`, config schemas with validation, INDI +
Ekos bring-up against simulators, and the DBus bridge. Then the HITL Wave 150i test.
