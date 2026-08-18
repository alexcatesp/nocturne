# 0016 — Addendum A ships as its own document and merges into SPEC.md before M3

Status: Accepted · 2026-08-18 · Milestone M1
Affects SPEC.md §0, §14, §15 and docs/SPEC-ADDENDUM-A.md.

## Context

The operator supplied **Specification Addendum A — scientific characterisation and data
provenance**, which its own header describes as "to be merged as sections §16–§18 and a
revised §14", and its §A.0 as "merge this before M3".

It is not a small extension. It promotes sensor characterisation from a §15 open item to
a hard M3 prerequisite, redefines the existing `transparency_index` field of §7.2, splits
the existing `fwhm_arcsec` into a measured value and a deconvolved estimate, adds a new
`config/science.yaml`, inserts two milestones (M3.5, M3.6) between M3 and M4, adds a fifth
view to M4, and — in §A.7 — states that several SPEC.md v1.0 defaults are not merely
incomplete for photometry but *actively wrong*.

Meanwhile the repository is at **M1 complete, M2 not started**. CLAUDE.md §4 forbids
starting milestone N+1 before milestone N's AUTO criteria pass, and none of what this
addendum specifies can be implemented for several milestones yet.

The decision forced now is not *whether* to accept it — it is authoritative and accepted.
It is **where the text lives in the meantime**, and that is a decision because the wrong
answer damages something.

There is also an immediate trigger. `docs/FIELD-NOTES-TIMING.md` cites this addendum by
section — §A.8.4 for the time source, §A.3 for photometric sky brightness — as the
justification for hardware already fitted and verified. Those citations pointed at
nothing in this repository, which is how the addendum's absence was noticed at all.

## Options considered

1. **Merge it into SPEC.md now, renumbering as §16–§18 and revising §14.** It is what the
   document asks for, and it is premature by five milestones. SPEC.md's §14 is the
   milestone gate the whole methodology hangs from, and `test_config_safety.py` asserts
   shipped configuration against SPEC §5.2 — a large renumbering edit to a document under
   that kind of assertion, made when nothing can yet be implemented against it, is churn
   with a real chance of collateral damage and no offsetting benefit.
2. **Hold it outside the repository until M3.** Keeps SPEC.md clean and loses the thing
   that matters most: §A.0 warns that "retrofitting provenance onto data already collected
   is not possible". Decisions taken during M2 — what the executor records, what the frame
   record carries — either respect this document or quietly foreclose on it. A
   specification that cannot be read while the code that must satisfy it is being written
   is not a specification, and the field notes' dangling citations were the first symptom.
3. **Summarise it in SPEC.md and keep the full text elsewhere.** Two documents that
   disagree by construction, where the shorter is the one people read. The summary would
   drift and the drift would be invisible.
4. **Commit the full text verbatim as its own document, referenced from SPEC.md, merged
   at M3.**

## Decision

Option 4. `docs/SPEC-ADDENDUM-A.md` carries the addendum **unedited**, SPEC.md §0 names it
as authoritative, and §15 records the merge as an open item scheduled before M3.

### Verbatim, with the commentary quarantined

The body is the operator's text with nothing changed. The only editorial addition is a
single marked block at the top, and it exists because two of the addendum's premises moved
between its writing and its filing:

- **§A.8.4 reserves `time_source` including `gps_pps` "for future occultation work".** The
  hardware is now fitted and verified — chrony selects PPS with an estimated error of
  ±155 ns against the ≤100 ms §A.9 asks for (ADR 0014, ADR 0015,
  `docs/FIELD-NOTES-TIMING.md`). The reservation stands exactly as written; it is simply
  no longer hypothetical.
- **§A.3's photometric sky brightness supersedes an SQM**, and now has to hold against a
  lux sensor too, since a VEML7700 was prototyped on the same I²C bus for an unrelated
  project. The field notes' §0 forbids it becoming a second path to that measurement.

Keeping these out of the body is the same rule the field notes follow: evidence and
commentary are separated so a later reader can tell which is which.

### The addendum is authoritative *now*, not at merge

This is the operative half of the decision. Being in its own file does not make it
advisory or deferred. From this commit:

- Work that would foreclose on it is a defect, even in M2. The clearest case is
  §A.5.1: `fwhm_arcsec` in the §7.2 record must become `fwhm_measured_arcsec`, because
  measured FWHM is not seeing and the addendum requires the distinction be carried in the
  field names. Naming that field the old way in M3 and renaming later means a stored
  series whose meaning changed mid-history.
- §A.0's own boundary is preserved: **nothing here modifies §9**. The characterisation
  tools are read-and-measure only, and any tool they introduce passes `safety.validate()`
  like everything else. That sentence is load-bearing and is why this ADR does not touch
  the safety layer or its tests.

### What merging will mean, so it is not rediscovered

At M3 the addendum's sections become SPEC.md §16–§18, §14 gains M3.5 and M3.6, §15 loses
the sensor-characterisation item to §A.1, and §7.2's `transparency_index` and
`fwhm_arcsec` are redefined per §A.4.1 and §A.5.2. Until then SPEC.md's own text is
unchanged apart from the pointers in §0 and §15 — so where the two disagree today, **the
addendum wins on scientific requirements and SPEC.md wins on everything else**, which is
the same precedence CLAUDE.md already sets between SPEC.md and itself.

## Consequences

- **`docs/SPEC-ADDENDUM-A.md` is a specification document living in `docs/`**, which is
  otherwise evidence and operator procedures. That is a small inconsistency, accepted for
  the duration; the merge resolves it.
- **The merge is an open item with a milestone attached**, recorded in SPEC.md §15. An
  addendum that is authoritative but unmerged is exactly the kind of state that becomes
  permanent by not being written down.
- **Two documents can disagree until then.** §A.7.1 says so explicitly and identifies five
  SPEC.md v1.0 defaults it contradicts. Because the addendum is verbatim and the
  contradictions are listed in it, the disagreement is legible rather than latent.
- **Nothing is implemented by this ADR.** No schema field, no config file, no module. In
  particular `config/science.yaml` (§A.9) and the `time_source` enum (§A.8.4) are **not**
  created here, because M3 has not started and CLAUDE.md §4 gates it. §A.8.4's "decide
  this now" is answered by this ADR: the reservation is a requirement of record, and the
  hardware that made it hypothetical no longer does.
- **The field notes' citations now resolve.** That was the trigger, and it is also the
  test: a document in this repository citing §A.8.4 should be able to link to it.
