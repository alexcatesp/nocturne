# Field Notes — timing and environmental sensors

**Hardware brought up on the reference rig, August 2026. Verified figures, not estimates.**

Same standing as [`FIELD-NOTES-M1.md`](FIELD-NOTES-M1.md): this document is evidence, not
instruction. Where it contradicts an assumption in SPEC.md, `install.sh` or an existing
ADR, the contradiction is the point — resolve it and record the resolution as an ADR.

This records hardware that now exists on the Nocturne Pi and that SPEC.md did not
describe. It is written for the implementing agent: what is fitted, how it is wired, what
was measured, and which parts of the spec it satisfies or changes.

Everything below was executed on the reference rig unless explicitly marked as pending.

> ## Repository note — four things that moved between writing and filing
>
> The body is the operator's text. These four are the editorial additions, kept here
> rather than woven into the evidence.
>
> **1. §6.1's GPS fix is redacted.** The document quoted the measured position to six
> decimal places. SPEC §2.3 keeps precise site coordinates out of this repository on
> purpose — *"a residential site to four decimal places is a home address to within
> metres, and this document sits beside an inventory of equipment left outside overnight
> and a schedule of when nobody is watching it."* That rule does not stop applying
> because the number arrived from a GPS receiver instead of a map. The measurement, the
> error against the shipped placeholder, and every conclusion drawn from it survive
> below; the position itself belongs in the operator's untracked
> `config/equipment.local.yaml` and nowhere else.
>
> **2. §6.1's premise is already fixed.** It says `config/equipment.yaml` declares the
> site's real position. It no longer does. [ADR 0013](decisions/0013-local-configuration-overrides.md)
> replaced it with the placeholder `45.0 / 0.0 / 100` and moved real coordinates to an
> untracked override — so the numbers §6.1 quotes as wrong are themselves gone from the
> repository, and are not reproduced here either. So there is no coordinate to correct in the repository — the
> correction is one the operator applies locally, and what §6.1 contributes is the
> *procedure*: a multi-hour average, at the mount position, geoid not ellipsoid.
>
> **3. §6.2 refers to a schema that does not exist yet.** `time_source` and
> `provenance.max_time_offset_ms` are specified in
> [Addendum A](SPEC-ADDENDUM-A.md) §A.8.4 and §A.9, which is M3 work. Nothing in the
> backend reserves the enum today. §6.2 is therefore a requirement on code not yet
> written, not a description of code that exists.
>
> **4. The three scripts in §5 are not in this repository.** `sensors_monitor.py`,
> `gps_monitor.py` and `uart_scan.py` were delivered to the rig and are referenced
> throughout, including in the appendix's verification commands. They are operator
> diagnostics rather than backend components, and filing them is a separate decision from
> filing this document.

---

## 0. Scope — read this first

Two separate subsystems were brought up in the same session. **They belong to different
projects and must not be conflated.**

| Subsystem | Belongs to | Status |
|---|---|---|
| GPS + PPS timing | **Nocturne.** Satisfies §A.8.4 | Fitted, verified |
| AHT20 + BMP280 (T / RH / P) | **Nocturne.** Feeds the §9.3 dew-point calculation | Fitted, verified |
| VEML7700 (illuminance) | **Not Nocturne.** Eclipse environmental logger | Prototyped only |

**The VEML7700 and the future TSL2591 are not Nocturne hardware.** They belong to a
standalone eclipse logger that will run on a separate Raspberry Pi Zero 2 W. They are
documented here only because they share the I²C bus during prototyping and because the
driver work is reusable. Do not add them to `equipment.yaml`, do not surface them in
telemetry, and do not add a dependency on them.

Specifically: sky brightness in Nocturne is derived photometrically from the frame zero
point (Addendum A §A.3), which explicitly supersedes a hardware sky-quality meter. A lux
sensor must not become an alternative path to that measurement.

---

## 1. GPIO and bus allocation

Fixed assignments on the Pi 5. Treat as reserved.

| Pin (physical) | GPIO | Function | Notes |
|---|---|---|---|
| 3 | GPIO2 | I²C SDA | AHT20, BMP280 (+ VEML7700 while prototyping) |
| 5 | GPIO3 | I²C SCL | |
| **7** | **GPIO4** | **PPS input** | `dtoverlay=pps-gpio,gpiopin=4` |
| 8 | GPIO14 | UART TX | **Deliberately NOT connected** — see §2.4 |
| 10 | GPIO15 | UART RX | GPS module TX |
| 12 | GPIO18 | *(reserved)* | PWM0, held for the §9.3 secondary dew heater |

GPIO4 was chosen for PPS specifically to leave GPIO18/PWM0 free for the dew heater. Do not
reassign either.

I²C addresses in use: `0x10` VEML7700, `0x38` AHT20, `0x77` BMP280. No conflicts, no
multiplexer required.

---

## 2. GPS receiver

### 2.1 What the module actually is

The board is silkscreened **GY-GPS6MV2** and the module carries a **u-blox NEO-6M-0-001**
label. **The label is not trustworthy.** Evidence:

- NMEA output uses talker IDs `$GN`, `$GP` and **`$BD`**. u-blox generation 6 is GPS-only
  and emits `$GP` exclusively; u-blox uses `$GB` for BeiDou, never `$BD`.
- `ubxtool -f /dev/ttyAMA0 -p MON-VER` returns nothing. A genuine u-blox answers.

Conclusion: re-marked silicon from another vendor (CASIC AT6558 or MediaTek MT3333 class),
which is common on loose GY-GPS6MV2 boards. Consequences:

- **Do not apply u-blox datasheet behaviour or UBX configuration.** It will not respond.
- Multi-constellation (GPS + BeiDou) is an advantage over a real NEO-6M.
- Timing quality is unaffected — see the measurements in §2.6.

The board does carry a 3.3 V regulator, a backup cell, a 24C32A EEPROM (so configuration
**survives power loss**; only removing the backup cell forces defaults) and a U.FL external
antenna connector.

### 2.2 Serial port — a Pi 5 trap

```
/dev/serial0 -> ttyAMA10     # debug UART on the 3-pin JST header. NOT the GPS.
/dev/ttyAMA0                 # GPIO14/15. This is the GPS.
```

On the Pi 4 `serial0` was the correct device; on the Pi 5 it is not. **Always configure
`/dev/ttyAMA0` explicitly.** Never use `/dev/serial0` for this device.

Baud rate: **9600**, confirmed by a checksum-validated scan across 4800–230400.

Required in `/boot/firmware/config.txt`:

```
enable_uart=1
dtoverlay=pps-gpio,gpiopin=4
```

And remove `console=serial0,115200` from `/boot/firmware/cmdline.txt` if present.

### 2.3 gpsd

`/etc/default/gpsd`:

```
DEVICES="/dev/ttyAMA0"
GPSD_OPTIONS="-n -b"
USBAUTO="false"
```

```bash
sudo systemctl disable gpsd.socket      # socket activation must be off
sudo systemctl enable --now gpsd
```

- **`-n`** is mandatory: without it gpsd does not poll the device until a client connects.
- **`-b`** is read-only mode: gpsd never writes to the device. **Required for this
  receiver.** On startup gpsd otherwise probes with u-blox, MediaTek and SiRF binary
  configuration packets, which on unidentified silicon can change the baud rate or switch
  the module to binary mode — persistently, because of the EEPROM.
- Socket activation left enabled produces a running service that nothing can connect to
  (`cgps: timeout contacting gpsd`).

An empty `DEVICES` logs only `Referenced but unset environment variable evaluates to an
empty string` and then runs happily doing nothing. Watch for that.

### 2.4 The UART TX line is deliberately absent

Connecting pin 8 (GPIO14/TX) to the module's RX broke NMEA reception entirely and produced
unparseable output at every baud rate. Removing it restored clean NMEA immediately.

**Leave the Pi's TX disconnected.** Nothing in Nocturne needs to configure the receiver:
1 Hz PPS at factory defaults is exactly what is wanted. One fewer wire is one fewer class
of failure in the subsystem the whole timing chain depends on.

### 2.5 PPS wiring

The 4-pin header (`VCC RX TX GND`) does **not** expose PPS. The signal is available at
**pin 3 of the module (TIMEPULSE)**, which drives the red LED through a series resistor.

⚠️ **Solder to the module side of the resistor, never the LED side.** On the LED side the
voltage is clamped by the diode forward drop to roughly 1.8–2.0 V, below the Pi's 2.31 V
(0.7 × 3.3) input-high threshold. It would work intermittently.

A 1 kΩ series resistor into GPIO4 is recommended as overvoltage insurance where the
TIMEPULSE level cannot be measured. It adds ~66 ns of rise time against a millisecond-scale
requirement. On the reference rig the link was made without it and the level proved correct.

**PPS is only produced once the receiver has a position fix.** No fix, no pulse, no LED.
Absence of PPS events with no fix is the expected result, not a fault.

### 2.6 Measured PPS quality

12 consecutive pulses via `ppstest /dev/pps0`, after removing the 1 Hz linear term:

| Quantity | Measured |
|---|---|
| Oscillator drift vs GPS | **−0.827 ppm** |
| Jitter, RMS | **2.04 µs** |
| Jitter, peak | 6.06 µs (single interrupt-latency outlier) |
| System clock error before PPS discipline | **4.10 ms** behind |

Requirement for eclipse contact timing is milliseconds. Margin is roughly three orders of
magnitude.

### 2.7 chrony

`chrony` is **not installed by default** on Raspberry Pi OS; `systemd-timesyncd` is, and it
cannot use reference clocks. Install chrony (the Debian package disables timesyncd
automatically).

`/etc/chrony/chrony.conf`, appended:

```
refclock PPS /dev/pps0 refid PPS
```

⚠️ **`/etc/default/chrony` must be changed:**

```
DAEMON_OPTS="-F -1"
```

**This is not optional and it fails silently.** Debian ships `-F 1`, enabling a seccomp
syscall filter that blocks the PPS ioctls. The symptom is `#? PPS` with `Reach 0`
indefinitely and **no error message anywhere in the journal** — chronyd opens the device
successfully and is then blocked on the ioctls. Several hours were lost to this. Record it
as an ADR: it is a security-relevant configuration change, and the chrony documentation
itself recommends it when reference clocks are used.

Verified result after that change:

```
MS Name/IP address    Stratum Poll Reach LastRx Last sample
#* PPS                      0    4    37     18  +256ns[+1399ns] +/-  155ns
^- tick.espanix.net         1    6    37     18 -1631us[-1630us] +/- 4385us
```

`#*` means chrony selected PPS as its reference and demoted all internet servers to `-`.
**Estimated error ±155 ns**, against ±21 ms before. Five orders of magnitude.

### 2.8 Permissions

`/dev/pps0` is `crw------- root root`. chronyd opens it before dropping privileges, so no
change is needed for chrony. **A non-root process — such as the Nocturne backend — cannot
read it.** If direct access is ever required:

```
# /etc/udev/rules.d/99-pps.rules
KERNEL=="pps[0-9]*", MODE="0660", GROUP="_chrony"
```

Preferred design: read timing state through `chronyc -c tracking`, not by opening
`/dev/pps0` directly. Keeps the backend unprivileged.

### 2.9 Open item — offline operation

**PPS marks the instant within a second; it does not say which second.** That label
currently comes from the internet NTP servers. At a remote site with no connectivity,
chrony has no way to identify the second and PPS alone is insufficient.

The fix is gpsd's shared-memory refclock:

```
refclock SHM 0 refid NMEA offset 0.200 delay 0.2 noselect
refclock PPS /dev/pps0 lock NMEA refid PPS
```

This was **not** brought up on the reference rig. It involves shared-memory permissions
between `gpsd` and `chronyd` and must be built and tested before any field deployment
without network. **Blocking for the 2027 eclipse site; not blocking for Nocturne at the
home site**, which has wired Ethernet.

---

## 3. Environmental sensors

### 3.1 AHT20 — temperature and humidity (authoritative)

I²C `0x38`. No register map: raw command sequences with CRC8 (polynomial 0x31, init 0xFF).

- Init `0xBE 0x08 0x00` after a 40 ms power-up wait; status bit `0x08` confirms calibration
- Measure `0xAC 0x33 0x00`, wait ≥80 ms, poll status bit `0x80` (busy), read 7 bytes
- Two 20-bit fields packed across 5 bytes:
  `RH% = raw_rh × 100 / 2²⁰` · `T°C = raw_t × 200 / 2²⁰ − 50`
- **Verify the CRC on every read.** A failed CRC means bus corruption — log a gap, never
  propagate the value.

Typical accuracy ±0.3 °C and ±2 % RH; repeatability measured at ±0.05 °C.

### 3.2 BMP280 — pressure only

I²C `0x77` (not `0x76` on this board — probe both). Chip ID at `0xD0` returns `0x58`
(`0x60` would be a BME280).

- Read 24 bytes of factory coefficients from `0x88`, unpack as `<HhhHhhhhhhhh`
- **Forced mode**, one conversion per sample, for deterministic timestamps:
  `ctrl_meas = 0x55` (T ×2, P ×16), `config = 0x08` (IIR ×4), ~43 ms conversion
- Apply the Bosch floating-point compensation from the datasheet appendix

**Validation:** 936.3 hPa measured at ~675 m elevation equals 1018 hPa at sea level, which
is consistent with an August Azores-high situation. Noise 0.02 hPa peak-to-peak.

Note the BMP280's absolute accuracy is specified over 950–1050 hPa. At the Tudela site the
station pressure sits just below that band. Relative accuracy (±0.12 hPa) is unaffected and
is what matters for trend work.

### 3.3 ⚠️ Use AHT20 for air temperature, never BMP280

Measured simultaneously on the same board:

```
AHT20   23.89 °C
BMP280  24.85 °C      +0.96 °C
```

The BMP280 self-heats. **The dew-point calculation required by §9.3 must use the AHT20
temperature and humidity.** Using the BMP280 temperature would bias the dew point by
roughly 1 °C and, given the spec's "keep the secondary above dew point" control target,
would under-heat the mirror in exactly the marginal conditions where it matters.

Any sensor pair fitted later must be checked the same way. This is a general property of
combined pressure/temperature parts, not a defect of this unit.

### 3.4 Radiation shield

**Mandatory for any outdoor deployment.** An unshielded sensor in sunlight measures its own
solar heating, not air temperature — an error of several degrees, i.e. ten times the sensor
specification. Stacked-plate shield with natural ventilation; approximately 10 € to build.

---

## 4. VEML7700 — reference only, not Nocturne hardware

See §0. Recorded here because the driver work is done and validated.

I²C `0x10`. Conversion is **not** a constant:

```
resolution [lux/count] = 0.0036 × (2 / gain) × (800 / IT_ms)
```

Above ~1000 lux the part is materially non-linear. Vishay's fourth-order correction
(from the application note *Designing the VEML7700 Into an Application*):

```
lux_corrected = 6.0135e-13·L⁴ − 9.3924e-9·L³ + 8.1488e-5·L² + 1.0023·L
```

At 10 000 apparent lux this yields 14 793 — a **48 % correction**. Omitting it deforms the
curve exactly where it matters.

Auto-ranging uses a ten-rung ladder of exact ×2 steps (gain × integration time), so the
required jump is a single logarithm rather than one rung per sample:

```
(1/8, 25) (1/8, 50) (1/8, 100) (1/8, 200) (1/4, 200)
(x1, 100) (x1, 200) (x2, 200)  (x2, 400)  (x2, 800)
```

Target window 2 000–30 000 counts. The 15× window against 2× rung spacing guarantees no
oscillation; verified over 40 samples at constant illumination.

Full scale spans 236 lux to 120 794 lux. **Full sun (~110 klux) sits at 91 % of the least
sensitive rung** — there is no headroom, and saturation is expected with bright reflective
surroundings. This is why the eclipse logger needs a second sensor (TSL2591) for the dark
end, where quantisation on the VEML7700 reaches 1.2 % at 0.3 lux.

---

## 5. Scripts

Delivered, syntax-checked, and validated against real captured data. They are operator
diagnostics, not backend components. **They are not in this repository** — see the
repository note at the top.

| Script | Purpose |
|---|---|
| `sensors_monitor.py` | 1 Hz table of AHT20 / BMP280 / VEML7700, optional CSV. Raw register access via `smbus2`; all conversion arithmetic visible, no vendor library |
| `gps_monitor.py` | 1 Hz GPS status from gpsd JSON or direct NMEA, with **incremental position averaging** (Welford) and reported scatter/standard error |
| `uart_scan.py` | Baud-rate scan with proper `tcflush` between rates and NMEA checksum scoring |

Dependency: `python3-smbus2` only.

⚠️ `gps_monitor.py` reports a standard error that assumes independent samples. **GPS errors
are strongly time-correlated over minutes**, so the figure is optimistic — treat it as a
lower bound. Simulation showed 1 h and 6 h of averaging giving identical scatter (3.0 m)
while the reported standard error fell from 5 cm to 2 cm, which is the signature of a
systematic that does not average down.

---

## 6. Changes this forces on SPEC.md

### 6.1 Site coordinates are wrong

> **Redacted, and already fixed — see repository notes 1 and 2.** This section was written
> against a `config/equipment.yaml` that still carried the site's real position. ADR 0013
> has since replaced it with a placeholder, and the operator's real coordinates live in an
> untracked `config/equipment.local.yaml`. Neither the measured fix nor the value it
> corrects is reproduced here: SPEC §2.3 keeps the site's precise position out of this
> repository, and a GPS receiver is not an exemption from that rule.

A GPS fix at the site landed **1.18 km** from the coordinates then shipped. That error
matters in a way that never announces itself: the longitude component alone was 0.0062°,
which is 1.5 s of local sidereal time — **22″ of RA**. Nothing fails; the ephemeris, the
altitude windows and the meridian timing are simply computed for somewhere else.

⚠️ **A single fix is not the answer.** The one taken had HDOP 2.51. Use a multi-hour
average from `gps_monitor.py`, taken at the actual mount position, and read §5's warning
about the reported standard error before trusting it.

⚠️ **Elevation is metres above the geoid, not above the ellipsoid.** INDI and KStars
expect the former. At this site the two differ by **51.7 m** — the geoid undulation over
Castilla. Confusing them produces no error anywhere, just a 52 m altitude offset, and the
receiver will happily report both.

### 6.2 §A.8.4 — `gps_pps` is now real

The schema reserved `time_source` including `gps_pps` for future work. It is fitted and
verified. FITS headers should now report `gps_pps` with a measured offset from
`chronyc tracking`, and `provenance.max_time_offset_ms: 100` is met by four orders of
magnitude.

Science-grade flagging should assert that chrony's selected reference is PPS, not merely
that NTP is synchronised — `chronyc -c tracking` field 0 is the reference ID.

### 6.3 §9.3 — local dew point source is available

The dew-heater controller can compute dew point from the local AHT20 instead of a weather
API. This removes a network dependency from a control path, which is preferable under
design principle 4 (fail safe, not fail open). Add the AHT20 to `equipment.yaml` as an
environmental sensor and use its temperature and humidity, **not** the BMP280 temperature
(§3.3).

### 6.4 New ADRs required

1. **chrony seccomp disabled (`-F -1`).** Security-relevant, non-obvious, and silent on
   failure.
2. **gpsd read-only mode (`-b`) and UART TX left unconnected.** Consequence of an
   unidentified receiver; also applies as good practice to the replacement.

*Both are written: [ADR 0014](decisions/0014-chrony-seccomp-disabled.md) and
[ADR 0015](decisions/0015-gpsd-read-only-and-tx-unconnected.md).*

---

## 7. Applicability to the replacement receiver

A genuine u-blox NEO-M8N with a documented PPS pin and a 3 m active antenna is on order.
Everything above still applies, with these differences:

| Item | Carries over? |
|---|---|
| `dtoverlay=pps-gpio,gpiopin=4`, GPIO4 wiring | Yes, unchanged |
| chrony `-F -1` seccomp fix | **Yes — this is the important one** |
| chrony `refclock PPS` configuration | Yes, unchanged |
| gpsd `DEVICES=/dev/ttyAMA0`, `-n`, socket disabled | Yes |
| gpsd `-b` read-only | Optional. A genuine u-blox tolerates probing; keeping `-b` is still the conservative choice |
| Pi TX left disconnected | Optional. Connect only if UBX configuration is actually wanted |
| PPS available on a header pin | **Yes — no soldering to an LED pad** |
| Measured jitter figures | Re-measure. Expect equal or better |
| 9600 baud | **Re-check.** u-blox modules commonly default to 9600 but are configurable |

Keep the current module as a spare. For a single irreproducible event, redundancy in the
timing chain is worth more than the 30 € it costs.

---

## Appendix: verification commands

```bash
# I²C
i2cdetect -y 1                       # expect 10, 38, 77
./sensors_monitor.py                 # 1 Hz table

# GPS
gpspipe -r -n 20 | grep -E "GGA|GSV" # fix quality is GGA field 6; GSV field 4 is SNR
./gps_monitor.py --csv position.csv --chrony

# PPS
cat /sys/class/pps/pps0/name         # expect pps@4.-1
sudo ppstest /dev/pps0               # assert, sequence incrementing at 1 Hz
chronyc sources -v                   # expect '#*' on PPS
chronyc tracking                     # expect sub-microsecond RMS offset
```

**Pass criteria:** three I²C devices respond; NMEA arrives with valid checksums; GGA field 6
is 1 or better; `ppstest` shows monotonic sequence numbers at 1 Hz; `chronyc sources` shows
`#*` against PPS with estimated error below 1 µs.

Two notes on field SNR, from bring-up experience. A patch antenna is strongly directional:
**ceramic face up, board horizontal.** Rotating it away from the zenith cost most of the
tracked satellites. And tracking a satellite works from ~20 dB-Hz, but **decoding ephemeris
needs ~30 dB-Hz held for 30 uninterrupted seconds per satellite** — which is why a receiver
behind glass shows many satellites and never fixes. Moving the antenna restarts that clock.
