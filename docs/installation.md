# Nocturne — Installation Guide

**From a fresh Raspberry Pi OS Trixie boot to a validated instrument stack.**

This guide is written to be followed in order, on the Pi itself. Every command has been
executed on the reference rig. Where something is known to fail, the failure and its fix
are inline rather than in a troubleshooting appendix — you should never have to search
for the answer to a problem you are currently having.

**Total time:** 4–6 hours, most of it unattended compilation.

> Commands prefixed `sudo` need administrator rights. If you are running as the
> unprivileged `nocturne-dev` user (see §7), switch to your normal user for those.

---

## 0. What you need before starting

- Raspberry Pi 5 (8 GB) with Raspberry Pi OS **64-bit Trixie (Debian 13) Lite**
- At least **20 GB free** on the root filesystem
- Wired Ethernet to the router (WiFi will work but is not recommended)
- The mount, powered, on a table — **without the OTA fitted and without the
  counterweight**
- SynScan closed on every phone, tablet and computer. An open SynScan app holds the
  serial port and the mount will not connect.

**Why Trixie and not Bookworm:** KStars 3.8.x is built on Qt6 and KDE Frameworks 6.
Bookworm (Debian 12) ships KF5, so building KStars there would require building Qt6 and
the entire KF6 tree from source — days of work that usually ends in failure. Trixie ships
KF6 packaged. This is not a preference; it is the difference between a build that
completes and one that does not.

---

## 1. First boot checks

```bash
uname -m                    # must print: aarch64
timedatectl                 # timezone correct, "System clock synchronized: yes"
df -h /                     # note the available space
vcgencmd measure_temp
```

**NTP synchronisation is not cosmetic.** Without it there is no reliable timestamp on any
frame, and every scientific output downstream is invalid. If `System clock synchronized`
reads `no`, fix that before continuing.

Set the timezone if needed:

```bash
sudo timedatectl set-timezone Europe/Madrid
```

---

## 2. Update the system first

Update before compiling, never during. Building against one set of libraries and then
upgrading them underneath is a straightforward way to lose two hours of work.

```bash
sudo apt update && sudo apt full-upgrade
sudo reboot        # if the kernel was updated
```

> **If apt reports 404 errors on package downloads**, the package index is stale — the
> versions apt has recorded no longer exist in the pool. `sudo apt update` fixes this in
> almost every case. If it persists, the CDN node is out of sync:
> ```bash
> sudo apt clean
> sudo rm -rf /var/lib/apt/lists/*
> sudo apt update
> ```
> If it still persists, edit `/etc/apt/sources.list.d/debian.sources` and replace
> `deb.debian.org` with a national mirror such as `ftp.es.debian.org`.

---

## 3. Protect the storage

**Skip this section only if you are booting from NVMe or SSD.** On microSD it is
essential — compilation is the most write-intensive thing this system will ever do.

```bash
# No swap file on the card
sudo systemctl disable --now dphys-swapfile

# Logs in RAM
sudo sed -i 's/^#*Storage=.*/Storage=volatile/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald

# /tmp in RAM
echo 'tmpfs /tmp tmpfs defaults,noatime,size=2G 0 0' | sudo tee -a /etc/fstab
```

Then add `noatime` to the root filesystem line in `/etc/fstab` and reboot.

### Compressed swap in RAM

Disabling the swap file removes the safety net that stops the compiler being killed when
memory runs out. Restore it without writing to the card:

```bash
sudo apt install zram-tools
echo -e "ALGO=zstd\nPERCENT=50" | sudo tee -a /etc/default/zramswap
sudo systemctl restart zramswap
free -h                     # expect roughly 4 GB of swap
```

> **If a build dies with no error message**, or a binary that should exist is reported
> missing at install time, check for the out-of-memory killer:
> ```bash
> dmesg | grep -i -E "killed process|out of memory"
> ```
> If it fired, rebuild with `-j1` instead of `-j2`.

---

## 4. Verify what the distribution provides

Run this before building anything. If a newer package appears in a future Debian release,
it may save you hours.

```bash
apt-cache policy libindi-dev indi-bin kstars libstellarsolver-dev
```

**As of Trixie (verified August 2026), everything must be built from source:**

| Package | Trixie ships | Nocturne needs |
|---|---|---|
| libindi-dev / indi-bin | 1.9.9 | ≥ 2.2.4 |
| kstars | 3.6.2 | ≥ 3.8.3 |
| libstellarsolver-dev | 2.6 | 2.8 |

**Do not add the INDI PPA.** It is built for Ubuntu; adding it to Debian is a known way
to break the system. There is no official apt repository for INDI on Debian ARM.

**In particular, do not use Debian's `indi-bin` for the mount test.** Support for the
Sky-Watcher Wave 150i arrived in the INDI 2.2.x series. A test against 1.9.9 would fail
for reasons unrelated to your hardware, and might wrongly send you down the WiFi fallback
path.

---

## 5. Build dependencies

**Install all of them now, before any build.** Not per component: the first end-to-end
run of the installer failed five times because dependencies were installed by the stage
that needed them, and by then the *previous* stage had already stopped. Each failure cost
an hour of compilation to reach a two-second `apt-get` (`docs/FIELD-NOTES-M1.md` §14).

If you are following this guide by hand rather than running `scripts/install.sh`, the
one list that is guaranteed to match what the installer uses is the installer's own:

```bash
./scripts/install.sh --check-packages
```

It prints nothing when every name is available on this release. The full set is:

```bash
sudo apt install -y --no-install-recommends \
  git cmake build-essential pkg-config \
  libnova-dev libcfitsio-dev libusb-1.0-0-dev zlib1g-dev libgsl-dev \
  libjpeg-dev libcurl4-gnutls-dev libtheora-dev libfftw3-dev \
  libev-dev libavcodec-dev libavdevice-dev libraw-dev libgphoto2-dev \
  libftdi1-dev libdc1394-dev libgps-dev librtlsdr-dev \
  python3-venv python3-dev siril \
  wcslib-dev libeigen3-dev libopencv-dev
```

The last three are the ones that are easy to miss and expensive to discover:

| Package | Needed by | Notes |
|---|---|---|
| `wcslib-dev` | StellarSolver, then KStars | stopped the first real run first |
| `libeigen3-dev` | StellarSolver, then KStars | same stage |
| `libopencv-dev` | KStars | requires >= 4.6.0, about 500 MB installed |

Qt6 and KF6 are in §10.1 — they are needed for StellarSolver and KStars, not for INDI,
and they are the largest part of the list.

---

## 6. Build INDI

### 6.1 Core library

```bash
mkdir -p ~/src && cd ~/src
git clone --depth 1 --branch v2.2.4 https://github.com/indilib/indi.git
cd indi
```

**Before configuring, remove the forced `-Werror`.** INDI v2.2.4 predates the GCC shipped
with Trixie, and the newer compiler emits warnings the project treats as fatal. At least
`indi_astrotrac_telescope` fails on `-Werror=stringop-overread`.

Note that `-DCMAKE_CXX_FLAGS="-Wno-error"` **does not work** — the project appends
`-Werror` after your flags, so its setting wins. The flag must be removed at source:

```bash
grep -n "Werror" cmake_modules/CMakeCommon.cmake
```

Find the line that reads `SET(COMP_FLAGS "${COMP_FLAGS} -Werror")` — line 70 in v2.2.4 —
and strip the flag:

```bash
sed -i '70s/ -Werror//' cmake_modules/CMakeCommon.cmake
sed -n '68,72p' cmake_modules/CMakeCommon.cmake     # confirm
```

Leave any `CHECK_C_COMPILER_FLAG(...)` lines alone. Those are compiler capability probes,
not build flags.

Now build. **Chain build and install with `&&`** so a failed build cannot be followed by a
partial install:

```bash
rm -rf build
cmake -B build -S . -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2 && sudo cmake --install build
```

30–45 minutes on a Pi 5. Warnings will scroll past; that is correct — you see them, they
no longer kill the build.

Verify and reclaim space:

```bash
pkg-config --modversion libindi          # must print 2.2.4
rm -rf ~/src/indi/build
df -h /
```

> If `pkg-config` reports 1.9.9, Debian's package is installed and shadowing the build.
> Remove it with `sudo apt remove libindi-dev indi-bin` and reinstall from source.

### 6.2 Third-party drivers

Only three components are needed. The full repository carries firmware for dozens of
manufacturers and wastes gigabytes.

```bash
cd ~/src
git clone --depth 1 --branch v2.2.4 https://github.com/indilib/indi-3rdparty.git
cd indi-3rdparty
```

This repository forces `-Werror` in **four** places, not one:

```bash
grep -n "set(COMP_FLAGS" cmake_modules/CMakeCommon.cmake
```

In v2.2.4 these are lines 86, 100, 108 and 116:

```bash
sed -i \
  -e '86s/ -Werror//' \
  -e '100s/ -Werror=stringop-truncation//' \
  -e '108s/ -Werror=unused-parameter//' \
  -e '116s/ -Werror=unused-but-set-variable//' \
  cmake_modules/CMakeCommon.cmake

grep -n "set(COMP_FLAGS" cmake_modules/CMakeCommon.cmake     # confirm all four are clean
```

Again, leave the `check_c_compiler_flag(...)` lines untouched.

Build in this order — the ZWO library must be installed before the drivers that link
against it:

```bash
cmake -B b-libasi -S libasi -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release -DBUILD_LIBS=1
cmake --build b-libasi -j2 && sudo cmake --install b-libasi

cmake -B b-asi -S indi-asi -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release
cmake --build b-asi -j2 && sudo cmake --install b-asi

cmake -B b-eqmod -S indi-eqmod -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release
cmake --build b-eqmod -j2 && sudo cmake --install b-eqmod
```

`-DBUILD_LIBS=1` on `libasi` is mandatory. Without it the ZWO SDK is not installed and
the drivers will fail to find its headers.

Verify:

```bash
ls -l /usr/bin/indi_eqmod_telescope /usr/bin/indi_asi_ccd \
      /usr/bin/indi_asi_wheel /usr/bin/indi_asi_focuser
grep -i -c "Wave 150i" /usr/share/indi/indi_eqmod.xml     # must return 1 or more
rm -rf ~/src/indi-3rdparty/b-*
```

If the `grep` returns 0, the installed driver predates Wave 150i support. Stop and
resolve that before attempting the mount test.

---

## 7. Device permissions and the development user

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG dialout $USER
```

**Log out and log back in**, then confirm:

```bash
groups | grep dialout
```

Without this the serial device returns permission denied, and the cause is not obvious
from the error.

Create the unprivileged user that Claude Code will run as:

```bash
sudo adduser --disabled-password nocturne-dev
sudo usermod -aG dialout nocturne-dev
```

This user has no `sudo`. Anything requiring privileges is executed by you, after reading
the command. This is not bureaucracy — it is what prevents an agent restarting a service
while the mount is powered and moving.

---

## 8. Mount bench test

**This is the most important verification in the project.** It determines whether the
Wave 150i speaks to the Pi over direct USB serial, or whether the SynScan app is required
as a bridge.

### 8.1 Physical state — check before running anything

- Mount head **on a table**
- **No OTA fitted**
- **No counterweight**
- SynScan **closed everywhere**
- Mount powered, USB connected to the Pi

Nothing in this procedure commands motion. The mount will energise when it connects,
which is why nothing is fitted to it.

### 8.2 Is the device visible?

```bash
ls -l /dev/serial/by-id/
```

**Expected on the Wave 150i:**

```
usb-STMicroelectronics_STM32_Virtual_ComPort_XXXXXXXXXXXX-if00 -> ../../ttyACM0
```

The Wave 150i presents an STM32 virtual COM port over CDC-ACM. It appears as
**`/dev/ttyACM0`, not `/dev/ttyUSB0`**, and there is no FTDI, CH340 or CP210x bridge
chip. If you were expecting one of those, this is correct, not a fault.

Confirm the kernel bound it:

```bash
dmesg | grep -i -E "cdc_acm|ttyACM"
ls -l /dev/ttyACM0          # should be owned by root:dialout
```

**If nothing appears:** check cable, port and mount power; try the other USB socket and a
different cable. A charge-only cable will power nothing and enumerate nothing.

### 8.3 Connect

Always use the `by-id` path, never `ttyACM0` — device numbering changes when other
hardware is attached.

```bash
indiserver -v indi_eqmod_telescope > ~/indiserver.log 2>&1 &
sleep 3
indi_getprop | grep -i "DEVICE_PORT\|BAUD\|CONNECTION"
```

The driver auto-detects the port, so `DEVICE_PORT.PORT` should already be populated. The
default baud rate of 9600 is **wrong** for this mount:

```bash
indi_setprop "EQMod Mount.DEVICE_BAUD_RATE.115200=On"
indi_setprop "EQMod Mount.CONNECTION.CONNECT=On"
sleep 4
indi_getprop "EQMod Mount.CONNECTION.CONNECT"
```

### 8.4 What a pass looks like

`CONNECT=On`, plus real values read back from the mount:

```bash
indi_getprop > ~/wave150i-properties.txt
grep -E "MOUNTINFORMATION|STEPPERS|EQUATORIAL_EOD" ~/wave150i-properties.txt
```

On the reference rig this returns `MOUNT_CODE=0x45`, `MOTOR_CONTROLLER=033b`,
`RASteps360=3878400` and `DESteps360=3525120`, and a declination of 90° — the parked
position, pointing at the pole.

**Those step counts are the proof.** They differ between axes, as a strain wave mount's
differing reductions require, and they were read from the controller board. A driver
inventing values would not produce them.

`MOUNT_TYPE=CUSTOM` is expected and is not a fault: mount code 0x45 is not in eqmod's
table of known models, so the driver reads the parameters from the mount itself rather
than from a lookup table. That is better for our purposes.

Keep `wave150i-properties.txt`. It is the first hard data the project produced.

### 8.5 Shut down cleanly

```bash
indi_setprop "EQMod Mount.CONNECTION.DISCONNECT=On"
sleep 2
pkill indiserver
```

> **If `CONNECT` stays `Off`**, read `~/indiserver.log`. A timeout points at the baud
> rate — try 9600. A failure to open the port points at permissions — recheck `dialout`.
> If neither resolves it, capture the diagnostic bundle:
> ```bash
> { echo "--- serial ---"; ls -l /dev/serial/by-id/ /dev/ttyACM* 2>&1
>   echo "--- kernel ---"; dmesg | tail -40
>   echo "--- groups ---"; groups
>   echo "--- indi   ---"; pkg-config --modversion libindi
>   echo "--- log    ---"; cat ~/indiserver.log
> } > ~/wave150i-failure.txt
> ```
> Then confirm the mount is healthy via SynScan and disconnect. **SynScan working while
> the direct cable does not is itself the finding** — it decides the fallback.

> **`bind: Address already in use` in the log** means a previous `indiserver` is still
> running. `pkill indiserver` and start again.

---

## 9. Other devices

Lower risk than the mount — the ZWO drivers are mature and use standard USB. Connect each
device and repeat the pattern:

```bash
indiserver -v indi_asi_ccd indi_asi_wheel indi_asi_focuser > ~/indiserver.log 2>&1 &
sleep 3
indi_getprop | grep -i CONNECTION
```

Then connect each and confirm it reports sensible values: the camera should report its
sensor temperature and cooler state, the filter wheel should report 8 slots, and the
focuser should report an absolute position and a temperature.

---

## 10. StellarSolver and KStars

*Performed on the reference rig, 2026-08-07. KStars 3.8.3 builds and installs.*

### 10.1 Check every package name first

One command, and it checks every name the build needs:

```bash
./scripts/install.sh --check-packages
```

It prints the names apt does not recognise, one per line, and **prints nothing if the
list is good**. Silence is the answer you want. If it does print something, stop: either
the OS is not Trixie, or those names differ on this release. Resolve it now rather than
an hour into a compile.

Verified present on the reference rig, 2026-08-07, all from `trixie/main`, arm64:

```
qt6-base-dev              6.8.2+dfsg-9+deb13u2
extra-cmake-modules       6.13.0-1
libkf6config-dev          6.13.0-2
libkf6i18n-dev            6.13.0-1
libkf6widgetsaddons-dev   6.13.0-1
```

No source build of Qt6 or KF6 is needed. That is the whole reason this guide requires
Trixie — see `docs/decisions/0008-raspberry-pi-os-trixie.md`.

### 10.2 Build

StellarSolver 2.8 first, then KStars. **KStars 3.8.2 or later is required**: it
introduced the Optical Trains DBus interface that Nocturne's executor targets.

KStars is pinned to a **commit**, not a tag, because KStars does not tag modern
releases — its tags stop at `v17.08.3`. The installer pins
`61d849b04c42217cf2f0ab956153e56a928ae8a8`, the head of `stable-3.8.3` as of
2026-08-07. That is the 3.8.3 stable *line* at that date: 3.8.3 plus post-release
fixes, the newest of them an INDI driver sync. See ADR 0006.

#### `-DBUILD_WITH_QT6=ON` is mandatory

KStars 3.8.3 defaults `option(BUILD_WITH_QT6 ...)` to **OFF** (`CMakeLists.txt` line 17).
Without it, configure looks for **Qt5 5.12.7**, and Trixie ships no Qt5 at all — so the
build fails on the very platform this guide requires. This is the exact invocation used
on the reference rig:

```bash
cd ~/src/kstars
cmake -B ../kstars-build -S . \
  -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_WITH_QT6=ON -DBUILD_TESTING=OFF
cmake --build ../kstars-build -j"$(nproc)"
sudo cmake --install ../kstars-build
```

If configure reports it cannot find **Qt5**, this flag is what is missing. Nothing else
produces that message on Trixie.

`/usr`, not `/usr/local`: KStars, the INDI drivers and pkg-config all have to agree on
one prefix, and Debian's own packages live in `/usr` (§6.1).

#### Space

**Budget 10 GB free before starting.** The KStars build tree reached about 9 GB on the
reference rig, plus ~0.5 GB for `libopencv-dev`. A build that runs out of space dies at
around 80 %, an hour in. `scripts/install.sh` projects this and refuses rather than
starting; by hand, check first:

```bash
df -BG ~/src
```

Afterwards, delete the build tree: `rm -rf ~/src/kstars-build`. The reference rig
finished with 13.9 GB free of 28.1 GB, with 39 astrometry index files installed.

#### Warnings you should not act on

Configure warns that it cannot find `Qt6DataVisualization`, `Qt6Keychain`, `LibXISF` and
`Cups`. **All four are expected and none is installed on purpose** — 3D charts, a
credential store, PixInsight's image format and printing, on a headless imaging box. Do
not chase a clean configure log. See ADR 0008.

Expect this to be the longest build of the installation.

### 10.3 Verify what you actually built

```bash
kstars --version
```

**Expected:**

```
kstars 3.8.3
```

The exact string may carry a build suffix; the part that matters is **`3.8.3`**.

- If it reports **3.8.2 or higher**, the Optical Trains interface is present and Nocturne
  can drive Ekos.
- If it reports **3.6.x**, you are running Debian's packaged KStars and the source build
  did not take, or was not first on `PATH`. Check with `command -v kstars`.
- If it reports **anything below 3.8.2**, stop. Nothing Ekos-dependent will work
  correctly, and it will fail in ways that look like Nocturne bugs.

This check ties the built binary back to a version number. The source tree says the same
thing — `PROJECT(kstars VERSION 3.8.3 ...)` at the top of its `CMakeLists.txt` — so the
pin, the tree and the binary all agree, and none of it rests on the branch name.

---

## 10.4 What comes next: the Ekos DBus capture

KStars being installed is what unblocks
[issue #2](https://github.com/alexcatesp/nocturne/issues/2): every Ekos DBus method name
in the bridge is currently a guess made without a KStars to ask.

`docs/ekos-dbus-capture.md` is the procedure — 15 minutes, read-only, and it commands no
equipment. Its output goes into `backend/tests/fixtures/hardware/` and the guesses are
replaced with what the machine says.

---

## 10.5 Your own settings go in a file git does not track

**Do not edit `config/equipment.yaml`.** It is version-controlled, so an edit there
collides with every `git pull` — and it can be pushed, which for the site coordinates
means publishing where the equipment lives.

Put your values beside it instead:

```bash
cp config/equipment.local.yaml.example config/equipment.local.yaml
$EDITOR config/equipment.local.yaml
```

`config/*.local.yaml` is gitignored. It survives every update, is never committed, and
holds only the values you changed — so an upgrade that adds a new setting still reaches
you with its default. There is one for each file:

| Edit this | Instead of | For |
|---|---|---|
| `equipment.local.yaml` | `equipment.yaml` | site coordinates, mount port |
| `safety.local.yaml` | `safety.yaml` | your measured meridian limits |
| `agent.local.yaml` | `agent.yaml` | autonomy level |

Only what you want to change needs to appear. Mappings merge key by key, so setting
`site.latitude` leaves the rest of `site` alone. Lists do not merge: giving
`filter_wheel.slots` replaces the whole wheel.

**Then check it took.**

```bash
.venv/bin/nocturne check-config
```

The report ends with a `Configuration sources:` section listing every value your local
file set, beside the shipped value it displaced. **If something you wrote is not in that
list, it did not take effect** — a misspelled top-level key merges cleanly and does
nothing. A misspelled key *inside* a section is caught and named, along with the file it
came from.

> That report prints your site coordinates. If you paste it into a bug report or a forum
> post, take them out first — it is the same information the shipped placeholder exists to
> keep out of the repository.

See `docs/decisions/0013-local-configuration-overrides.md`.

---

## 11. Backup

**Do this the moment the stack works.** Power down, remove the card or drive, and image it
from another machine:

```bash
sudo dd if=/dev/sdX of=nocturne-m1-working.img bs=4M status=progress
```

Four hours of compilation is worth ten minutes of copying. When a later configuration
change breaks something, you restore instead of rebuilding.

---

## Appendix: known-good versions

Recorded from the reference rig, August 2026.

| Component | Version | Source |
|---|---|---|
| OS | Raspberry Pi OS 64-bit Trixie (Debian 13) | Raspberry Pi Imager |
| INDI core | v2.2.4 | Built from source |
| indi-3rdparty | v2.2.4 (`64fbe2e2dcded132e107d764d4965e034b810a3f`) | Built from source |
| StellarSolver | 2.8 | Built from source |
| KStars | 3.8.3 (`61d849b04c42217cf2f0ab956153e56a928ae8a8`, `stable-3.8.3` head 2026-08-07) | Built from source, `-DBUILD_WITH_QT6=ON` |
| Qt6 | 6.8.2+dfsg-9+deb13u2 | `trixie/main` |
| KDE Frameworks 6 | 6.13.0 | `trixie/main` |
| Mount baud rate | 115200 | — |
| Mount device | `/dev/ttyACM0` (CDC-ACM, STM32 VCP) | — |
