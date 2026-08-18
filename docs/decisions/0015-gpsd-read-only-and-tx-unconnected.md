# 0015 — Nothing writes to the GPS receiver: gpsd read-only, and the Pi's TX left unwired

Status: Accepted · 2026-08-18 · Milestone M1 (infrastructure), prerequisite for Addendum A §A.8.4
Evidence: docs/FIELD-NOTES-TIMING.md §2.1, §2.3 and §2.4, measured on the reference rig.

## Context

The GPS receiver fitted to the rig is **not the part its label claims**. The board is
silkscreened GY-GPS6MV2 and the module is marked `u-blox NEO-6M-0-001`. Neither is true:

- NMEA output carries talker IDs `$GN`, `$GP` and **`$BD`**. u-blox generation 6 is
  GPS-only and emits `$GP` exclusively; where u-blox does support BeiDou it uses `$GB`,
  never `$BD`.
- `ubxtool -p MON-VER` gets no reply. A genuine u-blox answers that.

It is re-marked silicon of unknown vendor — CASIC AT6558 or MediaTek MT3333 class on the
evidence, common on loose GY-GPS6MV2 boards. **We do not know its command set**, and its
datasheet behaviour is whatever the real silicon does, which we cannot look up.

Two things on the rig write to a serial device by default, and both are dangerous here:

1. **gpsd probes.** On startup gpsd tries u-blox, MediaTek and SiRF binary configuration
   packets to identify the device. On unidentified silicon these can change the baud rate
   or switch the module into a binary protocol.
2. **The Pi's own UART TX.** Wiring GPIO14 to the module's RX gives anything on the Pi a
   path to the receiver.

The board carries a **24C32A EEPROM and a backup cell**, so a configuration change made
by either route is *persistent*. Recovering from it means removing the backup cell to
force defaults — a physical operation on a soldered assembly, in the dark, on a subsystem
the entire timing chain depends on.

This was not theoretical. Connecting the Pi's TX to the module's RX **broke NMEA
reception entirely**: unparseable output at every baud rate from 4800 to 230400.
Disconnecting that one wire restored clean, checksum-valid NMEA immediately.

## Options considered

1. **Identify the silicon properly, then configure it deliberately.** The engineering
   answer, and it buys nothing: 1 Hz PPS at factory defaults is precisely the
   configuration wanted. There is no setting we want to change, so the identification
   effort purchases the ability to make a change we do not want to make.
2. **Let gpsd probe, and recover if it breaks something.** Recovery is desoldering a
   backup cell. The probability is not high; the cost is out of proportion, and the
   failure would appear as "no fix" on a night when a fix was the point.
3. **Wire TX but keep gpsd read-only.** Leaves the path open for anything else on the
   Pi — a stray `cat > /dev/ttyAMA0`, a future script, an operator experimenting. The
   observed failure came through this wire, not through gpsd.
4. **Nothing writes to the receiver, by configuration *and* by wiring.**

## Decision

Option 4, enforced at two independent layers.

### Layer one — gpsd never writes

`/etc/default/gpsd`:

```
DEVICES="/dev/ttyAMA0"
GPSD_OPTIONS="-n -b"
USBAUTO="false"
```

- **`-b`** is read-only mode: gpsd sends no configuration packets, ever.
- **`-n`** is separately mandatory: without it gpsd does not poll the device until a
  client connects, so PPS and NMEA are absent until something asks — which looks exactly
  like a receiver with no fix.
- **Socket activation must be disabled** (`systemctl disable gpsd.socket`). Left enabled,
  the service runs and nothing can connect to it: `cgps` reports `timeout contacting
  gpsd` against a daemon that is up.

### Layer two — the wire is not there

**GPIO14 (physical pin 8) is left unconnected.** The receiver's TX reaches the Pi's RX;
nothing reaches the receiver.

This is the layer that matters, because it holds against software that has not been
written yet. A configuration flag protects against the daemon we configured; an absent
conductor protects against every future process on the machine, including a mistake made
at 3 a.m. by someone who has forgotten this decision exists.

`/dev/ttyAMA0` is also the explicit device, never `/dev/serial0` — on the Pi 5 `serial0`
is the debug UART on the 3-pin JST header, not the GPIO header. That is a correctness
requirement rather than a safety one, and it is recorded in FIELD-NOTES-TIMING §2.2.

### What is given up, plainly

The receiver cannot be reconfigured — no update rate above 1 Hz, no constellation
selection, no NMEA sentence filtering, no baud change. **Nothing in Nocturne wants any of
those.** The requirement is a 1 Hz pulse and a position, which is what the module does
when left alone. If a future requirement needs configuration, connecting one wire is a
five-minute change that supersedes this ADR, and it should be made deliberately rather
than found already made.

## Consequences

- **The installer sets this and `--check` verifies it.** As with ADR 0014, the check is
  of the outcome — NMEA arriving with valid checksums and a fix in GGA field 6 — not of
  the flag, because a flag we wrote asserts only that we wrote it.
- **`-b` and the missing TX are two answers to one question**, and both are kept even
  though either would have prevented the observed failure. They fail independently: the
  flag can be lost to a package upgrade or an edit, the wire cannot be lost by accident.
- **A future operator will see an unpopulated pin and want to fill it.** This ADR and
  FIELD-NOTES-TIMING §2.4 are the record that the gap is deliberate, which is the whole
  reason for writing it down: an absent wire looks like an unfinished one.
- **It carries over to the replacement.** The u-blox NEO-M8N on order *does* tolerate
  probing and *does* answer UBX, so both measures become optional there —
  FIELD-NOTES-TIMING §7 marks them as such. Keeping them is still the conservative
  choice, and the current module is retained as a spare, so the constraint stays live on
  at least one receiver.
- **This says nothing about PPS wiring**, which is a hardware detail rather than a
  decision: the signal is on the module's TIMEPULSE pin, must be taken from the module
  side of the LED series resistor, and is documented in FIELD-NOTES-TIMING §2.5.
