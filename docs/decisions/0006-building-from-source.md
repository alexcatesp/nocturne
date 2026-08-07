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

`scripts/install.sh` clones each component at a **tag or a full commit SHA**,
and `require_pinned_ref()` refuses to build anything else. A branch name is a
moving target: two installs of the same Nocturne commit, a week apart, would
build different upstream code, and the rig could behave differently between two
nights with nothing in this repository having changed. That failure mode is
difficult to diagnose at 03:00 and it is cheap to prevent.

### What "pinned" means — a correction to this ADR

The first version of this decision said *tag*, and the guard enforced exactly
that. **That was wrong, and it was wrong in two separate ways.** Both surfaced
when KStars was finally resolved (2026-08-07); neither is a KStars quirk worked
around, and both are recorded here because the guard's model of "pinned" was the
defect.

**A full 40-character commit SHA is the strongest pin there is, and the guard
refused it.** A tag is a label; upstream can move it or delete it, and a build
that trusts one trusts that nobody did. A commit is the commit. Refusing a SHA
while accepting a tag had the ordering backwards, and the cost was concrete:
KStars looks unpinnable under that rule, because KStars does not tag modern
releases at all.

**The check asked the wrong question.** It ran
`git describe --exact-match --tags HEAD`, which establishes that HEAD sits at
*some* tag — never that it sits at the *requested* one. A checkout that landed
somewhere else passed, and `versions.lock` then recorded the ref that was asked
for beside a SHA belonging to something else, which is worse than no record
because it reads as evidence. `indi-3rdparty` makes it concrete rather than
theoretical: `v2.2.3` and `v2.2.3.1` are the same commit, so "HEAD is tagged"
distinguishes nothing.

The guard now accepts a full lowercase 40-hex SHA or a tag, refuses branch names
and short SHAs, and in **both** cases verifies that HEAD is the commit that was
asked for. A mismatch fails even under `--allow-unpinned`: that flag relaxes
"is this pinned", never "is this the tree I was told to build".

`backend/tests/unit/test_install_pinning.py` runs the shipped function against
real git repositories — including one with two tags on a single commit, and one
commit carrying no tag at all — and each of the three defects above was
reproduced as a failing test before it was fixed.

SPEC section 4 sets a **floor** of INDI 2.2.3, not an exact version, so the pins
are the current upstream tags at or above it rather than the floor itself.

| Component | Ref | Commit | Verified against |
|---|---|---|---|
| `indilib/indi` | tag `v2.2.4` | `478d34a34e5dde5ef574bb23917618508707663d` | github.com/indilib/indi |
| `indilib/indi-3rdparty` | tag `v2.2.4` | `64fbe2e2dcded132e107d764d4965e034b810a3f` | github.com/indilib/indi-3rdparty |
| `rlancaste/stellarsolver` | tag `2.8` | `89377481934f40e7a84a8f91f667e37125e47ae0` | github.com/rlancaste/stellarsolver |
| KStars | commit, branch `stable-3.8.3` | `61d849b04c42217cf2f0ab956153e56a928ae8a8` | operator, 2026-08-07 |

`indi` and `indi-3rdparty` are held in **lockstep**: the ZWO drivers link the
core, and the projects tag together. `v2.2.4.1` and `v2.2.4.2` exist for the
core only, with no matching 3rdparty tag, so `v2.2.4` is the newest matched
pair. Raising the core alone would put a driver build against a core it was not
released with.

**Re-checked 2026-08-07**, with the question "was the tag-only rule hiding
anything here too?". All three GitHub pins are genuine tags and resolve to the
commits above, unchanged. The newest tags are `v2.2.4.2` for the core — with
still no matching 3rdparty tag — and `2.8` for StellarSolver, which is already
the pin. So nothing moves: `v2.2.4` remains the newest matched pair, and it is
the pair that built successfully on the Pi. The only thing the old rule hid on
these three was the `v2.2.3` / `v2.2.3.1` duplicate noted above.

### KStars: pinned to a commit, because it has no usable tags

**KStars does not tag its modern releases.** The repository's tags stop at
`v17.08.3`, from the KDE Release Service era; 3.x releases are cut from branches
named `stable-3.x.y`. There is no `v3.8.3` tag and there never will be. The
earlier note in this ADR — that the GitHub mirror's tag list looked incomplete,
with no `v3.6.x` or `v3.7.x` — was reading a real absence and drawing the wrong
conclusion from it. The tags are not missing from the mirror; they were never
cut.

```
KSTARS_REF    = 61d849b04c42217cf2f0ab956153e56a928ae8a8
KSTARS_BRANCH = stable-3.8.3
```

**Provenance: first-hand, from the origin.** This is `refs/heads/stable-3.8.3`
as `git ls-remote --heads` reported it when run against `invent.kde.org`
directly, on the Pi, **captured 2026-08-07**. It is not a value read off a
mirror, inferred from a release page, or relayed second-hand — it came from the
authoritative remote. `invent.kde.org` remains unreachable from the development
environment (`CONNECT tunnel failed, response 403`, re-checked the same day),
which is why the capture happened on the Pi rather than here.

**What this pin is, precisely — and this is now read from the tree, not
inferred.** It is *the state of the 3.8.3 stable line at the moment it was
captured*, **not** the 3.8.3 release tree. Those differ. KStars 3.8.3 was
released 1 June 2026; `stable-3.8.3` is a maintenance branch and takes
post-release fixes. There is no tag marking the release point, so there is
nothing to pin to that would mean "the release" instead.

The checked-out tree declares its own version, which closes the question this
ADR previously had to leave open:

```
CMakeLists.txt   PROJECT(kstars VERSION 3.8.3 ...)
commit           61d849b0, 2026-06-14, "INDI drivers sync"
```

So the pin is **3.8.3 plus post-release fixes to the stable line**, dated two
weeks after the release, and the fix it carries is an **INDI driver sync** —
which is the single most relevant post-release change this project could have
asked for. The cautious framing above was correct and is now a statement of fact
rather than an inference. Measured on the reference rig, 2026-08-07
(`docs/FIELD-NOTES-M1.md` section 16).

The capture date is therefore part of the pin, not metadata about it. Re-taking
the SHA later gives a different tree and needs a new date beside it.

A `stable-3.8.4` branch exists (head
`e6d2161c5e6daa0e75aa364901473f05ccc8c160`, same capture) but corresponds to no
announced release, so it is not used.

The version matters beyond currency: the Optical Trains DBus interface that the
executor bridge targets arrived in **3.8.2**. Building an older release would
have Nocturne working around the absence of an interface that exists upstream.

### Fetching a commit rather than a tag

`git clone --depth 1 --branch X` takes a tag or a branch and never a commit, so
a SHA-pinned component cannot be fetched the way a tagged one is. The order is:

1. Ask the server for that single commit — `git fetch --depth 1 origin <sha>`,
   which GitHub and recent GitLab both allow. One commit, no history.
2. If the server refuses, fetch the branch named in `KSTARS_BRANCH` to a shallow
   depth and check the commit out of it.

**The branch is only a route to the commit; it is never what gets built.**
`require_pinned_ref()` compares HEAD against the SHA afterwards either way, so a
branch that has moved on since the pin was taken fails loudly instead of
silently building a different commit. That verification is the part that
matters — the fetch strategy is an implementation detail that can change, and
the check cannot.

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
- **The KStars pin is a branch head with a date, not a release** — and it is
  now known to be 3.8.3, rather than assumed to be. The guard confirms HEAD *is*
  that commit; `CMakeLists.txt` in that commit declares
  `PROJECT(kstars VERSION 3.8.3 ...)`, and `kstars --version` on the built
  binary agrees. The caveat this bullet used to carry — that nothing tied the
  commit to a version number — is discharged. What remains true, and is the
  reason the date stays in the pin, is that it is the 3.8.3 *line* as of
  2026-08-07 and not the release tree.
- Pinning to a branch head means a future `stable-3.8.3` commit — a backport, a
  translation update — breaks the build until the pin is re-taken. That is the
  intended behaviour and the error says so, but it is a maintenance cost that a
  tag would not have carried.
- **The installer has now been run end to end, and it failed five times.** All
  five were one defect — it verified dependencies without installing them — and
  all five are fixed (field notes section 14). The pinning machinery this ADR is
  about came through unchanged: the fetch, the SHA verification and
  `versions.lock` all behaved as specified on a real run rather than only in
  tests. What the run found was upstream of pinning entirely, in apt.
- The whole build completes: INDI 2.2.4, indi-3rdparty 2.2.4, StellarSolver 2.8
  and KStars 3.8.3, on Trixie, from these pins. `--check` passes clean
  afterwards. That closes the last unverified claim in this ADR.
- The measured versions above close the "check this on the Pi" action this ADR
  opened. Nothing in Trixie can be used, so the full source build is not a
  precaution — it is the only route.
- Should the build burden become tiresome, option 4 — building a `.deb` once —
  is the way out, and `versions.lock` already names exactly what would go into
  it.
