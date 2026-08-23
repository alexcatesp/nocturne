# 0021 — Measured calibrations live in an append-only store, and nothing writes configuration

Status: Accepted · 2026-08-23 · Milestone M2
Requested by the operator: "se deben poder calibrar y escribir desde la app, con
confirmación de escritura." Affects SPEC §5.2, §9.1, and ADR 0010's open question.

## Context

The meridian limits are measured on the rig. Until now the only place to put a
measurement was `config/safety.yaml`, typed in by hand after a session with a
notebook on a terrace — and the operator has asked for the calibration tool in
the web app to measure them and write them itself.

That runs straight into CLAUDE.md invariant 2:

> The agent cannot write to `config/safety.yaml`, cannot modify limits at
> runtime, and cannot raise its own autonomy level. These are not policy; they
> are enforced in code.

with an unconditional test behind it. The invariant is about the *agent*, but
the enforcement is deliberately unconditional, because "which module is allowed
to write a limit" is exactly the question that gets answered generously at two
in the morning.

There is also a second problem the file cannot express. The mount's polar axis
is tilted to the site latitude, so the arc the tube sweeps past the tripod legs
is a function of it: a calibration measured on the home terrace is not valid at
a dark site far to the south. The operator said as much — "pueden variar entre
latitudes". A YAML file holds one calibration; a portable rig has one per place
it is set up, and each needs the latitude, the counterweight state and the
optical train it was measured with, or it cannot be told apart from the others.

## Options considered

1. **The app writes `safety.local.yaml`** through one narrow, allowlisted
   module. Simple, and it puts an API-reachable path to the safety limits in the
   codebase for the first time. Every future reader of the allowlist has to
   re-derive why it is safe.
2. **The app records a proposal; a CLI command applies it** to
   `safety.local.yaml`. Keeps the invariant, and asks a person who is not a
   programmer to run a terminal command on a terrace at night, having just done
   the one thing this tool exists to make unnecessary.
3. **The measurement goes to a store of its own**, and the governor consults
   the store alongside the file.

## Decision

**Option 3**, with the property that makes it safe stated first:

> The store can only ever make the forbidden band **wider**. It cannot open sky
> that `safety.yaml` closed.

Under ADR 0020 the meridian limits bracket a band the tube may not enter, and
stricter means wider. So when both sources have something to say, the enforced
band is the *union*: `min(east)`, `max(west)`, edge by edge. A store handing back
a narrow band changes nothing; a file with limits typed by hand is never
loosened by a database. That is the whole safety argument, it is one line of
code, and it is asserted as a property over generated numbers rather than as an
example.

### What is stored, and what is enforced

`nocturne/store/calibration.py` keeps profiles and their marks. A mark is one
point where the operator judged clearance to have run out, with the hour angle,
the **declination, the altitude and the pier side** it was taken at. Only the
derived pair is enforced — the widest mark on each side, computed in
`derive_band_deg` rather than in the operator's head — but everything is kept.
ADR 0020 leaves the shape of this limit genuinely open, and if the measurements
show it depends strongly on declination, the evidence to model that properly
must already exist rather than requiring another afternoon at the tripod.

### Append-only

No UPDATE, no DELETE, anywhere. Measuring again records a new profile and the
old one stays as evidence of what was being enforced last week. A structural
test scans the SQL handed to `execute`, `executemany` and `executescript` — not
every string in the file, because this module's own docstring says the words
UPDATE and DELETE while explaining that it does not use them, and a detector
that cannot tell prose from a statement is a detector that gets silenced.

### Matching, not approximating

`latest_for` returns the newest profile whose latitude is within
`latitude_tolerance_deg` of the current site, with the same counterweight state
and the same optical train. Otherwise it returns nothing. **Nothing is not a
fallback**: with no profile and no limits in the file, the rig is uncalibrated
and every pointing command is refused (ADR 0019). Shipped tolerance is 0.5°,
about 55 km, a tenth of the shipped safety margin.

### Read once, at startup

The governor is handed a resolved band at construction and holds it for the life
of the process. ADR 0010 asked for this and the reason has not changed: a limit
that can change under a running session is not a limit.

### Where the file lives

`~/.local/share/nocturne/nocturne.db`, or `NOCTURNE_DATA_DIR`. Not a
configuration value: it is machine state, SPEC §5 lists three configuration
files rather than four, and a fourth path to get wrong buys nothing. The test
suite redirects it for the whole run, for the same reason it refuses to touch
`config/` — on the reference rig that file holds the operator's real
calibration, and a test whose outcome depends on whether they have calibrated
is not a test.

## Consequences

- **`nocturne.store` is the first entry in `MODULES_THAT_MAY_WRITE`.** It writes
  one file, its own. `nocturne.agent`, `nocturne.api`, `nocturne.safety` and
  `nocturne.schemas` still may never appear there, and a test says so.
- **The disk-write detector had a blind spot and it is closed.** It looked for
  `write_text`, `open()` and friends; `sqlite3.connect()` creates a file and was
  invisible to it. Any module could have kept a database anywhere without ever
  appearing in the allowlist. Found while adding the first database to the
  project, with a positive control now proving the detector sees one.
- **The configuration-filename guard now reads code rather than prose.** It
  refuses any module that writes to disk and names a config file; the store
  names `safety.yaml` in its docstring to explain what it is *not* doing.
  Docstrings are excluded and every other string literal is still scanned, with
  positive controls on both halves — because the alternative was to reword a
  docstring to get past a detector, which is how detectors start lying.
- `nocturne.safety` still imports nothing from the store. The join lives in
  `nocturne/calibration/meridian.py`, so the safety layer stays a pure function
  of what it was handed.
- `nocturne check-config` now prints the enforced band **and its source** — the
  file, the store, or both. With two possible origins for a number, "no pointing
  between −30 and +25" is half an answer without "says who".
- ADR 0010's invalidation flag now has somewhere to live. It is not implemented
  here: nothing produces it until the driver backstop exists, and a setter with
  no producer is a partially implemented branch.
- The operator's "confirmación de escritura" is not in this slice. Nothing calls
  `record()` yet — the API and the app are the next two. What exists is the
  place the confirmed measurement will land, and the guarantee that landing
  there can never widen what the rig is allowed to do.
