# 0006 — Building INDI, StellarSolver and KStars from source

Status: Accepted · 2026-08-05 · Milestone M1

## Context

SPEC section 4 requires INDI >= 2.2.3: that is the release which added Wave
150i home indexer support to `indi-eqmod`, and the Wave 150i is the mount.
SPEC section 4 also requires a native install with no Docker.

What the distributions actually ship:

| Source | INDI version | Verdict |
|---|---|---|
| Raspberry Pi OS Bookworm (Debian) | 1.9.x | Too old |
| Ubuntu 24.04 universe | 1.9.9 | Too old |
| indilib PPA (`ppa:mutlaqja/ppa`) | 2.x | Ubuntu only; Raspberry Pi OS is Debian, and PPAs do not apply to it |
| Astroberry repository | 1.9.x era | Discontinued |

There is no packaged INDI >= 2.2.3 for Raspberry Pi OS. The ZWO drivers
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

| Component | Tag | Verified |
|---|---|---|
| `indilib/indi` | `v2.2.3` | Yes — `e7dfc40b926a28b2a090308c8075a9970dfcb5b8` |
| `indilib/indi-3rdparty` | `v2.2.3` | Yes — `d899e76bec71de4171c9fc55d48e42fd0b1b0f0e` |
| `rlancaste/stellarsolver` | `2.6` | Yes — `157092d6f843fb987818bd61f0b14b440eca3146` |
| KStars | `v3.7.0` | **No** — invent.kde.org was unreachable from the environment this was written in |

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
- **The KStars tag is unverified.** If `v3.7.0` is not a tag on invent.kde.org,
  the installer stops with the exact reason rather than building a branch. To
  find the right one:
  `git ls-remote --tags https://invent.kde.org/education/kstars.git | tail`
  then set `NOCTURNE_KSTARS_VERSION`. This is the most likely thing to need
  fixing on the first real install.
- **The installer has never been run end to end.** No aarch64 Raspberry Pi was
  available, and the environment it was written in could not reach the
  package sources. Its syntax, option handling, pinning guard and `--check`
  path are tested; the compilation stages are not.
- Should the build burden become tiresome, option 4 — building a `.deb` once —
  is the way out, and `versions.lock` already names exactly what would go into
  it.
