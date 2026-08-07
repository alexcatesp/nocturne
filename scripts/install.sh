#!/usr/bin/env bash
#
# Nocturne installer for Raspberry Pi OS 64-bit, TRIXIE (Debian 13) or later.
# SPEC sections 4 and 14 (M1).
#
# Native install. No Docker: SPEC section 4 rules it out because the USB
# passthrough friction outweighs the benefits, and this machine has five USB
# devices on it.
#
# What it installs:
#
#   INDI core >= 2.2.3      built from source. Raspberry Pi OS ships 1.9.x, and
#                           2.2.3 is the release that added Wave 150i home
#                           indexer support to indi-eqmod (SPEC section 4).
#   indi-3rdparty           the ASI camera, focuser and filter wheel drivers.
#   StellarSolver           plate solving, with local astrometry.net indices
#                           sized for the 39-arcminute field of the
#                           200PDS + ASI533MM (SPEC section 2.1).
#   KStars/Ekos             built against the INDI above, run headless. 3.8.x is
#                           Qt6/KF6, which is why Trixie is required: Debian 12
#                           (Bookworm) ships KF5 only. See
#                           docs/decisions/0008-raspberry-pi-os-trixie.md.
#   Siril                   stacking (SPEC section 10.3).
#   Nocturne                a Python virtual environment and this package.
#
# Every stage is idempotent: a stage whose output is already present and at the
# required version is skipped. Run it again after a failure.
#
# Usage:
#   ./scripts/install.sh                 install everything
#   ./scripts/install.sh --check         verify an existing install, change nothing
#   ./scripts/install.sh --check-packages   list Qt6/KF6 names apt does not know
#   ./scripts/install.sh --help          full option list

set -euo pipefail

# --------------------------------------------------------------------------
# Versions and defaults. Change here, not in the body.
# --------------------------------------------------------------------------

# SPEC section 4 sets a FLOOR of INDI 2.2.3, not an exact version. These are the
# current upstream tags at or above it, verified against the real upstreams.
# indi and indi-3rdparty are held in lockstep: the ZWO drivers link the core, and
# the projects release together. v2.2.4.1 and v2.2.4.2 exist for the core only,
# with no matching 3rdparty tag, so v2.2.4 is the newest pair.
readonly INDI_VERSION="${NOCTURNE_INDI_VERSION:-v2.2.4}"
readonly INDI_MINIMUM="2.2.3"
readonly STELLARSOLVER_VERSION="${NOCTURNE_STELLARSOLVER_VERSION:-2.8}"

# KStars does NOT tag its modern releases. The repository's tags stop at
# v17.08.3, from the KDE Release Service era; 3.x releases are cut from branches
# named stable-3.x.y, and no v3.8.3 tag exists or ever will.
#
# So this is pinned to a COMMIT, which is strictly stronger than a tag: a tag
# can be moved or deleted upstream, a SHA cannot. The branch name is kept only
# as a hint for fetching — the SHA is what is verified after checkout.
#
# 3.8.3 was released 1 June 2026 (kstars.kde.org). A stable-3.8.4 branch exists
# but corresponds to no announced release, so it is not used.
readonly KSTARS_REF="${NOCTURNE_KSTARS_REF:-61d849b04c42217cf2f0ab956153e56a928ae8a8}"
readonly KSTARS_BRANCH="${NOCTURNE_KSTARS_BRANCH:-stable-3.8.3}"

readonly INDI_REPO="https://github.com/indilib/indi.git"
readonly INDI_3RDPARTY_REPO="https://github.com/indilib/indi-3rdparty.git"
readonly STELLARSOLVER_REPO="https://github.com/rlancaste/stellarsolver.git"
readonly KSTARS_REPO="https://invent.kde.org/education/kstars.git"

# Astrometry.net index sizing — see size_note() for the arithmetic.
readonly INDEX_SCALE_MIN="${NOCTURNE_INDEX_SCALE_MIN:-4205}"
readonly INDEX_SCALE_MAX="${NOCTURNE_INDEX_SCALE_MAX:-4210}"
readonly INDEX_BASE_URL="${NOCTURNE_INDEX_BASE_URL:-https://data.astrometry.net/4200}"
readonly INDEX_DIR_DEFAULT="/usr/share/astrometry"

# Highest healpix suffix a split index series uses.
readonly INDEX_MAX_HEALPIX=47

# /usr, not /usr/local. KStars, the INDI drivers and pkg-config all have to
# agree on one prefix, and Debian's own packages live in /usr; installing the
# source build beside them under /usr/local produces two libindi on the same
# machine with the older one first on the search path. This is the prefix the
# reference rig was built with (docs/installation.md section 6).
readonly INSTALL_PREFIX="${NOCTURNE_INSTALL_PREFIX:-/usr}"

readonly BUILD_DIR_DEFAULT="${HOME}/.cache/nocturne-build"
readonly REQUIRED_ARCH="aarch64"

# Raspberry Pi OS Trixie is Debian 13. KStars 3.8.x is a Qt6/KF6 application;
# Debian 12 (Bookworm) ships KF5 only, and building the KF6 tree from source on
# a Pi is days of work that probably never completes.
readonly REQUIRED_DEBIAN_VERSION=13
readonly REQUIRED_DEBIAN_NAME="trixie"

# KStars 3.8.x build dependencies, Debian 13 package names. If a name here is
# wrong for your release, check_kstars_build_dependencies() names it before any
# compiler runs — correct it here, in one place.
readonly QT6_BUILD_PACKAGES=(
    qt6-base-dev qt6-base-dev-tools qt6-declarative-dev qt6-svg-dev
    qt6-tools-dev qt6-tools-dev-tools qt6-positioning-dev qt6-multimedia-dev
    qt6-websockets-dev qt6-5compat-dev libqt6sql6-sqlite
)
readonly KF6_BUILD_PACKAGES=(
    extra-cmake-modules libkf6config-dev libkf6coreaddons-dev libkf6crash-dev
    libkf6doctools-dev libkf6guiaddons-dev libkf6i18n-dev libkf6kio-dev
    libkf6newstuff-dev libkf6notifications-dev libkf6plotting-dev
    libkf6widgetsaddons-dev libkf6xmlgui-dev
)
readonly REQUIRED_DISK_GB=12
readonly PYTHON_MINIMUM="3.11"

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT

BUILD_DIR="${NOCTURNE_BUILD_DIR:-${BUILD_DIR_DEFAULT}}"
# Which commit of each upstream component this machine actually built.
VERSIONS_LOCK="${BUILD_DIR}/versions.lock"
INDEX_DIR="${NOCTURNE_INDEX_DIR:-${INDEX_DIR_DEFAULT}}"
JOBS="$(nproc 2>/dev/null || echo 2)"
CHECK_ONLY=0
ALLOW_UNPINNED=0
SKIP_INDICES=0
SKIP_KSTARS=0
ASSUME_YES=0

# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

if [[ -t 1 ]]; then
    readonly C_RESET=$'\033[0m' C_BOLD=$'\033[1m' C_RED=$'\033[31m'
    readonly C_GREEN=$'\033[32m' C_YELLOW=$'\033[33m'
else
    readonly C_RESET="" C_BOLD="" C_RED="" C_GREEN="" C_YELLOW=""
fi

step()  { printf '\n%s==> %s%s\n' "${C_BOLD}" "$*" "${C_RESET}"; }
info()  { printf '    %s\n' "$*"; }
ok()    { printf '    %s[ ok ]%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn()  { printf '    %s[warn]%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
fail()  { printf '\n%sInstallation failed:%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; exit 1; }

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --check              verify an existing installation and exit
  --check-packages     print Qt6/KF6 package names apt does not know, and exit.
                       Silence means the list is good on this release.
  --allow-unpinned     permit an unpinned upstream ref (build is not reproducible)
  --build-dir DIR      where to clone and build (default ~/.cache/nocturne-build)
  --index-dir DIR      where to put astrometry indices (default /usr/share/astrometry)
  --jobs N             parallel compile jobs (default: nproc)
  --skip-indices       do not download astrometry index files
  --skip-kstars        do not build KStars (INDI and Nocturne only)
  --yes                do not prompt before the long build
  --help               this text

Environment overrides:
  NOCTURNE_INDI_VERSION, NOCTURNE_STELLARSOLVER_VERSION, NOCTURNE_KSTARS_REF,
  NOCTURNE_KSTARS_BRANCH,
  NOCTURNE_INDEX_SCALE_MIN, NOCTURNE_INDEX_SCALE_MAX, NOCTURNE_INDEX_BASE_URL,
  NOCTURNE_BUILD_DIR, NOCTURNE_INDEX_DIR
EOF
}

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

as_root() {
    if [[ ${EUID} -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

# Is $1 >= $2, comparing dotted version numbers?
version_at_least() {
    [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

installed_indi_version() {
    command -v indiserver >/dev/null 2>&1 || return 0
    # indiserver --help exits non-zero by design, and pipefail would take the
    # whole script down with it.
    { indiserver --help 2>&1 || true; } |
        sed -n 's/.*INDI Library: \([0-9.]*\).*/\1/p' | head -n1
}

confirm() {
    [[ ${ASSUME_YES} -eq 1 ]] && return 0
    [[ ! -t 0 ]] && return 0
    local reply
    read -r -p "    $1 [y/N] " reply
    [[ ${reply} == [yY]* ]]
}

# Clone or update a repository at a tag, verify the ref does not move, and
# record the commit that was actually built. See
# docs/decisions/0006-building-from-source.md.
# fetch_source <repo> <ref> <dir> [branch_hint]
#
# `git clone --depth 1 --branch X` accepts a tag or a branch, never a commit, so
# a SHA-pinned component cannot be fetched the same way as a tagged one.
#
# For a SHA the order is: ask the server for that one commit
# (`fetch --depth 1 <sha>`, which GitHub and recent GitLab allow), and if it
# refuses, fall back to fetching the branch it is supposed to be on and checking
# the commit out of that. The branch hint is only a route to the commit — it is
# never what gets built, because require_pinned_ref compares HEAD against the
# SHA either way. A branch that has moved on fails there, loudly, rather than
# silently building a different commit.
fetch_source() {
    local repo="$1" ref="$2" dir="$3" branch_hint="${4:-}"
    local name
    name="$(basename "${dir}")"

    if [[ -d ${dir}/.git ]]; then
        info "updating ${name}"
        rm -rf "${dir}"
    fi

    info "cloning ${name} at ${ref}"
    if is_full_sha "${ref}"; then
        git init --quiet "${dir}"
        git -C "${dir}" remote add origin "${repo}"
        if git -C "${dir}" fetch --quiet --depth 1 origin "${ref}" 2>/dev/null; then
            git -C "${dir}" checkout --quiet FETCH_HEAD
        else
            [[ -n ${branch_hint} ]] || fail "${name} is pinned to a commit, this server
    will not serve a single commit, and no branch was given to find it on.
    Pass the branch as the fourth argument to fetch_source."
            info "server would not serve the commit directly; fetching ${branch_hint}"
            git -C "${dir}" fetch --quiet --depth 50 origin "${branch_hint}"
            git -C "${dir}" checkout --quiet "${ref}" 2>/dev/null || fail \
                "${ref} is not among the last 50 commits of ${branch_hint}.
    Either the pin is wrong or the branch has moved a long way. Confirm the
    commit before changing anything: this installer will not build a different
    one to get past the error."
        fi
    else
        git clone --quiet --depth 1 --branch "${ref}" "${repo}" "${dir}"
    fi

    require_pinned_ref "${name}" "${ref}" "${dir}"
    record_built_version "${name}" "${ref}" "$(git -C "${dir}" rev-parse HEAD)"
}

# A full 40-character commit SHA. Anything shorter is ambiguous, and a branch
# name is a moving target.
is_full_sha() {
    [[ $1 =~ ^[0-9a-f]{40}$ ]]
}

# A branch name is a moving target: two installs a week apart would build
# different code, and a bug could appear between two nights with nothing in this
# repository having changed.
#
# What counts as pinned is a full commit SHA or a tag — and a SHA is the
# STRONGER of the two, not a lesser substitute. A tag is a label upstream can
# move or delete; a SHA is the commit. An earlier version of this guard accepted
# only tags, which refused the strongest pin there is and forced a workaround for
# KStars, which tags nothing. See docs/decisions/0006-building-from-source.md.
#
# Either way, what is checked is that HEAD is *the commit that was asked for*.
# The earlier guard asked `git describe --exact-match --tags HEAD`, which only
# establishes that HEAD sits at SOME tag — so a checkout that landed on a
# different tag from the requested one passed, and versions.lock then recorded a
# ref beside a SHA that did not belong to it. indi-3rdparty makes that concrete:
# v2.2.3 and v2.2.3.1 are the same commit, so "HEAD is tagged" says nothing
# about which of them you built.
require_pinned_ref() {
    local name="$1" ref="$2" dir="$3"
    local head
    head="$(git -C "${dir}" rev-parse HEAD)"

    if is_full_sha "${ref}"; then
        [[ ${head} == "${ref}" ]] && return 0
        fail "${name} was pinned to commit ${ref}
    but the checkout is at ${head}.
    Nothing has been built. This is not a warning to pass: the source tree is
    not the one this installer was told to build."
    fi

    # Not a SHA, so it must be a tag, and HEAD must be at THAT tag.
    local tagged
    tagged="$(git -C "${dir}" rev-parse --verify --quiet "refs/tags/${ref}^{commit}" || true)"
    if [[ -z ${tagged} ]]; then
        # A shallow fetch does not always bring the tag ref with it. Ask the
        # remote rather than assume; an unreachable remote fails closed below.
        # `|| true` on the pipeline, not just 2>/dev/null: under `set -o pipefail`
        # a failed ls-remote — no origin, no network — would abort the installer
        # with exit 128 and no message at all, instead of reaching the explained
        # refusal below. Failing closed is right; failing silently is not.
        tagged="$( { git -C "${dir}" ls-remote origin "refs/tags/${ref}^{}" 2>/dev/null ||
            true; } | awk 'NR==1 {print $1}')"
    fi
    if [[ -n ${tagged} && ${tagged} == "${head}" ]]; then
        return 0
    fi

    if [[ ${ALLOW_UNPINNED} -eq 1 ]]; then
        warn "${name} is at '${ref}', which is neither a tag nor a full commit SHA.
    The build is NOT reproducible."
        return 0
    fi
    fail "${name} ref '${ref}' is neither a tag this checkout is sitting on nor a
    full 40-character commit SHA, so this build would not be reproducible.
    Two installs of the same Nocturne commit could then behave differently, and a
    change upstream could alter the rig between two nights.
    A short SHA is not enough: it is ambiguous, and it is not what gets recorded.
    Set the matching NOCTURNE_* variable to a tag or a full commit SHA, or pass
    --allow-unpinned if you are deliberately testing an upstream branch."
}

record_built_version() {
    local name="$1" ref="$2" sha="$3"
    mkdir -p "$(dirname "${VERSIONS_LOCK}")"
    # Replace any earlier line for this component, then append the current one.
    if [[ -f ${VERSIONS_LOCK} ]]; then
        grep -v "^${name} " "${VERSIONS_LOCK}" > "${VERSIONS_LOCK}.tmp" || true
        mv "${VERSIONS_LOCK}.tmp" "${VERSIONS_LOCK}"
    fi
    printf '%s %s %s\n' "${name}" "${ref}" "${sha}" >> "${VERSIONS_LOCK}"
    info "${name} ${ref} (${sha})"
}

cmake_build_install() {
    local source_dir="$1" build_subdir="$2"
    shift 2
    local build_dir="${BUILD_DIR}/${build_subdir}"
    # Delete rather than reuse. CMake's cache keeps the flags from the previous
    # configure, and reconfiguring over it does not reliably clear them, so a
    # build directory left over from before the -Werror patch below would still
    # fail with -Werror. This cost a full build cycle to discover; see
    # docs/decisions/0009-werror-and-trixie-gcc.md.
    rm -rf "${build_dir}"
    cmake -S "${source_dir}" -B "${build_dir}" \
        -DCMAKE_INSTALL_PREFIX="${INSTALL_PREFIX}" \
        -DCMAKE_BUILD_TYPE=Release \
        "$@"
    # Chained on purpose. Run as two statements, a failed build is followed by
    # an install that reports a missing binary, and that error names the install
    # step while hiding the real failure.
    cmake --build "${build_dir}" -j "${JOBS}" && as_root cmake --install "${build_dir}"
    as_root ldconfig
    # The build tree is the biggest thing on the disk and the rig has 32 GB.
    rm -rf "${build_dir}"
}

# --------------------------------------------------------------------------
# -Werror
# --------------------------------------------------------------------------
#
# INDI v2.2.4 predates the GCC in Trixie, and the newer compiler emits warnings
# the project treats as fatal — indi_astrotrac_telescope dies on
# -Werror=stringop-overread, killing the whole build at 32% for a driver we do
# not own. -DCMAKE_CXX_FLAGS="-Wno-error" does not help: the project appends
# -Werror after user flags, so its setting wins. The flag has to come out at
# source. See docs/decisions/0009-werror-and-trixie-gcc.md.
strip_werror() {
    local source_dir="$1" expected="$2"
    local file="${source_dir}/cmake_modules/CMakeCommon.cmake"
    [[ -f ${file} ]] || fail "${file} does not exist. The upstream layout has changed;
    re-check where -Werror is set before building, rather than building without
    knowing whether it is still forced."

    # Only lines that ADD the flag to the build. check_c_compiler_flag(...) lines
    # in the same file are capability probes: patching one changes what the build
    # detects, not what it enforces, and it must be left exactly as it is.
    local -a targets=()
    while IFS=: read -r line _; do targets+=("${line}"); done < <(
        grep -n -i -E '^[[:space:]]*set\(COMP_FLAGS.*-Werror' "${file}" || true
    )

    if (( ${#targets[@]} != expected )); then
        fail "expected ${expected} forced -Werror line(s) in
    ${file}
    but found ${#targets[@]}. This installer was written against ${INDI_VERSION};
    at a different tag the flag may be set somewhere else entirely. Check with:
        grep -n -i 'Werror' '${file}'
    and update strip_werror() before building. Nothing has been built."
    fi

    local line
    for line in "${targets[@]}"; do
        sed -i "${line}s/ -Werror[A-Za-z=-]*//g" "${file}"
    done

    if grep -q -i -E '^[[:space:]]*set\(COMP_FLAGS.*-Werror' "${file}"; then
        fail "-Werror is still forced in ${file} after patching it. Nothing has been built."
    fi
    info "removed ${expected} forced -Werror flag(s) from $(basename "${source_dir}")"
}

# Debian ships libindi 1.9.9. It is not merely old: it predates Wave 150i
# support in indi-eqmod, so a bench test run against it fails for reasons that
# have nothing to do with the cable — and could wrongly send the project down
# the WiFi fallback route. It also shadows the source build: pkg-config finds
# the packaged headers first and the drivers link the wrong library.
#
# There is no shortcut available here. The INDI PPA is Ubuntu-only, and adding
# it to Debian breaks the system; there is no INDI apt repository for Debian on
# ARM. Compiling is the only route, which is why this installer takes an hour.
refuse_packaged_indi() {
    local -a packaged=()
    local package
    for package in libindi-dev indi-bin; do
        if dpkg-query -W -f='${Status}' "${package}" 2>/dev/null | grep -q "install ok installed"; then
            packaged+=("${package}")
        fi
    done
    (( ${#packaged[@]} == 0 )) && return 0

    fail "Debian's INDI packages are installed: ${packaged[*]}

    Trixie ships libindi 1.9.9, which predates Wave 150i support in indi-eqmod.
    Left in place it also shadows the build this installer is about to make:
    pkg-config would find the packaged headers first and the drivers would link
    the wrong library.

    Remove them and run this again:
        sudo apt remove ${packaged[*]}

    There is no packaged alternative. The INDI PPA is Ubuntu-only and adding it
    to Debian breaks the system, so building from source is the only route.
    Nothing has been built."
}

# --------------------------------------------------------------------------
# Why these index scales, and not others
# --------------------------------------------------------------------------

size_note() {
    cat <<EOF
    Field: 3008 px x 0.776 arcsec/px = 38.9 arcmin (SPEC section 2.1).
    astrometry.net indexes quads by size; the useful range is roughly a third
    of the field up to somewhat more than the whole field. For 39 arcmin that
    is about 12 to 50 arcmin, which is series 4205 (11-16') through 4209
    (42-60'), plus 4210 (60-85') for margin after a coma corrector changes the
    focal length. Nocturne solves full frames from a known approximate
    position after a slew, not blind, so the very small scales (4200-4204, tens
    of gigabytes) buy nothing here.
    Override with NOCTURNE_INDEX_SCALE_MIN / _MAX if your optical train changes.
EOF
}

# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------

preflight() {
    step "Checking this machine"

    local arch
    arch="$(uname -m)"
    if [[ ${arch} != "${REQUIRED_ARCH}" ]]; then
        fail "this installer targets Raspberry Pi OS 64-bit (${REQUIRED_ARCH}), found ${arch}.
    A 32-bit install cannot address enough memory for stacking 3008x3008 frames."
    fi
    ok "architecture ${arch}"

    check_os_release

    if ! command -v sudo >/dev/null 2>&1 && [[ ${EUID} -ne 0 ]]; then
        fail "sudo is not installed and this is not running as root"
    fi

    local free_gb
    free_gb="$(df -BG --output=avail "${HOME}" | tail -n1 | tr -dc '0-9')"
    if [[ -n ${free_gb} ]] && (( free_gb < REQUIRED_DISK_GB )); then
        fail "only ${free_gb} GB free under ${HOME}; the build and the astrometry
    indices need about ${REQUIRED_DISK_GB} GB. SPEC section 2.2 requires NVMe storage:
    an SD card will not survive the stacking write load either."
    fi
    ok "${free_gb:-?} GB free"

    local python_version
    python_version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    if [[ -z ${python_version} ]] || ! version_at_least "${python_version}" "${PYTHON_MINIMUM}"; then
        fail "Python ${PYTHON_MINIMUM}+ is required, found ${python_version:-none}"
    fi
    ok "Python ${python_version}"

    if ! ping -c1 -W3 data.astrometry.net >/dev/null 2>&1; then
        warn "data.astrometry.net is not answering; the index download may fail"
    fi

    # Before any compiler runs, not an hour into one.
    check_kstars_build_dependencies
}

check_os_release() {
    if [[ ! -r /etc/os-release ]]; then
        fail "cannot read /etc/os-release, so the OS release cannot be checked"
    fi
    # shellcheck disable=SC1091  # provided by the OS, not this repository
    . /etc/os-release
    info "OS: ${PRETTY_NAME:-unknown}"

    if [[ ${ID:-} != "debian" && ${ID_LIKE:-} != *debian* ]]; then
        fail "this installer targets Raspberry Pi OS, which is Debian based.
    Found ID='${ID:-unknown}'."
    fi

    # Only Debian's own version numbers are comparable with Debian 13. A
    # debian-derived OS with a different scheme (Ubuntu's 24.04, say) is left to
    # the package check below, which is the real gate.
    if [[ ${ID:-} != "debian" && ${ID:-} != "raspbian" ]]; then
        warn "'${ID:-unknown}' is Debian-derived but is not Raspberry Pi OS;"
        warn "the Qt6/KF6 package check below decides whether it can build KStars"
        return 0
    fi

    local major="${VERSION_ID:-0}"
    major="${major%%.*}"
    if [[ ! ${major} =~ ^[0-9]+$ ]] || (( major < REQUIRED_DEBIAN_VERSION )); then
        fail "Raspberry Pi OS ${REQUIRED_DEBIAN_NAME} (Debian ${REQUIRED_DEBIAN_VERSION}) or
    later is required. This is Debian '${VERSION_ID:-unknown}'
    (${VERSION_CODENAME:-unknown}).

    Why: KStars 3.8.x is a Qt6/KF6 application. Debian 12 (bookworm) ships KF5
    only, so building KStars there would mean building Qt6 and the whole KF6
    tree from source on a Raspberry Pi — days of compilation that in practice
    does not complete. Trixie ships Plasma 6 and therefore packaged KF6.

    Re-image the Pi with a current Raspberry Pi OS 64-bit release, or run with
    --skip-kstars to install INDI and Nocturne only.
    See docs/decisions/0008-raspberry-pi-os-trixie.md."
    fi
    ok "Debian ${VERSION_ID} (${VERSION_CODENAME:-?}) — Qt6/KF6 available"
}

# Refuse before building anything if the toolkit KStars needs is not obtainable.
# Distinguishes "not installed but in the archive" from "apt has never heard of
# this name", because the second means the package list is wrong for this
# release and no amount of apt-get will fix it.
# --check-packages: name every Qt6/KF6 package apt does not recognise, and
# nothing else. Silence means the whole list is good on this release.
#
# Separate from check_kstars_build_dependencies() on purpose. That one is a
# preflight gate: it runs inside an install, stops it, and explains at length.
# This one answers a single question, prints one name per line, and is meant to
# be pasted into a terminal on the Pi and the output pasted back. Both read the
# same two arrays, so neither can drift from what the install would use.
report_unknown_packages() {
    local wanted=("${QT6_BUILD_PACKAGES[@]}" "${KF6_BUILD_PACKAGES[@]}")

    # One apt-cache call, not one per package: `apt-cache policy` silently omits
    # names it does not know, so what comes back is the set that exists and the
    # difference is the answer. Twenty-four separate `apt-cache show` calls took
    # 31 seconds; this takes one.
    local known
    known="$(apt-cache policy "${wanted[@]}" 2>/dev/null |
        sed -n 's/^\([^ :][^:]*\):$/\1/p')"

    local package unknown=()
    for package in "${wanted[@]}"; do
        grep -qxF "${package}" <<<"${known}" || unknown+=("${package}")
    done

    (( ${#unknown[@]} == 0 )) && return 0
    printf '%s\n' "${unknown[@]}"
    return 1
}

check_kstars_build_dependencies() {
    if [[ ${SKIP_KSTARS} -eq 1 ]]; then
        info "KStars skipped; not checking Qt6/KF6"
        return 0
    fi

    step "Checking the Qt6 and KF6 packages KStars 3.8.x needs"
    local installable=() unknown=() package
    for package in "${QT6_BUILD_PACKAGES[@]}" "${KF6_BUILD_PACKAGES[@]}"; do
        if dpkg-query -W -f='${Status}' "${package}" 2>/dev/null | grep -q "ok installed"; then
            continue
        fi
        if apt-cache show "${package}" >/dev/null 2>&1; then
            installable+=("${package}")
        else
            unknown+=("${package}")
        fi
    done

    if (( ${#unknown[@]} > 0 )); then
        fail "apt does not know these packages on this release:
        ${unknown[*]}

    That means either the OS is not what this installer expects, or the package
    names in QT6_BUILD_PACKAGES / KF6_BUILD_PACKAGES at the top of this script
    are wrong for your Raspberry Pi OS version. Check with:
        apt-cache search --names-only '^libkf6.*-dev$'
    and correct the list. Nothing has been built."
    fi

    if (( ${#installable[@]} > 0 )); then
        info "${#installable[@]} package(s) still to install; they are in the archive"
    fi
    ok "every Qt6/KF6 build dependency is installed or available"
}

install_build_dependencies() {
    step "Installing build dependencies"
    as_root apt-get update -qq
    as_root apt-get install -y --no-install-recommends \
        build-essential cmake git pkg-config \
        libnova-dev libcfitsio-dev libusb-1.0-0-dev zlib1g-dev libgsl-dev \
        libjpeg-dev libcurl4-gnutls-dev libtheora-dev libfftw3-dev \
        libev-dev libavcodec-dev libavdevice-dev libraw-dev libgphoto2-dev \
        libftdi1-dev libdc1394-dev libgps-dev librtlsdr-dev \
        python3-venv python3-dev \
        siril
    ok "apt dependencies installed"
}

install_kstars_dependencies() {
    step "Installing KStars build dependencies"
    as_root apt-get install -y --no-install-recommends \
        "${QT6_BUILD_PACKAGES[@]}" "${KF6_BUILD_PACKAGES[@]}" \
        libeigen3-dev wcslib-dev
    ok "Qt6 and KF6 build dependencies installed"
}

build_indi() {
    step "Building INDI ${INDI_VERSION}"
    local existing
    existing="$(installed_indi_version)"
    if [[ -n ${existing} ]] && version_at_least "${existing}" "${INDI_MINIMUM}"; then
        ok "INDI ${existing} is already installed (>= ${INDI_MINIMUM})"
        return
    fi
    [[ -n ${existing} ]] && info "found INDI ${existing}, which is below ${INDI_MINIMUM}"

    refuse_packaged_indi
    fetch_source "${INDI_REPO}" "${INDI_VERSION}" "${BUILD_DIR}/indi"
    strip_werror "${BUILD_DIR}/indi" 1
    cmake_build_install "${BUILD_DIR}/indi" "indi-build"

    existing="$(installed_indi_version)"
    version_at_least "${existing:-0}" "${INDI_MINIMUM}" ||
        fail "built INDI reports version '${existing:-unknown}', expected >= ${INDI_MINIMUM}"
    ok "INDI ${existing}"
}

build_indi_3rdparty() {
    step "Building the ZWO and EQMod drivers (indi-3rdparty)"
    fetch_source "${INDI_3RDPARTY_REPO}" "${INDI_VERSION}" "${BUILD_DIR}/indi-3rdparty"
    strip_werror "${BUILD_DIR}/indi-3rdparty" 4

    # Three components out of the whole repository. Each subdirectory is a
    # standalone CMake project. Building the lot is 204 MB of checkout and hours
    # of compiling firmware for cameras nobody here owns, on a machine with
    # 32 GB of storage and no swap.
    #
    # libasi FIRST, and with -DBUILD_LIBS=1: that is what installs the ZWO SDK
    # headers, and without them indi-asi cannot compile.
    info "the ZWO SDK (libasi)"
    cmake_build_install "${BUILD_DIR}/indi-3rdparty/libasi" "3rdparty-libasi" -DBUILD_LIBS=1

    info "the ZWO drivers (indi-asi)"
    cmake_build_install "${BUILD_DIR}/indi-3rdparty/indi-asi" "3rdparty-indi-asi"

    info "the mount driver (indi-eqmod)"
    cmake_build_install "${BUILD_DIR}/indi-3rdparty/indi-eqmod" "3rdparty-indi-eqmod"

    local missing=()
    local driver
    for driver in indi_asi_ccd indi_asi_focuser indi_asi_wheel indi_eqmod_telescope; do
        command -v "${driver}" >/dev/null 2>&1 || missing+=("${driver}")
    done
    if (( ${#missing[@]} > 0 )); then
        fail "these drivers were not installed: ${missing[*]}
    They are named in config/equipment.yaml and Nocturne cannot reach the rig without them."
    fi
    # The whole point of building 2.2.4 rather than taking the packaged 1.9.9.
    local eqmod_xml="${INSTALL_PREFIX}/share/indi/indi_eqmod.xml"
    if [[ -f ${eqmod_xml} ]] && ! grep -q -i "Wave 150i" "${eqmod_xml}"; then
        fail "the installed indi-eqmod does not list the Wave 150i:
        ${eqmod_xml}
    That means an older driver is installed, and a bench test against it would
    fail for reasons unrelated to the cable."
    fi
    ok "indi_asi_ccd, indi_asi_focuser, indi_asi_wheel, indi_eqmod_telescope"
}

build_stellarsolver() {
    step "Building StellarSolver ${STELLARSOLVER_VERSION}"
    if [[ -f /usr/local/lib/libstellarsolver.so || -f /usr/lib/libstellarsolver.so ]]; then
        ok "StellarSolver is already installed"
        return
    fi
    fetch_source "${STELLARSOLVER_REPO}" "${STELLARSOLVER_VERSION}" "${BUILD_DIR}/stellarsolver"
    cmake_build_install "${BUILD_DIR}/stellarsolver" "stellarsolver-build"
    ok "StellarSolver installed"
}

build_kstars() {
    step "Building KStars ${KSTARS_REF:-<unpinned>} (${KSTARS_BRANCH})"
    if [[ ${SKIP_KSTARS} -eq 1 ]]; then
        warn "skipped at your request; Nocturne cannot drive Ekos without it"
        return
    fi
    if command -v kstars >/dev/null 2>&1; then
        ok "KStars is already installed ($(kstars --version 2>&1 | head -n1))"
        return
    fi
    if [[ -z ${KSTARS_REF} ]]; then
        fail "no KStars commit is pinned, and this installer will not guess one.
    Building the wrong release is not cosmetic: the Optical Trains DBus interface
    the executor bridge targets arrived in 3.8.2, and an older build would have
    Nocturne working around the absence of an interface that exists upstream.

    KStars does not tag modern releases — its tags stop at v17.08.3 — so this is
    a commit, not a tag. Find the head of the stable branch for the release you
    want on a machine that can reach KDE:
        git ls-remote --heads https://invent.kde.org/education/kstars.git 'stable-3.8*'
    then re-run with:
        NOCTURNE_KSTARS_REF=<40-character commit> \\
        NOCTURNE_KSTARS_BRANCH=<stable-3.x.y> ./scripts/install.sh

    Or --skip-kstars to install INDI and Nocturne now and add KStars later."
    fi
    if ! is_full_sha "${KSTARS_REF}"; then
        fail "NOCTURNE_KSTARS_REF is '${KSTARS_REF}', which is not a full
    40-character commit SHA. KStars has no usable tags, so a commit is the only
    thing that pins it. Nothing has been built."
    fi
    install_kstars_dependencies
    fetch_source "${KSTARS_REPO}" "${KSTARS_REF}" "${BUILD_DIR}/kstars" "${KSTARS_BRANCH}"
    cmake_build_install "${BUILD_DIR}/kstars" "kstars-build" -DBUILD_TESTING=OFF
    command -v kstars >/dev/null 2>&1 || fail "KStars did not install"
    ok "KStars installed"
}

download_indices() {
    step "Downloading astrometry.net indices for a 39-arcminute field"
    if [[ ${SKIP_INDICES} -eq 1 ]]; then
        warn "skipped at your request; plate solving will not work offline"
        return
    fi
    size_note
    as_root mkdir -p "${INDEX_DIR}"

    local scale downloaded=0
    for (( scale = INDEX_SCALE_MIN; scale <= INDEX_SCALE_MAX; scale++ )); do
        if fetch_index_file "index-${scale}.fits"; then
            downloaded=$(( downloaded + 1 ))
            continue
        fi
        # Smaller scales are split across healpix tiles rather than one file.
        local healpix suffix got_any=0
        for (( healpix = 0; healpix <= INDEX_MAX_HEALPIX; healpix++ )); do
            suffix="$(printf '%02d' "${healpix}")"
            if fetch_index_file "index-${scale}-${suffix}.fits"; then
                got_any=1
                downloaded=$(( downloaded + 1 ))
            elif (( got_any == 1 )); then
                break   # the series ended
            else
                break   # this scale is not published in either form
            fi
        done
        if (( got_any == 0 )); then
            warn "no index files found for scale ${scale}"
        fi
    done

    (( downloaded > 0 )) || fail "no astrometry index files could be downloaded from ${INDEX_BASE_URL}"
    ok "${downloaded} index file(s) in ${INDEX_DIR}"
    write_astrometry_config
}

# Download one index file. Returns non-zero if it is not published.
fetch_index_file() {
    local name="$1" target="${INDEX_DIR}/$1"
    if [[ -s ${target} ]]; then
        info "have ${name}"
        return 0
    fi
    if ! curl -fsSL --retry 3 --retry-delay 2 -o /tmp/"${name}" "${INDEX_BASE_URL}/${name}"; then
        rm -f /tmp/"${name}"
        return 1
    fi
    as_root mv /tmp/"${name}" "${target}"
    info "fetched ${name}"
}

write_astrometry_config() {
    local config="/etc/astrometry.cfg"
    if [[ -f ${config} ]] && grep -qx "add_path ${INDEX_DIR}" "${config}"; then
        ok "${config} already points at ${INDEX_DIR}"
        return
    fi
    info "pointing ${config} at ${INDEX_DIR}"
    as_root tee "${config}" >/dev/null <<EOF
# Written by nocturne/scripts/install.sh
add_path ${INDEX_DIR}
autoindex
inparallel
EOF
}

install_nocturne() {
    step "Installing Nocturne"
    local venv="${REPO_ROOT}/.venv"
    if [[ ! -d ${venv} ]]; then
        python3 -m venv "${venv}"
    fi
    "${venv}/bin/pip" install --quiet --upgrade pip
    "${venv}/bin/pip" install --quiet -e "${REPO_ROOT}"
    ok "virtual environment at ${venv}"
}

verify() {
    step "Verifying the installation"
    local problems=0

    local indi_version
    indi_version="$(installed_indi_version)"
    if [[ -n ${indi_version} ]] && version_at_least "${indi_version}" "${INDI_MINIMUM}"; then
        ok "INDI ${indi_version}"
    else
        warn "INDI ${indi_version:-not found}, need >= ${INDI_MINIMUM}"
        problems=$(( problems + 1 ))
    fi

    local binary
    for binary in indiserver indi_eqmod_telescope indi_asi_ccd indi_asi_focuser indi_asi_wheel; do
        if command -v "${binary}" >/dev/null 2>&1; then
            ok "${binary}"
        else
            warn "${binary} is missing"
            problems=$(( problems + 1 ))
        fi
    done

    local missing_simulators=0
    for binary in indi_simulator_telescope indi_simulator_ccd indi_simulator_guide \
                  indi_simulator_focus indi_simulator_wheel; do
        if ! command -v "${binary}" >/dev/null 2>&1; then
            warn "${binary} is missing; the test suite cannot run without it"
            missing_simulators=$(( missing_simulators + 1 ))
        fi
    done
    if (( missing_simulators == 0 )); then
        ok "simulator drivers (the whole test suite runs on these)"
    else
        problems=$(( problems + missing_simulators ))
    fi

    if [[ ${SKIP_KSTARS} -eq 0 ]]; then
        if command -v kstars >/dev/null 2>&1; then
            ok "kstars"
        else
            warn "kstars is missing"
            problems=$(( problems + 1 ))
        fi
    fi

    if command -v siril-cli >/dev/null 2>&1 || command -v siril >/dev/null 2>&1; then
        ok "siril"
    else
        warn "siril is missing"
        problems=$(( problems + 1 ))
    fi

    local index_count=0
    [[ -d ${INDEX_DIR} ]] && index_count="$(find "${INDEX_DIR}" -name 'index-*.fits' | wc -l)"
    if (( index_count > 0 )); then
        ok "${index_count} astrometry index file(s) in ${INDEX_DIR}"
    else
        warn "no astrometry indices in ${INDEX_DIR}; plate solving will not work offline"
        problems=$(( problems + 1 ))
    fi

    if [[ -s ${VERSIONS_LOCK} ]]; then
        ok "upstream components built on this machine:"
        while read -r component ref sha; do
            info "  ${component} ${ref} ${sha}"
        done < "${VERSIONS_LOCK}"
    else
        warn "no ${VERSIONS_LOCK}; this install was not built by this script"
    fi

    if [[ -x ${REPO_ROOT}/.venv/bin/nocturne ]]; then
        "${REPO_ROOT}/.venv/bin/nocturne" check-config --config-dir "${REPO_ROOT}/config" ||
            { warn "the shipped configuration did not validate"; problems=$(( problems + 1 )); }
    else
        warn "the Nocturne virtual environment is not installed"
        problems=$(( problems + 1 ))
    fi

    if (( problems > 0 )); then
        printf '\n%s%d problem(s) found.%s\n' "${C_RED}" "${problems}" "${C_RESET}" >&2
        return 1
    fi
    printf '\n%sEverything checks out.%s\n' "${C_GREEN}" "${C_RESET}"
}

next_steps() {
    cat <<EOF

${C_BOLD}Next steps${C_RESET}

  1. Edit config/equipment.yaml and config/safety.yaml for your rig, then:

         .venv/bin/nocturne check-config

  2. Run the test suite against the simulators — no hardware needed:

         .venv/bin/pytest

  3. ${C_BOLD}Before any unattended session${C_RESET}, complete the meridian calibration in
     docs/meridian-calibration.md. Nocturne ships with meridian.calibrated set
     to false and refuses supervised and autonomous modes until you have
     measured your own limits. Without a tripod extension the 200PDS strikes
     the tripod legs near the meridian (SPEC section 9.1).

  4. M1 hardware test: connect the Wave 150i over USB serial and follow
     docs/hardware-setup.md. That test is the highest-risk unknown in the
     project and it is worth doing before anything else.
EOF
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --check)        CHECK_ONLY=1 ;;
            --check-packages) report_unknown_packages; exit $? ;;
            --allow-unpinned) ALLOW_UNPINNED=1 ;;
            --build-dir)    BUILD_DIR="$2"; VERSIONS_LOCK="${BUILD_DIR}/versions.lock"; shift ;;
            --index-dir)    INDEX_DIR="$2"; shift ;;
            --jobs)         JOBS="$2"; shift ;;
            --skip-indices) SKIP_INDICES=1 ;;
            --skip-kstars)  SKIP_KSTARS=1 ;;
            --yes|-y)       ASSUME_YES=1 ;;
            --help|-h)      usage; exit 0 ;;
            *)              fail "unknown option: $1 (try --help)" ;;
        esac
        shift
    done

    if [[ ${CHECK_ONLY} -eq 1 ]]; then
        verify || exit 1
        exit 0
    fi

    preflight

    step "About to build INDI, StellarSolver and KStars from source"
    info "This takes one to three hours on a Raspberry Pi 5, mostly KStars."
    info "It is safe to interrupt and re-run: every stage is idempotent."
    confirm "Continue?" || fail "cancelled"

    mkdir -p "${BUILD_DIR}"
    install_build_dependencies
    build_indi
    build_indi_3rdparty
    build_stellarsolver
    build_kstars
    download_indices
    install_nocturne
    verify
    next_steps
}

main "$@"
