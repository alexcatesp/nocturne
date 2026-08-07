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

**Please look at this output and tell me what it says.** If it also prints something like
`org.kde.kstars-1234`, that matters more than it looks: the bridge attaches to a fixed
name, and a version of KStars that only registers a per-process one would make it fail
in a way that looks like KStars not running.

### 3b. The whole object tree, before Ekos is started

```bash
gdbus introspect --session --dest org.kde.kstars --object-path / --recurse --xml \
  > kstars-at-rest.xml
wc -c kstars-at-rest.xml
grep -c '<method name=' kstars-at-rest.xml
```

**Expected:** tens of kilobytes and well over a hundred methods.

**Observed on the reference rig, 2026-08-07: 570 bytes and 3 methods.** The recursion did
not descend — three methods is `Introspect`, `Ping` and `GetMachineId`, which is what the
root node answers when it advertises no children. `--recurse` walks `<node name="…"/>`
entries, and there were none to walk. Do not conclude that KStars publishes nothing; it
means `/` is not a route to what it publishes. Go to 3c.

### 3c. When `/` says nothing — enumerate the paths instead

```bash
busctl --user tree org.kde.kstars > dbus-tree.txt; cat dbus-tree.txt
```

`busctl` walks the tree itself rather than trusting one node to describe it. If that lists
object paths, introspect each of them directly:

```bash
grep -o '^ *[|`]*-*/.*' dbus-tree.txt | tr -d ' |`-' | sort -u > object-paths.txt
while read -r path; do
  echo "=== ${path}"
  gdbus introspect --session --dest org.kde.kstars --object-path "${path}" --xml
done < object-paths.txt > kstars-objects.xml
grep -c '<method name=' kstars-objects.xml
```

If `busctl` lists nothing either, try the paths KStars is expected to use, and let the
failures tell you which are real:

```bash
for path in /KStars /kstars /KStars/INDI /KStars/Ekos /KStars/Ekos/Scheduler; do
  echo "=== ${path}"
  gdbus introspect --session --dest org.kde.kstars --object-path "${path}" --xml 2>&1 | head -3
done
```

### 3d. The source tree says the same thing, and does not need a running KStars

KStars generates its DBus adaptors from interface XML checked into the repository. Those
files *are* the interface definition, in the format wanted here, and they are on the Pi
already:

```bash
find ~/.cache/nocturne-build/kstars -name '*.xml' \
  \( -name 'org.kde.kstars*' -o -path '*dbus*' \) | sort
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

Starting Ekos needs a method name, and the method names are exactly what is unverified
here. So this step may fail, and that is an acceptable outcome:

```bash
gdbus call --session --dest org.kde.kstars --object-path /KStars/Ekos \
  --method org.kde.kstars.Ekos.setProfile "Simulators"

gdbus call --session --dest org.kde.kstars --object-path /KStars/Ekos \
  --method org.kde.kstars.Ekos.start
```

**If either says `No such method` or `No such interface`** — stop, do not experiment.
`kstars-at-rest.xml` already contains the real names, and I will read them out of it and
send you back the exact two commands. That is one round trip and it is the correct one:
guessing at method names on a live rig is how something gets commanded by accident.

**If they succeed,** give the modules a moment and capture again:

```bash
sleep 20
gdbus introspect --session --dest org.kde.kstars --object-path / --recurse --xml \
  > kstars-ekos-running.xml
busctl --user tree org.kde.kstars | grep -i -E 'ekos|train' | head -40
```

**Expected:** `kstars-ekos-running.xml` is substantially larger than `kstars-at-rest.xml`,
and the tree lists objects under `/KStars/Ekos/`. An **optical train** object or interface
appearing anywhere in that output is the thing ADR 0008 justified the whole Trixie
decision for — if you see one, say so.

---

## 5. Shut down cleanly

```bash
gdbus call --session --dest org.kde.kstars --object-path /KStars \
  --method org.kde.kstars.KStars.quit 2>/dev/null || pkill -TERM kstars
sleep 5
pgrep -a kstars || echo "stopped"
exit          # leaves the dbus-run-session shell; the private bus goes with it
```

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
| `kstars-at-rest.xml` | **the essential one** — every object and method KStars publishes |
| `kstars-ekos-running.xml` | the Ekos modules, if step 4 worked |
| `dbus-names.txt` | whether the bus name is fixed or per-process |
| `kstars-version.txt` | ties the capture to a build |
| `kstars.log` | says why, when something did not work |

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
