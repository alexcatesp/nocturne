# 0011 — The Wave 150i runs on direct USB serial; the WiFi fallback is not taken

Status: Accepted · 2026-08-06 · Milestone M1
Evidence: docs/FIELD-NOTES-M1.md section 1, and the property dump at
`backend/tests/fixtures/hardware/wave150i-properties.txt`.

## Context

CLAUDE.md section 4 named one unknown above all others:

> The Wave 150i connecting to `indi_eqmod` over direct USB serial — without the
> SynScan app as a bridge — is the highest-risk unknown in the project. If it
> fails, the fallback is the WiFi driver, and that decision must be recorded as
> an ADR before any further work.

This ADR records the outcome. It is written because the instruction was to
record the decision either way, and because "we tried it and it worked" is worth
a file that the next person can find.

## The result

**PASS.** The mount connects to `indi_eqmod_telescope` over a direct USB serial
cable, with the SynScan app closed and no bridge of any kind.

Read back from the controller board, not from a driver's defaults:

```
MOUNTINFORMATION.MOUNT_TYPE       = CUSTOM
MOUNTINFORMATION.MOTOR_CONTROLLER = 033b
MOUNTINFORMATION.MOUNT_CODE       = 0x45
STEPPERS.RASteps360               = 3878400
STEPPERS.DESteps360               = 3525120
STEPPERS.RAStepsWorm              = 14000000
STEPPERS.DEStepsWorm              = 14000000
SIMULATION.DISABLE                = On
```

Two things make this real hardware rather than a driver inventing plausible
numbers. The step counts **differ between the axes**, as a strain wave mount's
differing reductions require — a fabricated value would not. And mount code
`0x45` is not in eqmod's table of known models, so the driver read its parameters
from the mount instead of a lookup table, which is the preferable outcome: the
values are the mount's own.

`TELESCOPE_MOUNT_TYPE.EQ_GEM=On` confirms the driver treats it as a German
equatorial, so the meridian flip logic of SPEC section 9.1 applies as written.

## Decision

**Serial. `equipment.yaml` keeps `connection: "serial"`.** The WiFi driver is not
adopted, and no WiFi code path is written.

The fallback is not deleted from the schema. `MountConnection` still admits
`"wifi"`, because the situation it exists for — a cable or a port failing in the
field — has not stopped being possible; it has only stopped being the expected
case. What is removed is any *work* predicated on it.

## Consequences

- **M1's blocking unknown is closed.** The remaining M1 HITL work — camera,
  filter wheel, focuser, guide camera — is bench testing of devices whose
  drivers are not in doubt.
- The connection details are now specified rather than assumed, and each has a
  test: the port is CDC-ACM and appears as `ttyACM`, not `ttyUSB`
  (ADR-less, schema-enforced, field notes section 2.1); the baud rate is
  selected before `CONNECT` because the driver starts at 9600 (section 2.2); the
  port the driver reports is preferred over the configured one (section 2.3).
- The recorded property dump becomes a test fixture. It is categorically
  different from `fake_kstars.py`, which shares an author with the bridge it
  verifies and therefore shares any misconception; nobody wrote the contents of
  this one. Where an `indi_eqmod` path can be tested against it rather than
  against a stub, it is.
- The link is USB serial, so it carries the mount's power-cycle behaviour with
  it: unplugging or restarting the mount drops the device and `indiserver`
  respawns the driver at its defaults. That is why the slew-rate ceiling is
  re-applied on every reconnection rather than once at startup — see field notes
  section 3.2 and `backend/nocturne/executor/mount.py`.
- No ADR is needed for the WiFi driver, its INDI package, or its configuration.
  If the cable ever fails in the field, that decision gets its own file.
