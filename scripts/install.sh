#!/usr/bin/env bash
#
# Nocturne installer for Raspberry Pi OS 64-bit — SPEC sections 4 and 14 (M1).
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
#   KStars/Ekos             built against the INDI above, run headless.
#   Siril                   stacking (SPEC section 10.3).
#   Nocturne                a Python virtual environment and this package.
#
# Every stage is idempotent: a stage whose output is already present and at the
# required version is skipped. Run it again after a failure.
#
# Usage:
#   ./scripts/install.sh                 install everything
#   ./scripts/install.sh --check         verify an existing install, change nothing
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

# KStars is DELIBERATELY UNSET. See docs/decisions/0006-building-from-source.md.
# invent.kde.org is not reachable from the environment this was written in, and
# the KDE mirror on GitHub is missing whole release series (no v3.6.x, no
# v3.7.x), so it cannot be used to confirm a tag. Rather than guess, the build
# stops and asks. Confirm the tag on a machine that can reach KDE:
#   git ls-remote --tags https://invent.kde.org/education/kstars.git | grep 3.8
# then:  export NOCTURNE_KSTARS_VERSION=<tag>
readonly KSTARS_VERSION="${NOCTURNE_KSTARS_VERSION:-}"

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

readonly BUILD_DIR_DEFAULT="${HOME}/.cache/nocturne-build"
readonly REQUIRED_ARCH="aarch64"
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
  --allow-unpinned     permit a non-tag upstream ref (build is not reproducible)
  --build-dir DIR      where to clone and build (default ~/.cache/nocturne-build)
  --index-dir DIR      where to put astrometry indices (default /usr/share/astrometry)
  --jobs N             parallel compile jobs (default: nproc)
  --skip-indices       do not download astrometry index files
  --skip-kstars        do not build KStars (INDI and Nocturne only)
  --yes                do not prompt before the long build
  --help               this text

Environment overrides:
  NOCTURNE_INDI_VERSION, NOCTURNE_STELLARSOLVER_VERSION, NOCTURNE_KSTARS_VERSION,
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
fetch_source() {
    local repo="$1" ref="$2" dir="$3"
    local name
    name="$(basename "${dir}")"

    if [[ -d ${dir}/.git ]]; then
        info "updating ${name}"
        git -C "${dir}" fetch --quiet --tags --depth 1 origin "${ref}"
        git -C "${dir}" checkout --quiet FETCH_HEAD
    else
        info "cloning ${name} at ${ref}"
        git clone --quiet --depth 1 --branch "${ref}" "${repo}" "${dir}"
    fi

    require_pinned_ref "${name}" "${ref}" "${dir}"
    record_built_version "${name}" "${ref}" "$(git -C "${dir}" rev-parse HEAD)"
}

# A branch name is a moving target: two installs a week apart would build
# different code, and a bug could appear between two nights with nothing in
# this repository having changed.
require_pinned_ref() {
    local name="$1" ref="$2" dir="$3"
    if git -C "${dir}" describe --exact-match --tags HEAD >/dev/null 2>&1; then
        return 0
    fi
    if [[ ${ALLOW_UNPINNED} -eq 1 ]]; then
        warn "${name} is at '${ref}', which is not a tag. The build is NOT reproducible."
        return 0
    fi
    fail "${name} ref '${ref}' is not a tag, so this build would not be reproducible.
    Two installs of the same Nocturne commit could then behave differently, and a
    change upstream could alter the rig between two nights.
    Set the matching NOCTURNE_*_VERSION variable to a tag, or pass
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
    mkdir -p "${build_dir}"
    cmake -S "${source_dir}" -B "${build_dir}" \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DCMAKE_BUILD_TYPE=Release \
        "$@"
    cmake --build "${build_dir}" -j "${JOBS}"
    as_root cmake --install "${build_dir}"
    as_root ldconfig
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

    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091  # provided by the OS, not this repository
        . /etc/os-release
        info "OS: ${PRETTY_NAME:-unknown}"
        if [[ ${ID:-} != "debian" && ${ID_LIKE:-} != *debian* ]]; then
            warn "this installer is written for Raspberry Pi OS (Debian). Continuing anyway."
        fi
    fi

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
        extra-cmake-modules qtbase5-dev qtdeclarative5-dev libqt5svg5-dev \
        qtpositioning5-dev qtmultimedia5-dev libqt5websockets5-dev \
        libqt5sql5-sqlite kdoctools-dev libkf5config-dev libkf5crash-dev \
        libkf5doctools-dev libkf5i18n-dev libkf5newstuff-dev \
        libkf5notifications-dev libkf5plotting-dev libkf5xmlgui-dev \
        libeigen3-dev wcslib-dev
    ok "KStars dependencies installed"
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

    fetch_source "${INDI_REPO}" "${INDI_VERSION}" "${BUILD_DIR}/indi"
    cmake_build_install "${BUILD_DIR}/indi" "indi-build"

    existing="$(installed_indi_version)"
    version_at_least "${existing:-0}" "${INDI_MINIMUM}" ||
        fail "built INDI reports version '${existing:-unknown}', expected >= ${INDI_MINIMUM}"
    ok "INDI ${existing}"
}

build_indi_3rdparty() {
    step "Building the ZWO drivers (indi-3rdparty)"
    fetch_source "${INDI_3RDPARTY_REPO}" "${INDI_VERSION}" "${BUILD_DIR}/indi-3rdparty"

    # The vendor libraries must be installed before the drivers that link them.
    info "libraries first (ASI SDK)"
    cmake_build_install "${BUILD_DIR}/indi-3rdparty" "indi-3rdparty-libs" -DBUILD_LIBS=1

    info "then the drivers"
    cmake_build_install "${BUILD_DIR}/indi-3rdparty" "indi-3rdparty-drivers"

    local missing=()
    local driver
    for driver in indi_asi_ccd indi_asi_focuser indi_asi_wheel indi_eqmod_telescope; do
        command -v "${driver}" >/dev/null 2>&1 || missing+=("${driver}")
    done
    if (( ${#missing[@]} > 0 )); then
        fail "these drivers were not installed: ${missing[*]}
    They are named in config/equipment.yaml and Nocturne cannot reach the rig without them."
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
    step "Building KStars ${KSTARS_VERSION:-<unpinned>}"
    if [[ ${SKIP_KSTARS} -eq 1 ]]; then
        warn "skipped at your request; Nocturne cannot drive Ekos without it"
        return
    fi
    if command -v kstars >/dev/null 2>&1; then
        ok "KStars is already installed ($(kstars --version 2>&1 | head -n1))"
        return
    fi
    if [[ -z ${KSTARS_VERSION} ]]; then
        fail "no KStars version is pinned, and this installer will not guess one.
    Building the wrong release is not cosmetic: the Optical Trains DBus interface
    the executor bridge targets arrived in 3.8.2, and an older build would have
    Nocturne working around the absence of an interface that exists upstream.

    Find the current stable tag on a machine that can reach KDE:
        git ls-remote --tags https://invent.kde.org/education/kstars.git | grep 3.8
    then re-run with:
        NOCTURNE_KSTARS_VERSION=<tag> ./scripts/install.sh

    Or --skip-kstars to install INDI and Nocturne now and add KStars later."
    fi
    install_kstars_dependencies
    fetch_source "${KSTARS_REPO}" "${KSTARS_VERSION}" "${BUILD_DIR}/kstars"
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
