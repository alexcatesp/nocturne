"""``install.sh`` and its apt packages: what it checks must be what it installs.

The first end-to-end run of the installer failed five times, and all five were
one defect: it *verified* dependencies without *installing* them, from a list
that existed twice. ``--check-packages`` confirmed the Qt6 and KF6 names were
available and nothing ever ran ``apt-get install`` on them; ``wcslib-dev`` and
``libeigen3-dev`` appeared only in an inline list inside a stage that ran too
late; ``libopencv-dev`` appeared nowhere at all. Each one stopped a configure
step an hour into a compile.

So the tests here are not "does the list contain wcslib-dev". They are:

* the set the check verifies and the set the install asks apt for are the **same
  set**, established by running both against stubs and comparing, and
* that comparison is proven to notice when they differ, by pointing it at a
  variant of the script where they do.

Both are absence-shaped assertions — nothing is missing, nothing diverges — so
both carry a positive control (CLAUDE.md section 2). The equality check would
also pass vacuously if the list were empty, which is why the size of it is
asserted too.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "scripts" / "install.sh"

#: The five that stopped the first real run, in the order they stopped it.
#: Each must survive in whatever the installer asks apt for.
FIRST_RUN_CASUALTIES = (
    "wcslib-dev",
    "qt6-base-dev",
    "libkf6config-dev",
    "libopencv-dev",
    "libeigen3-dev",
)

#: Functions that must go through required_packages() rather than reading an
#: array. These are the three that see the package list at all.
PACKAGE_CONSUMERS = ("report_unknown_packages", "check_apt_packages", "install_apt_packages")


def installer_source() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def extract_function(name: str, source: str | None = None) -> str:
    body = source if source is not None else installer_source()
    match = re.search(rf"^{name}\(\) \{{$.*?^\}}$", body, re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name}() is no longer defined in scripts/install.sh"
    return match.group(0)


def extract_package_arrays(source: str | None = None) -> str:
    """Every ``readonly *_PACKAGES=( ... )`` declaration, verbatim.

    Raises:
        AssertionError: if none are found. A helper that quietly returned an
            empty string would disarm every test in this file at once, and each
            of them would then pass while comparing nothing to nothing.
    """
    body = source if source is not None else installer_source()
    found = re.findall(r"^readonly [A-Z0-9_]*PACKAGES=\(.*?\)$", body, re.MULTILINE | re.DOTALL)
    assert found, "no *_PACKAGES arrays found in scripts/install.sh"
    return "\n".join(found)


def package_array_names(source: str | None = None) -> list[str]:
    body = source if source is not None else installer_source()
    names = re.findall(r"^readonly ([A-Z0-9_]*PACKAGES)=\(", body, re.MULTILINE)
    assert names, "no *_PACKAGES arrays found in scripts/install.sh"
    return names


# --------------------------------------------------------------------------
# A bash harness holding the real functions, with apt stubbed out
# --------------------------------------------------------------------------

PRELUDE = """
set -euo pipefail
fail() { printf 'INSTALLER FAILED: %s\\n' "$*" >&2; exit 1; }
step() { :; }
info() { :; }
ok()   { :; }
warn() { :; }
as_root() { "$@"; }
"""


def write_apt_stubs(directory: Path, *, known: list[str], record: Path) -> None:
    """An apt-get that records what it was asked to install, and an apt-cache
    and dpkg-query that know exactly ``known``.

    ``apt-cache policy`` prints a ``name:`` line per package it recognises and
    says nothing at all about the rest. That silence is what the check reads, so
    the stub reproduces it rather than printing an error.
    """
    directory.mkdir(parents=True, exist_ok=True)
    listed = "\n".join(known)

    (directory / "apt-get").write_text(
        "#!/usr/bin/env bash\n"
        "[[ $1 == install ]] || exit 0\n"
        "shift\n"
        'for arg in "$@"; do\n'
        "  [[ $arg == -* ]] && continue\n"
        f'  printf "%s\\n" "$arg" >> {record}\n'
        "done\n",
        encoding="utf-8",
    )
    (directory / "apt-cache").write_text(
        "#!/usr/bin/env bash\n"
        f"known=$(cat <<'NAMES'\n{listed}\nNAMES\n)\n"
        "case $1 in\n"
        "  policy)\n"
        "    shift\n"
        '    for want in "$@"; do\n'
        '      grep -qxF "$want" <<<"$known" &&\n'
        '        printf "%s:\\n  Installed: (none)\\n  Candidate: 1.0\\n" "$want"\n'
        "    done\n"
        "    ;;\n"
        '  show) grep -qxF "$2" <<<"$known" || exit 1 ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    # Nothing is installed yet, on purpose: it makes check_apt_packages sort
    # every name into "installable" or "unknown" rather than skipping it.
    (directory / "dpkg-query").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")

    for stub in ("apt-get", "apt-cache", "dpkg-query"):
        (directory / stub).chmod(0o755)


def run_harness(
    tmp_path: Path,
    command: str,
    *,
    skip_kstars: bool = False,
    known: list[str] | None = None,
    source: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run ``command`` against the installer's real package functions.

    Returns the completed process and the list of names ``apt-get install`` was
    asked for, in call order.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    stub_dir = tmp_path / "bin"
    record = tmp_path / "installed.txt"
    record.write_text("", encoding="utf-8")
    body = source if source is not None else installer_source()
    write_apt_stubs(stub_dir, known=known if known is not None else [], record=record)

    script = "\n".join(
        [
            PRELUDE,
            f"SKIP_KSTARS={1 if skip_kstars else 0}",
            extract_package_arrays(body),
            *(
                extract_function(name, body)
                for name in ("required_packages", *PACKAGE_CONSUMERS)
            ),
            extract_function("refuse_empty_package_list", body),
            command,
        ]
    )
    environment = dict(os.environ, PATH=f"{stub_dir}:{os.environ['PATH']}")
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, env=environment
    )
    installed = [line for line in record.read_text(encoding="utf-8").split() if line]
    return result, installed


class TestTheHarnessWorks:
    """Without these, everything below could pass for the wrong reason."""

    def test_bash_is_available(self) -> None:
        assert shutil.which("bash") is not None

    def test_the_stubs_are_reached_instead_of_the_real_apt(self, tmp_path: Path) -> None:
        """If PATH injection failed, the real apt would answer and the results
        below would describe this machine rather than the fixture."""
        result, installed = run_harness(tmp_path, "install_apt_packages")
        assert result.returncode == 0, result.stderr
        assert "build-essential" in installed

    def test_the_array_extractor_finds_every_array(self) -> None:
        names = package_array_names()
        assert "QT6_BUILD_PACKAGES" in names
        assert "KF6_BUILD_PACKAGES" in names
        assert len(names) >= 4, names

    def test_the_array_extractor_refuses_a_source_with_no_arrays(self) -> None:
        """The positive control for the helper the rest of the file rests on."""
        with pytest.raises(AssertionError):
            extract_package_arrays("#!/usr/bin/env bash\necho hello\n")


class TestTheCheckAndTheInstallAskForTheSameSet:
    """The defect that cost five runs, stated as an invariant."""

    def check_set(
        self, tmp_path: Path, *, skip_kstars: bool = False, source: str | None = None
    ) -> set[str]:
        """What --check-packages verifies: with apt knowing nothing, every name
        it looks at comes back as unrecognised, so the output *is* the set."""
        result, _ = run_harness(
            tmp_path,
            "report_unknown_packages || true",
            known=[],
            skip_kstars=skip_kstars,
            source=source,
        )
        return set(result.stdout.split())

    def install_set(
        self, tmp_path: Path, *, skip_kstars: bool = False, source: str | None = None
    ) -> set[str]:
        result, installed = run_harness(
            tmp_path, "install_apt_packages", skip_kstars=skip_kstars, source=source
        )
        assert result.returncode == 0, result.stderr
        return set(installed)

    @pytest.mark.parametrize("skip_kstars", [False, True])
    def test_they_are_identical(self, tmp_path: Path, skip_kstars: bool) -> None:
        checked = self.check_set(tmp_path / "check", skip_kstars=skip_kstars)
        installed = self.install_set(tmp_path / "install", skip_kstars=skip_kstars)

        assert checked == installed, (
            f"checked but not installed: {sorted(checked - installed)}; "
            f"installed but never checked: {sorted(installed - checked)}"
        )

    def test_the_set_is_not_empty(self, tmp_path: Path) -> None:
        """Two empty sets are equal, and that is what a broken extractor
        produces. The equality above means nothing without this."""
        assert len(self.install_set(tmp_path)) > 20

    @pytest.mark.parametrize("package", FIRST_RUN_CASUALTIES)
    def test_every_package_the_first_run_died_on_is_installed(
        self, tmp_path: Path, package: str
    ) -> None:
        assert package in self.install_set(tmp_path)

    def test_a_divergence_is_detected(self, tmp_path: Path) -> None:
        """The positive control.

        This is the old shape of the bug: an install stage that adds packages
        of its own, from a list the check never sees. If the comparison above
        cannot see this, it cannot see anything.
        """
        mutated = installer_source().replace(
            'as_root apt-get install -y --no-install-recommends "${wanted[@]}"',
            'as_root apt-get install -y --no-install-recommends "${wanted[@]}" libsmuggled-dev',
        )
        assert "libsmuggled-dev" in mutated, "the mutation did not apply"

        checked = self.check_set(tmp_path / "check", source=mutated)
        installed = self.install_set(tmp_path / "install", source=mutated)
        assert installed - checked == {"libsmuggled-dev"}

    def test_skipping_kstars_narrows_both_sides_together(self, tmp_path: Path) -> None:
        full = self.install_set(tmp_path / "full", skip_kstars=False)
        lean = self.install_set(tmp_path / "lean", skip_kstars=True)

        assert lean < full, "--skip-kstars should ask for strictly fewer packages"
        # libopencv-dev is ~500 MB and KStars is the only thing that wants it.
        assert "libopencv-dev" in full - lean
        assert "libkf6config-dev" in full - lean
        # StellarSolver is built either way, and it needs these.
        assert {"wcslib-dev", "qt6-base-dev"} <= lean


class TestItNamesWhatAptDoesNotKnow:
    """--check-packages: silence means the list is good on this release."""

    def unknown(self, tmp_path: Path, known: list[str]) -> list[str]:
        result, _ = run_harness(tmp_path, "report_unknown_packages", known=known)
        return result.stdout.split()

    def all_names(self, tmp_path: Path) -> list[str]:
        _, installed = run_harness(tmp_path, "install_apt_packages")
        return installed

    def test_an_unknown_name_is_reported(self, tmp_path: Path) -> None:
        """The positive control: it must fire on a name apt does not have."""
        names = self.all_names(tmp_path / "names")
        known = [name for name in names if name != "libkf6i18n-dev"]
        assert len(known) == len(names) - 1, "libkf6i18n-dev is no longer in the list"

        assert self.unknown(tmp_path / "run", known) == ["libkf6i18n-dev"]

    def test_it_fails_when_something_is_unknown(self, tmp_path: Path) -> None:
        result, _ = run_harness(tmp_path, "report_unknown_packages", known=[])
        assert result.returncode != 0

    def test_a_clean_list_prints_nothing_and_succeeds(self, tmp_path: Path) -> None:
        """Silence is the answer the operator is looking for."""
        names = self.all_names(tmp_path / "names")
        result, _ = run_harness(tmp_path / "run", "report_unknown_packages", known=names)

        assert result.stdout == ""
        assert result.returncode == 0

    def test_it_prints_one_name_per_line_and_nothing_else(self, tmp_path: Path) -> None:
        """Meant to be pasted back, so no banners, counts or decoration."""
        names = set(self.all_names(tmp_path / "names"))
        result, _ = run_harness(tmp_path / "run", "report_unknown_packages", known=[])
        for line in result.stdout.splitlines():
            assert line in names, line


class TestTheGateRefusesAnUnobtainableName:
    def test_it_stops_the_install(self, tmp_path: Path) -> None:
        """A name apt has never heard of is not a slow install, it is a wrong
        package list, and no amount of apt-get fixes it."""
        result, installed = run_harness(tmp_path, "check_apt_packages", known=[])

        assert result.returncode != 0
        assert "apt does not know these packages" in result.stderr
        assert installed == [], "nothing may be installed once the gate has failed"

    def test_it_passes_when_everything_is_available(self, tmp_path: Path) -> None:
        _, names = run_harness(tmp_path / "names", "install_apt_packages")
        result, _ = run_harness(tmp_path / "run", "check_apt_packages", known=names)
        assert result.returncode == 0, result.stderr


class TestAnEmptyListIsRefusedRatherThanReportedClean:
    """An empty package list looks exactly like success from the outside.

    ``--check-packages`` would print nothing, which is the all-clear; the
    install stage would run apt-get over no arguments and report done. Both read
    as confirmation, so the emptiness itself has to be the error.
    """

    def empty_arrays(self) -> str:
        source = installer_source()
        return re.sub(
            r"^readonly ([A-Z0-9_]*PACKAGES)=\(.*?^\)$",
            r"readonly \1=()",
            source,
            flags=re.MULTILINE | re.DOTALL,
        )

    @pytest.mark.parametrize("function", PACKAGE_CONSUMERS)
    def test_each_consumer_refuses(self, tmp_path: Path, function: str) -> None:
        mutated = self.empty_arrays()
        assert "PACKAGES=()" in mutated, "the mutation did not apply"

        result, installed = run_harness(tmp_path, function, source=mutated)
        assert result.returncode != 0, result.stdout
        assert "required_packages() produced nothing" in result.stderr
        assert installed == []


class TestOnlyOneFunctionReadsThePackageArrays:
    """Structural: the arrays have one reader, so a second list cannot appear.

    Behavioural equality above proves the check and the install agree *today*.
    This is what stops a fourth consumer being added tomorrow that reads the
    arrays directly and drifts.
    """

    def readers(self, source: str) -> dict[str, list[str]]:
        """Which function expands which ``*_PACKAGES`` array."""
        found: dict[str, list[str]] = {}
        for match in re.finditer(
            r"^([a-z_0-9]+)\(\) \{$(.*?)^\}$", source, re.MULTILINE | re.DOTALL
        ):
            arrays = re.findall(r"\$\{([A-Z0-9_]*PACKAGES)\[@\]\}", match.group(2))
            if arrays:
                found[match.group(1)] = sorted(set(arrays))
        return found

    def test_the_detector_sees_the_one_legitimate_reader(self) -> None:
        """Assert the scan found something: a regex that matched nothing would
        make the test below pass while looking at an empty dictionary."""
        readers = self.readers(installer_source())
        assert set(readers) == {"required_packages"}, readers
        assert len(readers["required_packages"]) == len(package_array_names())

    def test_required_packages_reads_every_array(self) -> None:
        """An array declared and never read is a dependency nobody installs."""
        declared = set(package_array_names())
        read = set(self.readers(installer_source())["required_packages"])
        assert declared == read, f"declared but never required: {sorted(declared - read)}"

    def test_a_second_reader_is_detected(self) -> None:
        """The positive control: a synthetic offender doing what the old
        install_kstars_dependencies() did."""
        offender = (
            installer_source()
            + "\n"
            + "install_extra_dependencies() {\n"
            + '    as_root apt-get install -y "${QT6_BUILD_PACKAGES[@]}"\n'
            + "}\n"
        )
        readers = self.readers(offender)
        assert readers.get("install_extra_dependencies") == ["QT6_BUILD_PACKAGES"]


class TestTheOptionIsWiredUp:
    def test_check_packages_sets_a_flag_rather_than_running_immediately(self) -> None:
        """It has to run *after* the loop, because --skip-kstars changes its
        answer and may come after it on the command line."""
        source = installer_source()
        assert "--check-packages) CHECK_PACKAGES=1" in source
        assert "report_unknown_packages" in extract_function("main", source)

    def test_the_usage_line_is_a_comment(self) -> None:
        """It was not, once, and the script executed itself until bash gave up.

        The top-of-file block is prose that ``usage()`` strips the ``#`` from,
        so an un-commented line there is a live statement — and this one
        re-invoked the installer, recursing until SHLVL hit 1000.
        """
        for line in installer_source().splitlines():
            if "./scripts/install.sh --check-packages" in line:
                assert line.lstrip().startswith("#"), line

    def test_help_lists_every_option_the_parser_accepts(self) -> None:
        """usage() reads the header block by shape rather than by line number.
        A fixed range silently stopped covering it once already."""
        result = subprocess.run(
            ["bash", str(INSTALLER), "--help"], capture_output=True, text=True, check=True
        )
        parsed = set(re.findall(r"^\s+(--[a-z-]+)\)", installer_source(), re.MULTILINE))
        assert parsed, "no options found in the argument parser"
        for option in parsed:
            assert option in result.stdout, option
