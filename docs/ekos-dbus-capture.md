# Capturing the Ekos DBus interface

**For the operator, on the Pi, against the live KStars build.**
Closes the evidence gap in [issue #2](https://github.com/alexcatesp/nocturne/issues/2).

Every Ekos DBus method name in `backend/nocturne/executor/ekos.py` is a **guess**. They
were written without a KStars to ask. `backend/tests/fixtures/fake_kstars.py` is a stub
written by the same author as the bridge it verifies, so the two share whatever
misconception is in them, and the tests that pass against it prove only that the bridge
is consistent with its own assumptions.

This procedure replaces the assumptions with what the machine actually says. It reads;
it does not command.

---

## Before you start

**Nothing here moves any equipment, and it must stay that way.**

- **Leave the mount powered off**, or unplug the USB. Not because a command is issued —
  none is — but because the cost of being wrong about that is a slew into a tripod leg,
  and the cost of powering it down is nothing.
- If you would rather leave the rig as it is, that is fine too. **Do not select your real
  equipment profile in step 4.** `Simulators` is the profile named below and it touches
  no hardware.

**Time:** 15 minutes, most of it waiting for KStars to start.

**One prerequisite,** if `gdbus` is not already there:

```bash
sudo apt install -y libglib2.0-bin
```

Everything else is already installed.

---

## 1. Somewhere to put the output

```bash
mkdir -p ~/ekos-capture && cd ~/ekos-capture
kstars --version > kstars-version.txt 2>&1
cat kstars-version.txt
```

**Expected:** a line containing `3.8.3`. If it says `3.6.x` you are running Debian's
packaged KStars rather than the one you built — stop and check `command -v kstars`.

---

## 2. Start KStars headless, on its own session bus

KStars needs a session bus and a display. `dbus-run-session` supplies the first and Qt's
`offscreen` platform plugin removes the need for the second — the same pair
`scripts/systemd/nocturne-kstars.service` uses in production.

Run this and **stay in the shell it gives you**; every later step needs the same bus.

```bash
cd ~/ekos-capture
QT_QPA_PLATFORM=offscreen dbus-run-session -- bash
```

You are now in a normal-looking shell that has a private session bus. Inside it:

```bash
kstars > kstars.log 2>&1 &
gdbus wait --session --timeout 120 org.kde.kstars && echo "ON THE BUS"
```

**Expected:** `ON THE BUS`, within about 20 seconds.

If `gdbus wait` is not supported by your glib, this does the same job:

```bash
for i in $(seq 60); do
  gdbus call --session --dest org.kde.kstars --object-path /KStars \
    --method org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1 && { echo "ON THE BUS"; break; }
  sleep 2
done
```

**If it never appears**, send me `kstars.log` and stop here. That log is the answer.

---

## 3. Capture — the part that matters most

### 3a. What names KStars actually registers

```bash
gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
  --method org.freedesktop.DBus.ListNames > dbus-names.txt
grep -o 'org\.kde\.[a-zA-Z0-9._-]*' dbus-names.txt | sort -u
```

**Expected:** `org.kde.kstars`.

**Observed on the reference rig, 2026-08-07: `org.kde.kstars`, and nothing else.** One
fixed well-known name, no per-process `org.kde.kstars-<pid>`. That settles a real
question rather than a formality: `backend/nocturne/executor/ekos.py` attaches to a fixed
name, and a KStars that registered only a per-process one would fail in a way that reads
as "KStars is not running". It does not. This is the one assumption in the bridge that
is now verified against the hardware.

Re-check it if the KStars pin ever moves — it is a property of the build, not of DBus.

### 3b. `--recurse` does not do what it looks like it does

**Do not use `--xml` and `--recurse` together.** They do not compose: `--xml` prints the
introspection of the *one* object it was given, and `--recurse` only affects the
human-readable output mode. Asking for both silently gives you the first.

That was found the slow way. On the reference rig, 2026-08-07:

```bash
gdbus introspect --session --dest org.kde.kstars --object-path / --recurse --xml \
  > kstars-at-rest.xml     # 570 bytes, 3 methods
```

570 bytes reads like a failure and is not one — it is the root node's own XML, correctly
returned, carrying `Introspect`, `Ping`, `GetMachineId` and **two child nodes**:

```xml
<node name="KStars"/>
<node name="kstars"/>
```

The children were advertised the whole time. Nothing was wrong with KStars, with the bus
or with the root node; the command asked for one object and got one object. Enumerate the
paths and introspect each of them — 3c.

### 3c. Enumerate the paths, then introspect each one

```bash
busctl --user tree org.kde.kstars
```

**Observed at rest** — Ekos *not* started — with the `/kstars/MainWindow_1/actions/…`
branch elided, because it is several hundred KXMLGui menu-action objects and none of them
is an interface Nocturne would ever call:

```
├─ /KStars
│ ├─ /KStars/Ekos
│ │ └─ /KStars/Ekos/Scheduler
│ ├─ /KStars/FOV/1 … /5
│ ├─ /KStars/INDI
│ └─ /KStars/SimClock
└─ /kstars
  └─ /kstars/MainWindow_1/actions/…      (menu actions — ignore)
```

**Note what is absent.** No `/KStars/Ekos/Capture`, `/Focus`, `/Guide`, `/Align`,
`/Mount` or optical-train object. Those are created when Ekos is *started*, which is why
this procedure has two passes rather than one. `Scheduler` existing at rest is the
exception, not the pattern.

```bash
for path in /KStars /KStars/Ekos /KStars/Ekos/Scheduler /KStars/INDI /KStars/SimClock; do
  echo "===== ${path}"
  gdbus introspect --session --dest org.kde.kstars --object-path "${path}" --xml
done > kstars-live-at-rest.xml
grep -c '<method name=' kstars-live-at-rest.xml
```

### 3d. The source tree declares the same interfaces, and needs no running KStars

KStars generates its DBus adaptors from interface XML checked into the repository. Those
files *are* the interface definition, in the format wanted here, and they are on the Pi
already — **nineteen of them**, found at the pinned commit on 2026-08-07 under
`~/.cache/nocturne-build/kstars/kstars/`:

```
org.kde.kstars.xml                        org.kde.kstars.INDI.xml
org.kde.kstars.Ekos.xml                   org.kde.kstars.INDI.Dome.xml
org.kde.kstars.Ekos.Align.xml             org.kde.kstars.INDI.DustCap.xml
org.kde.kstars.Ekos.Capture.xml           org.kde.kstars.INDI.GenericDevice.xml
org.kde.kstars.Ekos.Focus.xml             org.kde.kstars.INDI.LightBox.xml
org.kde.kstars.Ekos.Guide.xml             org.kde.kstars.INDI.PAC.xml
org.kde.kstars.Ekos.Mount.xml             org.kde.kstars.INDI.Weather.xml
org.kde.kstars.Ekos.Observatory.xml       org.kde.kstars.FOV.xml
org.kde.kstars.Ekos.OpticalTrain.xml      org.kde.kstars.SimClock.xml
org.kde.kstars.Ekos.Scheduler.xml
```

**`org.kde.kstars.Ekos.OpticalTrain.xml` is there.** That is the interface ADR 0008 and
ADR 0006 both cite as the reason for requiring KStars >= 3.8.2 — now confirmed present in
the tree that was actually built, rather than taken from release notes.

To collect them into one file to send back:

```bash
cd ~/.cache/nocturne-build/kstars/kstars
for f in org.kde.kstars*.xml; do echo "===== $f"; cat "$f"; done \
  > ~/ekos-capture/kstars-dbus-interfaces.xml
```

To re-derive the list on a different build:

```bash
find ~/.cache/nocturne-build/kstars -name 'org.kde.kstars*.xml' | sort
```

This is evidence of the same kind as an introspection dump — generated from the tree that
was built, not written by the author of the bridge — and it does not depend on
introspection working at all. **If step 3b came back small, run this: it may be the whole
answer.**

**One of 3b, 3c or 3d is enough to unblock issue #2.** If anything below goes wrong, stop
and send me whichever of them produced something.

---

## 4. Start Ekos, then capture again

Ekos creates its per-module DBus objects — capture, focus, guide, align, mount, scheduler
and the optical trains — **when Ekos is started**, not when KStars is. So the file from
step 3b does not contain them, and the second capture is the one that has them.

**The method names below are no longer guesses.** They were read out of the capture from
step 3c: `org.kde.kstars.Ekos` declares `getProfiles() -> as`, `setProfile(s) -> b` and
`start()`, and `start` is annotated NoReply — it returns immediately and says nothing.

Ask which profiles exist rather than assuming one is called `Simulators`:

```bash
gdbus call --session --dest org.kde.kstars --object-path /KStars/Ekos \
  --method org.kde.kstars.Ekos.getProfiles
```

**Pick a simulator profile from what that prints.** If there is no simulators profile,
stop and say so — do not select the profile that holds the real equipment.

```bash
gdbus call --session --dest org.kde.kstars --object-path /KStars/Ekos \
  --method org.kde.kstars.Ekos.setProfile "Simulators"

gdbus call --session --dest org.kde.kstars --object-path /KStars/Ekos \
  --method org.kde.kstars.Ekos.start
```

`setProfile` returns `(true,)` on success and `(false,)` if the name is not a profile —
**check it**, because `start` will happily start whatever profile is currently selected.

`start` returns nothing at all, so ask Ekos whether it came up rather than assuming:

```bash
gdbus call --session --dest org.kde.kstars --object-path /KStars/Ekos \
  --method org.freedesktop.DBus.Properties.Get org.kde.kstars.Ekos ekosStatus
```

Then give the modules a moment and capture again:

```bash
sleep 20
busctl --user tree org.kde.kstars | grep -i -E 'ekos|train'
```

Introspect each path that appears — **not** `/` with `--recurse`, for the reason in 3b:

```bash
# grep -o, not tr: busctl draws the tree with box characters, not | and -.
# Capital K also excludes the /kstars/MainWindow_1 menu-action branch.
busctl --user tree org.kde.kstars | grep -o '/KStars[^ ]*' | sort -u > ekos-paths.txt
while read -r path; do
  echo "===== ${path}"
  gdbus introspect --session --dest org.kde.kstars --object-path "${path}" --xml
done < ekos-paths.txt > kstars-ekos-running.xml
grep -c '<method name=' kstars-ekos-running.xml
```

**Expected:** paths under `/KStars/Ekos/` that were absent at rest — `Capture`, `Focus`,
`Guide`, `Align`, `Mount`. An **optical train** object appearing there is the thing ADR
0008 justified the whole Trixie decision for; its interface is declared in the built tree
already, and this would be it exported.

---

## 5. Shut down cleanly

```bash
pkill -TERM kstars
sleep 5
pgrep -a kstars || echo "stopped"
exit          # leaves the dbus-run-session shell; the private bus goes with it
```

A signal, not a DBus call. An earlier version of this document said
`org.kde.kstars.KStars.quit`, and the capture shows there is no such interface: `/KStars`
carries `org.kde.kstars` and `org.kde.KMainWindow`, and neither declares `quit`. It was a
guess of exactly the kind this whole procedure exists to remove.

`exit` is not optional housekeeping — the session bus lives only as long as that shell,
and leaving it open leaves a KStars running.

---

## 6. What to send back

Everything in `~/ekos-capture`:

```bash
cd ~ && tar czf ekos-capture.tar.gz ekos-capture/
ls -la ekos-capture.tar.gz
```

| File | Why |
|---|---|
| `kstars-dbus-interfaces.xml` | **the essential one** — all nineteen declared interfaces, from the built tree (3d) |
| `kstars-live-at-rest.xml` | what is actually exported before Ekos starts (3c) |
| `kstars-ekos-running.xml` | the Ekos modules, if step 4 worked |
| `dbus-names.txt` | whether the bus name is fixed or per-process |
| `kstars-version.txt` | ties the capture to a build |
| `kstars.log` | says why, when something did not work |

The first two answer different questions and are both wanted. The source XML is what
KStars **declares**; the live introspection is what it **exports at that moment**. A
method in the first and not the second is not a contradiction — it is an object that has
not been created yet — but a method in neither is a guess in `ekos.py` that has to go.

They go into `backend/tests/fixtures/hardware/`, alongside the INDI property dumps, and
get the same standing: recorded from the machine, not written by the author of the code
they test. `fake_kstars.py` is then rebuilt from the XML rather than from assumptions,
and every guessed method name in `ekos.py` is replaced by one that was read off a live
interface or deleted.

---

## What this does not do

It does not run a session, connect the real equipment, or exercise any Ekos module.
Capturing the interface is evidence gathering for M1; **using** it — align, focus, guide,
capture, flip — is M2, and none of it starts until this is in the repository and issue #2
is closed against it.
