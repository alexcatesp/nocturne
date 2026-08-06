# 0006 — Building INDI, StellarSolver and KStars from source

Status: Accepted · 2026-08-05 · Milestone M1
Revised 2026-08-06: the distribution versions below were unverified when this
was written. They have now been measured on the reference rig running Trixie
(docs/FIELD-NOTES-M1.md section 5.1), and the guesses are replaced by figures.

## Context

SPEC section 4 requires INDI >= 2.2.3: that is the release which added Wave
150i home indexer support to `indi-eqmod`, and the Wave 150i is the mount.
SPEC section 4 also requires a native install with no Docker.

What the distributions actually ship:

| Source | INDI version | Verdict |
|---|---|---|
| Raspberry Pi OS Bookworm (Debian 12) | 1.9.x | Too old, and superseded as a target by ADR 0008 |
| Raspberry Pi OS Trixie (Debian 13) | **1.9.9+dfsg-3+b5** | Measured. Too old |
| Ubuntu 24.04 universe | 1.9.9 | Too old |
| indilib PPA (`ppa:mutlaqja/ppa`) | 2.x | Ubuntu only; Raspberry Pi OS is Debian, and PPAs do not apply to it |
| Astroberry repository | 1.9.x era | Discontinued |

### Measured on Trixie, 2026-08

The hope recorded in the first version of this ADR — that Trixie, being newer
than Bookworm, might ship an INDI close enough to the 2.2.3 floor to drop most
of the source build — **did not survive contact with the machine.** What Trixie
actually has:

```
libindi-dev / indi-bin     1.9.9+dfsg-3+b5      (need >= 2.2.3)
kstars                     5:3.6.2-2+b5         (need >= 3.8.2 for Optical Trains)
libstellarsolver-dev       2.6-1+b1             (need 2.8)
```

Not one of the three is usable. Everything is built from source, and the
`apt-cache policy libindi-dev` check that this ADR previously asked the operator
to run has been performed: the answer is 1.9.9.

There is no apt repository for INDI on Debian ARM at all. The indilib PPA is
Ubuntu-only and adding it to Debian breaks the system, which is worth stating
plainly because it is the obvious thing to try.

**Debian's `indi-bin` is worse than merely old here.** 1.9.9 predates Wave 150i
support in `indi-eqmod`, so a bench test run against it would fail for reasons
that have nothing to do with the cable — and could have sent the project down
the WiFi fallback route for no reason (ADR 0011). `scripts/install.sh` therefore
refuses to run while `libindi-dev` or `indi-bin` is installed, rather than
offering the packaged INDI as a shortcut, and its error text says why.

The ZWO drivers (`indi-asi`) come from `indi-3rdparty` and must link the same
INDI. KStars must be built against that INDI, and StellarSolver is not packaged
at a usable version either.

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

**The pinning mechanism has now been verified end to end.** On the reference
rig, the `indi-3rdparty` v2.2.4 tag resolved to commit
`64fbe2e2dcded132e107d764d4965e034b810a3f` — exactly the SHA recorded in
`versions.lock` (field notes section 5.5). A tag that had been moved upstream
would have produced a different SHA and the mismatch would have been visible.

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
- **The installer has still never been run end to end.** The stages were
  performed by hand on the reference rig, following `docs/installation.md`, and
  everything that went wrong in the process is now folded back into the script
  (ADR 0009, and field notes section 5). That is not the same as the script
  having run: its syntax, option handling, pinning guard, `-Werror` patch and
  `--check` path are tested, the compilation stages are not.
- The measured versions above close the "check this on the Pi" action this ADR
  opened. Nothing in Trixie can be used, so the full source build is not a
  precaution — it is the only route.
- Should the build burden become tiresome, option 4 — building a `.deb` once —
  is the way out, and `versions.lock` already names exactly what would go into
  it.
