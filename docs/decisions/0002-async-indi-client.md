# 0002 — An async INDI client in place of pyindi-client

Status: Accepted · 2026-08-05 · Milestone M1
Revised: 2026-08-05 after the decision was challenged in review. The first
version of this ADR argued partly from assumption. It has been rewritten
against measurements, and one of its original claims was wrong — see
"Correction" below.

## Context

SPEC section 4 lists `pyindi-client` for direct INDI access. CLAUDE.md
section 6 requires the backend to be async throughout with no blocking calls in
request handlers, and section 8 forbids reimplementing instrument control.
Adding or dropping a dependency requires this record.

The objection to answer is the serious one: Nocturne's premise is that it does
**not** reimplement solved problems, and writing an INDI client is a
reimplementation. Roughly 600 lines and 100 tests of maintenance surface for
something the INDI project already ships.

## Correction to the first version of this ADR

The original text claimed pyindi-client "builds a C extension in CI and on the
Pi". **That is false, and the decision must not rest on it.** Measured:

```
$ pip install pyindi-client
Successfully installed pyindi-client-2.2.0
$ python -c "import PyIndi; print(PyIndi.__file__)"
.../site-packages/PyIndi/__init__.py         # prebuilt wheel, no compiler run
```

PyPI publishes `manylinux2014_aarch64` wheels for CPython 3.9 through 3.12,
which covers the Raspberry Pi exactly. Installation on the Pi is a download.

## What was actually measured

Against `indiserver` running `indi_simulator_focus`:

```
connectServer() -> True         blocking call took 0.001s
connectServer is a coroutine:   False
main thread:                    MainThread
callbacks delivered on threads: {'Dummy-1'}
live threads:                   ['Dummy-1', 'MainThread']
devices:                        ['Focuser Simulator']
```

1. **It works.** It connects, enumerates devices, and delivers properties.
2. **It is synchronous and callback-driven.** `connectServer()` returns a bool.
   There is nothing to await.
3. **Callbacks arrive on a foreign thread.** `Dummy-1` is the name CPython gives
   a thread it did not create — the C++ client's reader thread, attached to the
   interpreter. Every callback that wants to touch asyncio state must go through
   `loop.call_soon_threadsafe`, every exception raised in one crosses a language
   boundary, and every future resolved from one is resolved off-loop.
4. **Its dependency tail is wrong for this project.** `pyindi-client` requires
   `dbus-python`, `bottle` and `requests`. `dbus-python` has no wheel and failed
   to build until `libdbus-1-dev` was installed by hand; `bottle` is a web
   framework. Nocturne already has `dbus-next` for DBus and will have FastAPI
   for HTTP.
5. **It vendors its own INDI.** The wheel ships `libcfitsio` inside it and links
   an INDI client library of its own version (2.2.0), not the 2.2.3+ that
   `install.sh` builds. The version we run and the version we build would be
   different things, and neither declares the other.

## Options considered

1. **pyindi-client with a thread bridge.** Roughly fifty lines to marshal
   callbacks onto the event loop. Costs: the async mandate met only by wrapping,
   a foreign thread inside the process that drives the mount, three unwanted
   transitive dependencies including a web framework, and a silent version skew
   between the client library and the server we build.
2. **Our own async client over the INDI protocol.** Costs: ~600 lines we
   maintain. Buys: native asyncio with no foreign threads, no extra
   dependencies, and framing behaviour we can test exactly.
3. **Ekos DBus for everything.** Rejected in ADR 0001, for reasons unrelated to
   this.

## Decision

Option 2. On review the decision stands — but on the honest grounds, not the
ones first given.

The deciding argument is **scope, not build friction**. What is reimplemented
here is the wire format: XML in, XML out, a property cache, a reconnect loop.
What is emphatically *not* reimplemented is anything CLAUDE.md section 8
protects — guiding, autofocus, polar alignment, plate solving and meridian
handling all remain Ekos'. Those are years of field-tested judgement about
optics and mechanics. A property vector parser is not; it is a format with a
published grammar and an exhaustively enumerable set of messages.

The second argument is that a foreign C++ thread inside the process that
commands a mount is a category of bug — a callback raising into a thread with no
Python frame, a future resolved off-loop, a deadlock at shutdown — that is hard
to reproduce and shows up at night.

## Test coverage of what we now own

`nocturne.executor.indi` is at **93 % statement and branch coverage**, split
across the pure protocol and the client:

| Concern | Where |
|---|---|
| Every def/set/del/message form, all five vector types | `test_indi_protocol.py` |
| Malformed XML mid-stream, unquoted attributes, truncated messages | `test_indi_protocol.py`, `test_indi_client_failures.py` |
| Partial reads: split chunks, byte-at-a-time, several messages per read | `test_indi_protocol.py`, `test_indi_client_failures.py` |
| `>` and quotes inside attribute values; XML declarations; unknown tags | `test_indi_protocol.py` |
| Unbounded message refused rather than buffered | `test_indi_protocol.py` |
| Sexagesimal and decimal numbers, sign handling, round trips | `test_indi_protocol.py` |
| BLOB: definition, base64 payload, split across chunks, invalid base64, size cap | `test_indi_client_failures.py` |
| Driver restart **mid-transaction**: pending wait and in-flight write both fail promptly, other devices unaffected, device usable again afterwards | `test_indi_client_failures.py` |
| Driver restart against the five **real** simulator drivers, SIGKILLed | `test_indi_simulators.py` |
| Server drop, reconnect with backoff, device restoration, giving up | `test_indi_client.py` |

The mid-transaction tests found a real defect on first run: a caller waiting on
a property whose driver had died sat out the full timeout instead of failing.
`IndiDeviceLostError` now fails exactly the waiters for the lost device and
leaves the rest alone. That defect existed because nobody had tested the case —
which is an argument for owning the code *and* testing it, not for not owning
it.

## Fallback, with a trigger

This is not a one-way door. `IndiClient` is reached through one facade
(`Executor`), it is confined to `nocturne.executor.indi` by an enforced import
boundary (`test_safety_boundaries.py`), and its surface is small: connect,
disconnect, read property, write property, wait, subscribe.

**Trigger.** If the M1 hardware test shows the client mishandling the real
`indi_eqmod` driver in a way the simulators do not reproduce — dropped updates,
a hang, a framing error against real hardware timing — do not debug it under
time pressure at night. Swap the implementation.

**The swap.** Write `PyIndiClient` with the same public methods, backed by
`PyIndi.BaseClient`, marshalling its callbacks onto the loop with
`loop.call_soon_threadsafe`. `Executor` takes it in its constructor and nothing
else changes. Estimate: a day, most of it re-running the existing suite against
the new implementation — the tests are written against the client's public API,
not its internals, so they transfer.

Record the outcome as a new ADR either way.

## Consequences

- No C extension, no foreign threads, no `bottle`, no `dbus-python`, and no skew
  between the INDI we build and the INDI we link.
- Nocturne owns the correctness of its protocol handling, and has the tests to
  match. That is a standing cost, honestly incurred.
- Framing is done explicitly rather than by relying on when expat surfaces an
  end-element event: expat defers those, so a driver's reply would otherwise sit
  in the buffer until unrelated traffic arrived — a latency bug in a control
  path.

  **Correction (2026-08-05, with ADR 0008.)** This bullet originally justified
  the explicit framing by saying `XMLPullParser.flush()` does not exist on the
  Python 3.11.2 that Raspberry Pi OS Bookworm ships. The target is now Trixie,
  which ships Python 3.13, where `flush()` does exist. **That specific argument
  no longer applies and is withdrawn.** The framing stays for the reasons that
  do not depend on the interpreter version: it is deterministic, it is
  independent of expat's deferral behaviour changing between releases, and it is
  covered by tests down to byte-at-a-time delivery. No code changed; only this
  justification did.
- BLOB transfer is parsed but never enabled: Nocturne reads FITS from disk, not
  over the wire (SPEC section 11.3). A test asserts the client never sends
  `enableBLOB`.
