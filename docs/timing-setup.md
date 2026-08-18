# GPS, PPS and the environmental sensors — setting up and checking

**What this is:** bringing up the little board that tells the Pi what time it is to within
a fraction of a microsecond, and the two sensors that measure the air. Twenty minutes,
indoors, plus however long the GPS takes to see the sky.

**Why it matters:** two separate reasons, and they are unrelated to each other.

- **Time.** Timing an exoplanet transit or an eclipsing binary minimum is a measurement of
  *when*, and a measurement of when is only as good as the clock. The internet gets the Pi
  to a few milliseconds. The GPS pulse gets it to about 155 nanoseconds.
- **Air.** The dew point is what decides how hard the secondary heater has to work, and
  the heater is what stands between an unattended night and three hours of veiled frames.
  Computing dew point on the rig means the answer does not arrive over the internet.

> **Nothing here moves the mount, and none of it is used by Nocturne yet.** The hardware
> is fitted and verified; the code that reads it comes later. This document is how you
> check it is still working, and how you redo it when the replacement receiver arrives.

Everything below was measured on the reference rig. The evidence, with the reasoning, is
in [`FIELD-NOTES-TIMING.md`](FIELD-NOTES-TIMING.md).

---

## Before you start

- The Pi, powered, on the network, and you logged into it.
- The GPS board wired to the GPIO header, **including the extra PPS wire** — see below.
- The AHT20 and BMP280 on the I²C pins.
- **A view of the sky.** Not a window. See "If it never gets a fix" at the end.

---

## Step 0 — The three wiring facts that are easy to get wrong

You are unlikely to be rewiring anything, but if you are:

**The PPS signal is not on the 4-pin header.** That header is `VCC RX TX GND` and PPS is
not one of them. The pulse comes from pin 3 of the module, marked TIMEPULSE, which is also
what drives the little red LED.

⚠️ **Solder to the module side of the resistor, not the LED side.** On the LED side the
diode drags the voltage down to about 1.8–2.0 V, and the Pi needs 2.31 V to read a "high".
It would work sometimes, which is worse than not working.

⚠️ **Leave the Pi's pin 8 disconnected.** That is the Pi's transmit line, and connecting
it to the module broke reception completely on the reference rig — unreadable output at
every speed. Removing that one wire fixed it instantly. Nothing needs to send anything to
the receiver, so the wire stays off (ADR 0015). **An empty pin 8 is correct, not
unfinished.**

**Pin 12 is spoken for.** It is held for the dew heater. Do not use it for anything else.

---

## Step 1 — Are the sensors there?

```
i2cdetect -y 1
```

**Good:** `38` and `77` appear in the grid. (`10` may also appear — that is a light sensor
belonging to a different project and nothing to do with Nocturne.)

**Bad:** empty grid. Check the SDA/SCL wires and that I²C is enabled in
`raspi-config`. If only one of the two shows up, it is that sensor's wiring — they share
the same two wires, so both failing together points at the bus and one failing alone
points at the part.

> Some BMP280 boards sit at `76` instead of `77`. Either is fine; note which you have.

---

## Step 2 — Is the GPS talking?

```
gpspipe -r -n 20
```

**Good:** lines starting with `$GN`, `$GP` or `$BD`, each ending in `*` and two
characters. That is NMEA, and the two characters are a checksum — if they are there and
the lines look orderly, the receiver and the Pi agree.

**Bad, and what it means:**

| What you see | What it is |
|---|---|
| Nothing at all | `gpsd` is not running, or is not pointed at `/dev/ttyAMA0` |
| `timeout contacting gpsd` | socket activation is still on — `sudo systemctl disable gpsd.socket` |
| Garbage characters | wrong baud rate, or pin 8 got connected |

The device must be **`/dev/ttyAMA0`**. On the Pi 5, `/dev/serial0` is a *different* port —
the little 3-pin debug connector — and pointing anything at it will simply never work.

Now check it can actually see satellites:

```
gpspipe -r -n 20 | grep GGA
```

The **sixth comma-separated field** is the fix quality. `0` means no fix. `1` or more
means it knows where it is.

---

## Step 3 — Is the pulse arriving?

**The pulse only exists once the receiver has a fix.** No fix, no pulse, no LED. If
step 2 gave you a `0`, stop here and go to the last section — this is not broken.

```
cat /sys/class/pps/pps0/name
```

**Good:** `pps@4.-1`. That is the kernel confirming a pulse source on GPIO4. If the file
does not exist, `dtoverlay=pps-gpio,gpiopin=4` is missing from
`/boot/firmware/config.txt` and the Pi needs a reboot after you add it.

```
sudo ppstest /dev/pps0
```

**Good:** a line about once a second, with the `sequence` number going up by one each
time. Let it run for ten seconds, then Ctrl-C.

**Bad:** silence. Almost always no fix (step 2), otherwise the PPS wire — and if you have
just soldered it, re-read Step 0 about which side of the resistor.

---

## Step 4 — Is the clock actually using it? ⚠️ The one that lies

This is the step that cost hours on the reference rig, so it gets its own warning.

```
chronyc sources -v
```

**Good** — look for the line with `PPS` in it, and specifically for the two characters at
the start:

```
#* PPS                      0    4    37     18  +256ns[+1399ns] +/-  155ns
^- tick.espanix.net         1    6    37     18 -1631us[-1630us] +/- 4385us
```

`#*` is what you want. The `*` means chrony has **chosen** the pulse as its reference, and
the `-` on the internet servers below means it has demoted them. The `+/-` at the end is
its own estimate of how wrong it might be: nanoseconds, not milliseconds.

**Bad:**

```
#? PPS                      0    4     0      -     +0ns[   +0ns] +/-    0ns
```

`#?` with **`Reach 0`**, staying at 0 forever.

If steps 1–3 all passed and you see this, the cause is almost certainly **not** what it
looks like. Debian ships chrony with a security filter that blocks exactly the calls a
pulse source needs. chrony opens the device successfully, gets blocked, and **writes
nothing to any log**. There is no error message anywhere. The fix:

```
sudo nano /etc/default/chrony
```

Make the line read:

```
DAEMON_OPTS="-F -1"
```

Then `sudo systemctl restart chronyd` and check again. This is a deliberate,
recorded change — see [ADR 0014](decisions/0014-chrony-seccomp-disabled.md) — not a
workaround to be tidied up later. If someone puts it back, the pulse silently stops being
used and the clock quietly returns to millisecond accuracy.

Finally:

```
chronyc tracking
```

**Pass:** the RMS offset is well under a microsecond.

---

## Step 5 — Do the sensors read sensibly?

Compare the two temperatures. **They will not agree, and that is expected**: the pressure
sensor warms itself up and reads about 1 °C high. On the reference rig, side by side:

```
AHT20   23.89 °C
BMP280  24.85 °C
```

**The AHT20 is the one to believe** for air temperature and humidity, and it is the one
the dew point will be computed from. The BMP280 is for pressure and nothing else. If your
two agree exactly, something is reporting the same sensor twice.

Pressure sanity check: at around 675 m, roughly 936 hPa is right. It should read *low*
compared to the weather forecast, because the forecast is corrected to sea level.

---

## Full pass criteria

Everything below, together:

- `i2cdetect` shows **38** and **77**
- NMEA arrives with valid checksums, and GGA field 6 is **1 or better**
- `ppstest` shows the sequence number **incrementing once a second**
- `chronyc sources` shows **`#*`** against PPS, with an estimated error **under 1 µs**
- The two temperatures differ by roughly 1 °C, with the BMP280 the higher

---

## If it never gets a fix

The most common cause is not a fault.

**Put the antenna outside, ceramic face up, board flat.** The patch antenna is strongly
directional — tipping it away from straight up cost most of the satellites on the
reference rig.

**A window is not enough, and here is why it looks like it should be.** The receiver needs
two different things from each satellite, and they have very different requirements.
*Seeing* one takes a weak signal, around 20 dB-Hz. *Learning where it is* needs about
30 dB-Hz held for **thirty uninterrupted seconds**. Behind glass you comfortably clear the
first and never the second — so the display fills up with satellites and the fix never
arrives, which reads as broken hardware and is not.

Any interruption restarts that thirty seconds. Move the antenna once, put it somewhere
with real sky, and leave it alone for a few minutes.

**Outdoors, permanently, the sensors need a radiation shield.** In sunlight an
unshielded sensor measures how hot the sun has made *it*, not the air — wrong by several
degrees, which is ten times its own specification. A stacked-plate shield with air moving
through it costs about 10 €.

---

## When the new receiver arrives

A genuine u-blox NEO-M8N is on order, and it brings PPS out on a proper header pin
instead of an LED pad. When you swap it in:

- **The chrony fix in step 4 still applies.** It is about chrony, not about the receiver.
  This is the item that carries over.
- The wiring, the overlay and the chrony reference-clock line are unchanged.
- **Re-check the speed.** 9600 is right for the current module; u-blox modules are
  configurable and yours may differ.
- **Re-run step 3 and note the numbers.** Expect the same or better.
- **Keep the old module.** For an event that happens once, a spare in the timing chain is
  worth more than it costs.

The full comparison is in [`FIELD-NOTES-TIMING.md`](FIELD-NOTES-TIMING.md) §7.
