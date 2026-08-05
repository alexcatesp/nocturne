# 0002 — An async INDI client in place of pyindi-client

Status: Accepted · 2026-08-05 · Milestone M1

## Context

SPEC section 4 lists `pyindi-client` for direct INDI access. CLAUDE.md section 6
requires the backend to be async throughout with no blocking calls in request
handlers, and requires an ADR for any change to the dependency list.

`pyindi-client` is a SWIG binding over `libindiclient`. It is synchronous and
callback-driven, it runs its own thread, and it builds from source against the
installed `libindi-dev`.

## Options considered

1. **Use pyindi-client as listed.** Faithful to SPEC section 4. Costs a thread
   plus a queue to bridge into asyncio, a C extension build in CI and on the Pi,
   and a hard coupling between the Python package and the INDI version installed
   at build time — awkward when INDI is built from source at a pinned tag.
2. **Use pyindi-client only where needed, asyncio elsewhere.** Two INDI client
   implementations. Worse than either alternative.
3. **Write the client against the INDI protocol directly.** INDI 1.7 is a small
   XML protocol over TCP: `getProperties`, `def*Vector`, `set*Vector`,
   `new*Vector`, `delProperty`, `message`. An async client is a few hundred
   lines with no build step and no threads.

## Decision

Option 3. `backend/nocturne/executor/indi/` implements the protocol and an
asyncio client. `pyindi-client` is not a dependency.

This is not a reimplementation of instrument control, which CLAUDE.md section 8
forbids: guiding, autofocus, polar alignment and plate solving remain Ekos'.
What is implemented here is the wire format underneath them.

## Consequences

- No C extension, no threads, no coupling to the INDI version at build time.
- Nocturne owns the correctness of its protocol handling. It is covered by unit
  tests over the parser and serialiser and by integration tests against the real
  simulator drivers, including chunk-boundary and byte-at-a-time framing.
- Message framing is done explicitly rather than by relying on when the
  underlying expat parser surfaces an end-element event. Expat defers those
  events, and `XMLPullParser.flush()` does not exist on the Python 3.11.2 that
  Raspberry Pi OS Bookworm ships, so a driver's reply would otherwise sit in the
  buffer until unrelated traffic arrived — a latency bug in a control path.
- BLOB transfer is parsed but not enabled: Nocturne reads FITS from disk, not
  over the wire (SPEC section 11.3).
