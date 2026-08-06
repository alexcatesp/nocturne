# 0006 — Building INDI, StellarSolver and KStars from source

Status: Accepted · 2026-08-05 · Milestone M1

## Context

SPEC section 4 requires INDI >= 2.2.3: that is the release which added Wave
150i home indexer support to `indi-eqmod`, and the Wave 150i is the mount.
SPEC section 4 also requires a native install with no Docker.

What the distributions actually ship:

| Source | INDI version | Verdict |
|---|---|---|
| Raspberry Pi OS Bookworm (Debian 12) | 1.9.x | Too old, and superseded as a target by ADR 0008 |
| Raspberry Pi OS Trixie (Debian 13) | 2.0.x–2.1.x at freeze | **Unverified from the development environment** — see below |
| Ubuntu 24.04 universe | 1.9.9 | Too old |
| indilib PPA (`ppa:mutlaqja/ppa`) | 2.x | Ubuntu only; Raspberry Pi OS is Debian, and PPAs do not apply to it |
| Astroberry repository | 1.9.x era | Discontinued |

**Confirm this on the Pi before assuming the source build is needed.** Trixie is
newer than Bookworm and its INDI may be closer to the floor. The one-line check
is `apt-cache policy libindi-dev`; if it reports 2.2.3 or later, the INDI and
indi-3rdparty source builds can be dropped in favour of packages, which removes
most of the build time. The development environment for this repository could
not reach Debian's package data, so this is stated as a check to perform, not a
fact established.

At the time of writing there is no packaged INDI >= 2.2.3 known for Raspberry Pi
OS. The ZWO drivers
(`indi-asi`) come from `indi-3rdparty` and must link the same INDI. KStars must
be built against that INDI, and StellarSolver is not packaged for Raspberry Pi
OS at all.

The cost is real: one to three hours on a Pi 5, most of it KStars.

## Options considered

1. **Use the distribution packages.** Half an hour instead of three. But INDI
   1.9.9 predates Wave 150i support, which is the one thing M1 exists to
   establish. This does not meet the specification.
2. **Mix: packaged KStars, source INDI.** KStars from Debian links Debian's
   `libindiclient` 1.9.9 while our drivers link the source-built 2.2.3. Two INDI
   versions on one machine, and the combination is untested by anyone. It might
   work — the protocol is compatible over TCP — but "might work" is not a
   property to want in the layer that moves the mount.
3. **Build everything from source at pinned tags.** Slow, reproducible, and it
   is what the INDI project itself documents for this platform.
4. **Build a Debian package once and install it thereafter.** The right answer
   eventually. It is a build-infrastructure project of its own, and there is one
   machine.

## Decision

Option 3, with pinning enforced rather than merely intended.

`scripts/install.sh` clones each component at a **tag**, and
`require_pinned_ref()` refuses to build anything whose checked-out commit is not
exactly a tag. A branch name is a moving target: two installs of the same
Nocturne commit, a week apart, would build different upstream code, and the rig
could behave differently between two nights with nothing in this repository
having changed. That failure mode is difficult to diagnose at 03:00 and it is
cheap to prevent.

SPEC section 4 sets a **floor** of INDI 2.2.3, not an exact version, so the pins
are the current upstream tags at or above it rather than the floor itself.

| Component | Tag | Commit | Verified against |
|---|---|---|---|
| `indilib/indi` | `v2.2.4` | `478d34a34e5dde5ef574bb23917618508707663d` | github.com/indilib/indi |
| `indilib/indi-3rdparty` | `v2.2.4` | `64fbe2e2dcded132e107d764d4965e034b810a3f` | github.com/indilib/indi-3rdparty |
| `rlancaste/stellarsolver` | `2.8` | `89377481934f40e7a84a8f91f667e37125e47ae0` | github.com/rlancaste/stellarsolver |
| KStars | **unset** | — | **not resolved; see below** |

`indi` and `indi-3rdparty` are held in **lockstep**: the ZWO drivers link the
core, and the projects tag together. `v2.2.4.1` and `v2.2.4.2` exist for the
core only, with no matching 3rdparty tag, so `v2.2.4` is the newest matched
pair. Raising the core alone would put a driver build against a core it was not
released with.

### KStars: deliberately unpinned, and the build refuses

`invent.kde.org` is not reachable from the environment this repository was
developed in (`CONNECT tunnel failed, response 403`). The obvious substitute,
the KDE mirror at `github.com/KDE/kstars`, **cannot be trusted for this**: its
`v3.x` tags run `v3.0.0` through `v3.5.10` and then jump to `v3.80.2`,
`v3.80.3`, `v3.90.1` … `v3.97.0`. There is no `v3.6.x` and no `v3.7.x` at all,
and both of those series certainly shipped — KStars 3.6.3 is in Debian
bookworm. Whatever that mirror is, it is not a complete tag list, and reading a
release tag off it would be a second guess dressed as evidence.

So `KSTARS_VERSION` has **no default**. The KStars stage stops with an
explanation and the command to resolve it:

```
git ls-remote --tags https://invent.kde.org/education/kstars.git | grep 3.8
NOCTURNE_KSTARS_VERSION=<tag> ./scripts/install.sh
```

The version matters beyond currency: the Optical Trains DBus interface that the
executor bridge targets arrived in **3.8.2**. Building an older release would
have Nocturne working around the absence of an interface that exists upstream.
The current stable release is reported as **3.8.3** (1 June 2026); the tag name
and commit are to be confirmed from a machine that can reach KDE, and recorded
here when they are.

Every build appends the component, the tag and the **resolved commit** to
`${BUILD_DIR}/versions.lock`, which `--check` prints. That file is the record of
what this machine is actually running, and it is what to compare when two
installs disagree.

`--allow-unpinned` exists for deliberately testing an upstream branch. It warns
loudly and records the branch name in the lock file.

## Consequences

- The first install takes one to three hours. It is idempotent, so an
  interrupted build resumes; and it is a one-off per machine, not per session.
- Upgrading INDI is an edit to one variable at the top of the installer,
  followed by a rebuild — deliberate, not incidental.
- **KStars will not build until its tag is supplied.** That is deliberate: a
  wrong release here is not cosmetic. `--skip-kstars` installs INDI and Nocturne
  now and leaves KStars for later.
- **The installer has never been run end to end.** No aarch64 Raspberry Pi was
  available, and the environment it was written in could not reach the
  package sources. Its syntax, option handling, pinning guard and `--check`
  path are tested; the compilation stages are not.
- Should the build burden become tiresome, option 4 — building a `.deb` once —
  is the way out, and `versions.lock` already names exactly what would go into
  it.
