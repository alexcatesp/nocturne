# Wave 150i bench test — the M1 hardware test

**What this is:** proving the mount talks to the Pi over a USB cable, with no
telescope attached and nothing moving. Twenty minutes indoors, on a table.

**Why it matters:** this is the highest-risk unknown in the whole project. Some
users report the Wave 150i only connecting through the SynScan app acting as a
bridge. If a direct cable works, everything else follows. If it does not, the
fallback is WiFi and that changes the design.

> **Nothing in this procedure moves the mount.** No slew, no goto, no park, no
> tracking. Not one motor turns.

---

## Before you start

**On the table:**

- The Wave 150i mount head. **No telescope. No counterweight. No dovetail.**
  Nothing bolted to it. If the OTA is on, take it off.
- Mount powered from its 12 V supply.
- A USB cable from the mount to the Raspberry Pi.
- The Pi powered, on the network, and you logged into it.
- The Pi running **Raspberry Pi OS 64-bit Trixie (Debian 13) or later**. Check
  with `cat /etc/os-release`. If it says `bookworm`, re-image first — the
  installer will refuse, and it is right to (ADR 0008).

**Not needed:** tripod, polar alignment, darkness, the camera, the filter wheel,
the focuser. Those come later.

**Close the SynScan app.** On your phone, on any tablet, anywhere. The whole
point is to find out whether the cable works *without* it. If SynScan is
connected to the mount, this test tells you nothing.

---

## Step 1 — Is the cable there?

```
ls -l /dev/serial/by-id/
```

**Good:** one line comes back, mentioning something like `CH340`, `CP210x`,
`FTDI` or `Prolific`, ending in `-port0`.

**Bad:** `No such file or directory`, or nothing listed.

If bad: it is the cable, the port or the power — not the software. Try the other
USB socket on the mount, try a different cable (some are charge-only and carry
no data), and check the mount is actually powered on. Then run it again.

**Copy that whole line down.** You need it in step 3.

---

## Step 2 — Tell Nocturne where the mount is

Open the equipment file:

```
nano config/equipment.yaml
```

Find the `mount:` section near the bottom, and the line `port:`. Change it to
the path from step 1, in full, in quotes:

```yaml
mount:
  port: "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
```

Save with `Ctrl+O`, `Enter`, then `Ctrl+X`.

Check it:

```
.venv/bin/nocturne check-config
```

**Good:** `Nocturne configuration OK.` and a summary of your rig.

**Bad:** anything else. It will name the line it did not like. Usually a missing
quote mark.

---

## Step 3 — Start the drivers

```
indiserver -v indi_eqmod_telescope
```

This keeps running and printing. **Leave it.** Open a *second* terminal window
for everything below.

Watch the first window as you do step 4. It is where the mount's own complaints
appear.

---

## Step 4 — Connect

In the second window:

```
.venv/bin/python scripts/bench-test-mount.py
```

It takes under a minute. It connects, reads what the mount says about itself,
writes one harmless value (your observing site), reads it back, and
disconnects. It does not move anything.

---

## What the result means

### Pass

The last line says:

```
RESULT: PASS — the Wave 150i is talking to the Pi over direct USB serial.
```

That is the answer we needed. Direct USB serial works, no SynScan bridge. The
project's biggest unknown is resolved in the good direction.

Stop the `indiserver` window with `Ctrl+C`. You are done. Tell me it passed and
I will write the outcome up.

### Fail

The last line says:

```
RESULT: FAIL — <reason>
```

Do not try to work around it. **Capture the evidence:**

```
.venv/bin/python scripts/bench-test-mount.py > ~/bench-test.log 2>&1
```

Then in the `indiserver` window press `Ctrl+C`, and run this to collect
everything in one file:

```
{ echo "--- serial devices ---"; ls -l /dev/serial/by-id/ /dev/ttyUSB* 2>&1
  echo "--- kernel log ---";     dmesg | tail -40
  echo "--- indi version ---";   indiserver --help 2>&1 | head -5
  echo "--- config ---";         .venv/bin/nocturne check-config 2>&1
  echo "--- bench test ---";     cat ~/bench-test.log
} > ~/wave150i-failure.txt
```

Send me `~/wave150i-failure.txt`. That has everything I need.

**Then, and only then**, one diagnostic worth doing: connect the mount with the
SynScan app as it normally works, confirm the mount itself is healthy, and
disconnect again. If SynScan works and the cable does not, that is the exact
finding we are looking for, and it decides the fallback. It gets recorded as an
ADR before any further work.

### Neither — it hangs

If nothing has printed for two minutes, press `Ctrl+C`. That is a fail: the
mount accepted the connection but never answered. Capture the logs the same way
and say it hung.

---

## What this test does and does not prove

**Proves:** the cable carries data, `indi_eqmod` recognises the Wave 150i,
Nocturne can connect it, read its state and write to it — all without SynScan.

**Does not prove:** that slewing, tracking, syncing and pulse guiding work. That
is deliberate — none of them is safe to exercise blind, and none is needed to
answer the question this test asks. SPEC section 14 does require them for M1's
hardware criteria, and they need the OTA fitted, the mount on the tripod and the
sky. That is **stage two**: a separate session, which should not happen until
the meridian calibration in
[`meridian-calibration.md`](meridian-calibration.md) is done — because a slew
near the meridian with a 200PDS and no tripod extension is the collision this
project is most concerned about.

> ⚠️ Until M2 lands, Nocturne does not enforce altitude, meridian or Sun limits
> on a pointing command (see
> [ADR 0007](decisions/0007-m1-pointing-is-ungated.md) and issue #1). **Do not
> point the rig at the sky under Nocturne's control yet.** Stage two is done by
> hand through Ekos, not through Nocturne.

---

## Known constraints on this rig

- **No tripod extension.** The tube reaches the tripod legs near the meridian.
  This is the most dangerous thing about the setup, and
  [`meridian-calibration.md`](meridian-calibration.md) exists for it.
- **SD card, not NVMe.** SPEC section 2.2 lists the NVMe HAT and SSD as a
  required purchase. Stacking write loads kill SD cards. Not blocking until M3 —
  but if the Pi needs re-imaging for Trixie anyway, fit the NVMe in the same
  sitting.
- **No secondary dew heater.** SPEC section 9.3 makes this blocking for M6.
- **Rain is yours to watch.** There is no observatory, and parking does not
  protect the equipment (SPEC section 9.4).
