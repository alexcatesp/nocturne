# 0013 — Shipped configuration, and an untracked local override beside it

Status: Accepted · 2026-08-08 · Milestone M1
Reported by the operator after M1 completed, against the reference rig.
Affects SPEC section 5.

## Context

`config/equipment.yaml` is version-controlled **and** it is the file that must
hold the operator's real site coordinates. Those two facts are in direct
conflict, and the conflict is not theoretical: every `git pull` on the rig
collided with the edit that makes the rig work.

The problem was made worse, not better, by the decision in ADR 0006's sibling
work to ship a *placeholder* site rather than the operator's real one — the right
call, taken because the previous file put a home address to within metres in a
public repository next to a description of expensive equipment left outside at
night. But it means the shipped file is guaranteed wrong for every user, so
every user must edit it, so every user collides on every update.

It is not only the coordinates:

| Value | File | Why it differs per install |
|---|---|---|
| `site.*` | `equipment.yaml` | where the rig is |
| `mount.port` | `equipment.yaml` | which `by-id` path this cable is |
| `limits.meridian.*` | `safety.yaml` | measured on one tripod, tube and camera |
| `autonomy_level` | `agent.yaml` | how much the operator currently trusts it |

No test could have found this. It is not a property of the code; it is a
property of what the code asks a person to do.

## Options considered

1. **Untrack the configuration entirely** — `.gitignore config/*.yaml` and ship
   `*.yaml.example`. Simple, and it is what many projects do. It loses the
   thing that makes this project's configuration reviewable: the shipped values
   are read by the test suite, and `test_config_safety.py` asserts the shipped
   safety limits against SPEC section 5.2. Untracked files cannot be asserted
   on, so the defaults would stop being verified at exactly the moment they
   stopped being visible.
2. **Environment variables for the handful of per-site values.** Works for
   `site.latitude`; does not survive `limits.meridian`, which is a nested
   mapping with an interlock between four fields. It also puts the meridian
   limits somewhere no one would think to look for them.
3. **A `config/local/` directory of complete replacement files.** Whole-file
   replacement means the operator's copy silently stops receiving new keys — a
   new safety limit added upstream would be absent from their file, and
   `extra="forbid"` would not catch an *absence*. Their rig would run with a
   default they never saw.
4. **A tracked placeholder plus an untracked override merged over it.**

## Decision

Option 4. Each shipped `config/<name>.yaml` may be accompanied by an untracked
`config/<name>.local.yaml`, merged **over** it at load time.

The override path is **derived** rather than listed, so a fourth configuration
file gets one for free and cannot be forgotten.

### Merge rules

- **Mappings merge, key by key, recursively.** Setting `site.latitude` leaves
  `site.longitude` alone.
- **Everything else replaces wholesale** — scalars, nulls, and *lists*. A merged
  list would have to guess whether the operator meant to add a filter or
  renumber the wheel, and guessing about which filter is in slot 4 is how B
  frames get filed as H-Alpha (ADR 0012).
- **A missing override file is normal.** A *present and broken* one is fatal.
  Falling back to the shipped values on a malformed override is how an operator
  ends up observing from the placeholder site while believing they fixed it.

### Provenance is reported, not implied

`check-config` prints every value the local file set, beside the shipped value
it displaced. This is the part that makes the mechanism safe rather than merely
convenient: a configuration assembled from two files is one where *"I changed
that"* and *"the change took effect"* are different statements, and a
misspelled top-level key merges cleanly, changes nothing, and looks exactly like
one that worked.

For the same reason, a validation error names **the file that supplied the bad
value**. Without that, every error is reported against the shipped file, sending
the operator to edit the one file they must not edit.

### `safety.yaml` is layered too, and that needs saying out loud

The meridian limits are the clearest case of a value that is measured per rig,
so `safety.local.yaml` exists. It also means a file the repository does not
track can decide whether ten kilograms of glass swings into a tripod leg.

What makes that acceptable is unchanged from the shipped file:

1. **Nothing in Nocturne can write it.** `test_safety_boundaries.py` forbids any
   module that writes to disk from naming any configuration file, and the
   `.local.yaml` names are now in that list. `meridian.calibrated` becomes true
   because a human typed it, and by no other route (CLAUDE.md invariant 2).
2. **Every override is printed** by `check-config`, with what it displaced.
3. **The default is still refusal.** The shipped file has `calibrated: false`,
   and an absent or empty override changes nothing.

The alternative — layering equipment but not safety — would have left the
operator editing a tracked `safety.yaml` by hand after every calibration, which
is the defect this ADR exists to fix, on the one file where a merge conflict
resolved carelessly at 2 a.m. does the most damage.

**What was considered and rejected:** allowing the override to *tighten* limits
only. It sounds prudent and is wrong here — the shipped limits are placeholders
with `calibrated: false`, so "tighter than shipped" has no meaning, and the
operator's measured limits are the authority the governor exists to enforce.

## Consequences

- `.gitignore` carries `config/*.local.yaml`, with `*.local.yaml.example`
  excepted. Three tracked examples ship, and the one for `safety` leads with
  what the file can do rather than how to fill it in.
- The shipped files stay tracked, stay asserted against SPEC section 5.2, and
  are **never edited by an operator**. `check-config` says so where it warns
  about the placeholder site.
- Two files can now disagree, so `check-config` grew a section. It is longer
  output on a phone at night; the alternative is a silent override, which is
  worse.
- **The tests got the same rule as the code.** The shared `config_dir` fixture
  hands out the repository's real `config/`, and the first version of the test
  for this feature wrote overrides into it — silently changing the shipped
  safety margin that another test asserts, and, on the rig, leaving files that
  would override the operator's own meridian limits. A session-scoped guard now
  fingerprints that directory and fails the run if anything changed it, and
  `writable_config_dir` gives tests a throwaway copy.
- An operator upgrading keeps receiving new keys and new defaults, because their
  file holds only what they changed. That is the property option 3 loses and the
  reason for the merge.
