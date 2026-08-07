"""``install.sh --check-packages`` — the Qt6/KF6 name check.

The operator verified five of the twenty-four names by hand on Trixie. This is
the command that checks the rest in one go, so its output is what decides
whether the KStars build stage is safe to run. A check that silently reported
"nothing unknown" because it could not see the list would be worse than no
check: it would read as confirmation.

So it is exercised against a stub ``apt-cache`` that knows exactly which names
exist, and must name the ones that do not. Positive control first — CLAUDE.md
section 2.
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


def extract_function(name: str) -> str:
    source = INSTALLER.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{$.*?^\}}$", source, re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name}() is no longer defined in scripts/install.sh"
    return match.group(0)


def stub_apt_cache(directory: Path, known: list[str]) -> Path:
    """An apt-cache that knows exactly ``known``, shaped like the real one.

    ``apt-cache policy`` prints a ``name:`` line per package it recognises and
    says nothing at all about the rest. That silence is what the check reads, so
    the stub has to reproduce it rather than print an error.
    """
    binary = directory / "apt-cache"
    listed = "\n".join(known)
    binary.write_text(
        "#!/usr/bin/env bash\n"
        "[[ $1 == policy ]] || exit 0\n"
        "shift\n"
        f"known=$(cat <<'NAMES'\n{listed}\nNAMES\n)\n"
        'for want in "$@"; do\n'
        '  if grep -qxF "$want" <<<"$known"; then\n'
        '    printf "%s:\\n  Installed: (none)\\n  Candidate: 1.0\\n" "$want"\n'
        "  fi\n"
        "done\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def run_check(tmp_path: Path, known: list[str]) -> subprocess.CompletedProcess[str]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    stub_apt_cache(stub_dir, known)

    script = (
        "set -euo pipefail\n"
        "QT6_BUILD_PACKAGES=(qt6-base-dev qt6-svg-dev)\n"
        "KF6_BUILD_PACKAGES=(libkf6config-dev libkf6i18n-dev)\n"
        f"{extract_function('report_unknown_packages')}\n"
        "report_unknown_packages\n"
    )
    environment = dict(os.environ, PATH=f"{stub_dir}:{os.environ['PATH']}")
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


ALL_FOUR = ["qt6-base-dev", "qt6-svg-dev", "libkf6config-dev", "libkf6i18n-dev"]


class TestTheHarnessWorks:
    """Without these the tests below would pass for the wrong reason."""

    def test_bash_is_available(self) -> None:
        assert shutil.which("bash") is not None

    def test_the_stub_is_reached_instead_of_the_real_apt_cache(self, tmp_path: Path) -> None:
        """If PATH injection failed, the real apt-cache would answer and the
        results below would be about this machine rather than the fixture."""
        result = run_check(tmp_path, known=[])
        assert sorted(result.stdout.split()) == sorted(ALL_FOUR), result.stdout


class TestItNamesWhatAptDoesNotKnow:
    def test_an_unknown_name_is_reported(self, tmp_path: Path) -> None:
        """The positive control: the check must fire on a name that is missing."""
        known = [name for name in ALL_FOUR if name != "libkf6i18n-dev"]
        result = run_check(tmp_path, known=known)

        assert result.stdout.split() == ["libkf6i18n-dev"]
        assert result.returncode != 0

    def test_several_unknown_names_are_all_reported(self, tmp_path: Path) -> None:
        result = run_check(tmp_path, known=["qt6-base-dev"])
        assert sorted(result.stdout.split()) == sorted(
            ["qt6-svg-dev", "libkf6config-dev", "libkf6i18n-dev"]
        )

    def test_a_clean_list_prints_nothing_and_succeeds(self, tmp_path: Path) -> None:
        """Silence is the answer the operator is looking for."""
        result = run_check(tmp_path, known=ALL_FOUR)

        assert result.stdout == ""
        assert result.returncode == 0

    def test_it_prints_one_name_per_line_and_nothing_else(self, tmp_path: Path) -> None:
        """Meant to be pasted back, so no banners, counts or decoration."""
        result = run_check(tmp_path, known=["qt6-base-dev"])
        for line in result.stdout.splitlines():
            assert line in ALL_FOUR, line


class TestTheShippedListIsWhatGetsChecked:
    """The command and the install must read the same names, or it proves nothing."""

    def installer(self) -> str:
        return INSTALLER.read_text(encoding="utf-8")

    def test_the_check_reads_both_shipped_arrays(self) -> None:
        body = extract_function("report_unknown_packages")
        assert "QT6_BUILD_PACKAGES" in body
        assert "KF6_BUILD_PACKAGES" in body

    def test_the_install_gate_reads_the_same_arrays(self) -> None:
        body = extract_function("check_kstars_build_dependencies")
        assert "QT6_BUILD_PACKAGES" in body
        assert "KF6_BUILD_PACKAGES" in body

    @pytest.mark.parametrize("array", ["QT6_BUILD_PACKAGES", "KF6_BUILD_PACKAGES"])
    def test_neither_array_is_empty(self, array: str) -> None:
        """An empty list would make --check-packages print nothing: a false all-clear."""
        match = re.search(rf"{array}=\((.*?)\)", self.installer(), re.DOTALL)
        assert match is not None
        assert match.group(1).split()

    def test_the_option_is_wired_up_and_exits(self) -> None:
        assert "--check-packages) report_unknown_packages; exit $?" in self.installer()

    def test_the_usage_line_is_a_comment(self) -> None:
        """It was not, once, and the script executed itself until bash gave up.

        The top-of-file block is prose that ``usage()`` strips the ``#`` from,
        so an un-commented line there is a live statement — and this one
        re-invoked the installer, recursing until SHLVL hit 1000.
        """
        for line in self.installer().splitlines():
            if "./scripts/install.sh --check-packages" in line:
                assert line.lstrip().startswith("#"), line
