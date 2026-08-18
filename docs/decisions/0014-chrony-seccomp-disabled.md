# 0014 — chrony's seccomp filter is disabled so it can read the PPS reference clock

Status: Accepted · 2026-08-18 · Milestone M1 (infrastructure), prerequisite for Addendum A §A.8.4
Evidence: docs/FIELD-NOTES-TIMING.md §2.7, measured on the reference rig.

## Context

A GPS receiver with a 1 Hz pulse-per-second output is now fitted to the Pi, wired to
GPIO4. `chronyd` disciplines the system clock from it via `refclock PPS /dev/pps0`.

Debian ships `/etc/default/chrony` with `DAEMON_OPTS="-F 1"`, which enables chrony's
seccomp system-call filter in enforcing mode. That filter does not permit the ioctls a
PPS device requires. With it enabled, on this rig:

```
MS Name/IP address    Stratum Poll Reach LastRx Last sample
#? PPS                      0    4     0      -     +0ns[   +0ns] +/-    0ns
```

`Reach 0`, indefinitely.

**The failure is silent.** `chronyd` opens `/dev/pps0` successfully — the open is
permitted, the ioctls are not — so there is no permission error, no device error, and
**nothing in the journal at all**. Several hours of bring-up were lost to a subsystem
that reported itself as working and was not. The only symptom is a reference clock that
never becomes reachable, which is indistinguishable from bad wiring, a receiver without
a fix, or a missing device-tree overlay: the three things anyone would check first.

This matters beyond convenience. Addendum A §A.8.4 requires the frame's time source and
its measured offset in every FITS header, and requires science-grade flagging to be
refused when the clock is not synchronised. A clock that believes it is fine while its
reference is unreachable is exactly the failure that provenance exists to catch, and
here it would be caught only by noticing the absence of something.

## Options considered

1. **Leave `-F 1` and drop the PPS refclock.** Keeps the shipped hardening and gives up
   the reference clock. NTP over wired Ethernet already meets §A.8.4's ≤100 ms. This
   discards a measured five-order-of-magnitude improvement (±21 ms → ±155 ns) and closes
   off occultation timing entirely, for a threat model that does not apply — see below.
2. **`-F 0`: filter compiled in but not enforced.** Half a measure. It neither hardens
   nor documents; a reader finds a number between two working values and cannot tell
   which behaviour was wanted.
3. **Write a custom seccomp policy permitting the PPS ioctls.** chrony's filter is not
   configurable from the outside; this means patching and building chrony. A local build
   of a clock daemon, maintained against Debian security updates, to avoid one flag.
4. **`-F -1`: filter disabled.** What chrony's own documentation recommends when
   reference clocks or hardware timestamping are in use.

## Decision

Option 4. `/etc/default/chrony` carries:

```
DAEMON_OPTS="-F -1"
```

### Why this is acceptable, stated rather than assumed

The seccomp filter defends against an attacker who has already achieved code execution
inside `chronyd`, and limits what they can then do. The exposure it removes here is
small and the cost it imposes is total:

- `chronyd` on this rig serves **no clients**. It is a client of upstream NTP servers
  and of a local reference clock. There is no listening service to reach it through.
- The rig sits behind a residential router on a wired LAN, not on a public address.
- The one input that is externally influenced — NTP packets from `tick.espanix.net` and
  peers — is exactly the surface the filter was written for, and it remains the reason
  this is recorded as a decision rather than done quietly.
- With the filter enabled, the feature the daemon is installed for **does not work at
  all**, and does not say so.

**This is a security-relevant configuration change made deliberately.** It is recorded
here because a future reader finding `-F -1` on a machine has no way to tell a
considered decision from a copied-and-pasted workaround, and the difference decides
whether they put it back.

### It belongs in the installer, with its own verification

`install.sh` must set it and `--check` must verify the *outcome*, not the flag: that
chrony's selected reference is PPS. Asserting the flag would confirm only that the file
says what we wrote in it. The outcome is one command:

```
chronyc -c tracking      # field 0 is the reference ID; expect PPS
```

That assertion is the same one Addendum A §A.8.4 needs at frame time, so it is written
once and used in both places.

## Consequences

- **The failure mode is now documented, and it is the one worth documenting.** Anyone
  debugging an unreachable PPS refclock on Debian or Raspberry Pi OS will check wiring,
  overlay and fix status — all three of which can be perfect. This ADR and
  FIELD-NOTES-TIMING §2.7 are the record that the fourth cause exists.
- **`chrony` becomes a dependency of the timing subsystem**, replacing
  `systemd-timesyncd`, which cannot use reference clocks at all. The Debian package
  disables timesyncd on install, so this is not a conflict to manage — but it is a change
  of the machine's clock daemon and belongs in the installer's package list rather than
  in an operator's shell history.
- **The setting survives package upgrades** (`/etc/default/chrony` is a conffile) but a
  distribution that changes the default would prompt on upgrade. `--check` catches a
  regression regardless, because it tests the outcome.
- **This carries over unchanged to the replacement receiver.** The u-blox NEO-M8N on
  order changes the wiring and possibly the baud rate; it does not change what seccomp
  blocks. FIELD-NOTES-TIMING §7 marks this as the one item on that list that is not
  optional.
- **The backend still does not touch `/dev/pps0`.** It is `crw------- root root`, and the
  design reads timing state through `chronyc -c tracking` instead, keeping the Nocturne
  process unprivileged. Loosening the device permissions would be a separate decision and
  is not taken here.
