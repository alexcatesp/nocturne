# 0001 — Ekos for modules, direct INDI for properties

Status: Accepted · 2026-08-05 · Milestone M1

## Context

SPEC section 3 places KStars/Ekos at layer 1 as the executor, and SPEC section 4
lists two ways of talking to it: `dbus-next` for the Ekos bridge, and
`pyindi-client` for "properties Ekos does not expose".

The M1 AUTO criteria (SPEC section 14) are: connect to all five simulator
drivers, **read and write properties**, survive a driver restart, reconnect
automatically. So M1 has to decide which of the two paths carries generic
property access.

The KStars DBus surface is not a published, stable contract. It has changed
between KStars releases, and it is oriented towards Ekos *modules* — align,
focus, guide, capture, mount — rather than towards arbitrary vectors on
arbitrary drivers. Nothing in SPEC.md states which path M1's property access
should take.

## Options considered

1. **Everything through Ekos DBus.** One dependency, one connection. But
   arbitrary property read/write is exactly the case SPEC section 4 carves out
   for direct INDI, and it would make M1 depend on a DBus surface that cannot be
   verified in CI without running a Qt application.
2. **Everything through direct INDI.** Simple and fully testable, but it throws
   away the reason Ekos is in the design at all: polar alignment, V-curve
   autofocus, guiding, dithering and meridian handling are solved problems
   (SPEC section 3, rationale for Ekos as executor).
3. **Split by concern.** Direct INDI for device connection and property
   read/write; the Ekos DBus bridge for the Ekos-level lifecycle and, from M2,
   the modules.

## Decision

Option 3.

- `backend/nocturne/executor/indi/` owns device connection state and property
  read/write. It is what the M1 AUTO criteria are verified against, using real
  `indiserver` and the real simulator drivers.
- `backend/nocturne/executor/ekos.py` owns the Ekos lifecycle: is Ekos running,
  are the profile's devices connected. M2 adds the align, focus, guide and
  capture modules here. Nocturne does not reimplement any of them.
- `backend/nocturne/executor/executor.py` is the facade the rest of the system
  uses, and the place the safety governor is enforced.

## Consequences

- The M1 AUTO criteria are verifiable in CI with no display server and no Qt.
- The Ekos DBus method names remain unverified against a real KStars until the
  M1 HITL step. The bridge introspects the remote objects at connect time and
  refuses to start if the methods it needs are absent, naming what it wanted and
  what it found, so a wrong name is a loud startup failure rather than a call
  that silently does nothing at night.
- Two connections are open during a session, one to indiserver and one to the
  session bus. Both are supervised and both reconnect on their own.
