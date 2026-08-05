# Hardware setup and the M1 hardware test

SPEC section 14, milestone M1. This is the HITL half of M1: the AUTO half runs
in CI against the INDI simulators and is already green.

> **This is the highest-risk item in the project.** Some NINA users report the
> Wave 150i connecting only through the SynScan serial bridge. If the direct
> connection fails here, the fallback is the WiFi driver, and that outcome gets
> recorded as an ADR before any further work (SPEC section 14, M1).

---

## 1. What to connect

Everything on USB to the Pi 5, everything on its own 12 V supply as usual:

| Device | Connection | Notes |
|---|---|---|
| Sky-Watcher Wave 150i | **USB serial, direct to the Pi** | No SynScan app, no phone, no WiFi. This is the point of the test. |
| ZWO ASI533MM Pro | USB 3 | Its own 12 V for the TEC |
| ZWO EFW 8×1.25" | USB | |
| ZWO EAF | USB | |
| ZWO ASI120MM Mini | USB 2 | |

Mount powered and roughly polar aligned. Counterweight fitted as you normally
fit it. Do this in daylight, on the terrace, with the tube pointed somewhere
harmless — you are going to slew.

---

## 2. Install

```bash
git clone https://github.com/alexcatesp/nocturne.git
cd nocturne
./scripts/install.sh
```

It builds INDI, the ZWO drivers, StellarSolver and KStars from source. Budget
one to three hours on a Pi 5, mostly KStars. It is safe to interrupt and re-run.

Then:

```bash
./scripts/install.sh --check
```

Everything should report `[ ok ]`. In particular `INDI 2.2.3` or later — the
release that added Wave 150i home indexer support to `indi_eqmod`.

---

## 3. Find the mount's serial port

With the mount powered and plugged in:

```bash
ls -l /dev/serial/by-id/
dmesg | tail -20
```

You are looking for a CH340, CP210x or FTDI adapter appearing as `/dev/ttyUSB0`
(occasionally `/dev/ttyACM0`). If nothing appears, the cable or the mount's USB
port is the problem, not the software.

If the port is not `/dev/ttyUSB0`, put the real one in `config/equipment.yaml`
under `mount.port`, and prefer the stable path:

```yaml
mount:
  port: "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
```

Then confirm the configuration still loads:

```bash
.venv/bin/nocturne check-config
```

---

## 4. The test itself

### 4.1 Start the drivers

```bash
indiserver -v indi_eqmod_telescope indi_asi_ccd indi_asi_focuser indi_asi_wheel
```

Leave it running and watch it. In a second terminal:

```bash
.venv/bin/python - <<'PY'
import asyncio
from nocturne.executor import IndiClient, IndiSettings

async def main():
    async with IndiClient(IndiSettings()) as client:
        await asyncio.sleep(5)
        for device in client.devices():
            print(device)
            await client.connect_device(device)
            print("   connected:", client.is_device_connected(device))

asyncio.run(main())
PY
```

### 4.2 What counts as a pass

**The mount, which is the whole point:**

- `indi_eqmod_telescope` connects over `/dev/ttyUSB0` with **no SynScan app
  running anywhere**, and reports `CONNECTION.CONNECT = On`.
- `EQUATORIAL_EOD_COORD` shows RA and Dec, and the numbers **change while
  tracking is on** and hold still while it is off.
- A slew to a named target arrives: `TELESCOPE_TRACK_STATE` goes Busy, then Ok,
  and the tube is pointing where you asked.
- A sync accepts new coordinates and `EQUATORIAL_EOD_COORD` reads them back.
- Pulse guide: writing `TELESCOPE_TIMED_GUIDE_NS` produces a small, visible
  correction. It does not have to be accurate — it has to *happen*.

**The rest:**

- **ASI533MM Pro** — cools towards −10 °C and holds it, and captures a light
  frame that lands on disk with sensible ADU values.
- **EFW** — cycles through all eight positions and reports each one back. Slots
  6, 7 and 8 are empty; they should still be reachable.
- **EAF** — moves to an absolute position, reports arriving there, and
  `FOCUS_TEMPERATURE` reads a plausible ambient temperature.
- **ASI120MM Mini** — streams frames.

**Pass = all five devices connect, and the mount does all four of slew, track,
sync and pulse guide over direct USB serial.**

### 4.3 If the mount will not connect directly

Stop. Do not work around it. Record what happened:

1. The exact `indiserver -v` output when the connection is attempted.
2. What `/dev/serial/by-id/` shows.
3. Whether the mount connects at all through the SynScan bridge, to establish
   that the cable and the port are fine.
4. Whether a different baud rate helps (`mount.baud`, default 115200).

Then the fallback is the WiFi driver, `indi_eqmod_telescope` over TCP or the
Sky-Watcher WiFi driver, and **that decision is written up as an ADR in
`docs/decisions/` before any further work** (SPEC section 14, M1). WiFi on a
terrace is a worse link than a cable and the decision deserves to be recorded
rather than absorbed.

---

## 5. After it passes

1. Do the **meridian calibration** in
   [`meridian-calibration.md`](meridian-calibration.md). Twenty minutes in
   daylight. Until it is done, Nocturne refuses unattended operation.
2. Note anything that needed changing in `config/equipment.yaml` — port, baud,
   device labels — so the next session starts from a file that already works.
3. M2 is polar alignment, plate solving, autofocus, guiding and the meridian
   flip, and it needs the measured meridian limits from step 1.

---

## Known constraints on this rig

- **No tripod extension.** The tube reaches the tripod legs near the meridian.
  See the meridian calibration; this is the single most dangerous thing about
  the setup.
- **SD card, not NVMe.** SPEC section 2.2 lists the NVMe HAT and SSD as a
  required purchase. Stacking write loads will kill an SD card. M1 does not
  stack, so it is not blocking yet — M3 is where it starts to matter.
- **No secondary dew heater.** SPEC section 9.3 makes this blocking for M6:
  unattended operation is refused without an INDI-controllable dew heater,
  because a fogged Newtonian secondary has no sharp symptom and costs the second
  half of the night.
- **Rain is yours to watch.** There is no observatory and parking does not
  protect the equipment (SPEC section 9.4).
