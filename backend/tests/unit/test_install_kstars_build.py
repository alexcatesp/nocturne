"""The two things that made the KStars stage impossible, and the disk.

``-DBUILD_WITH_QT6=ON`` is not a preference. KStars 3.8.3's CMakeLists.txt
declares ``option(BUILD_WITH_QT6 "Build using Qt6" OFF)`` — default **off** — and
without the flag its configure step goes looking for Qt5 5.12.7, which Trixie
does not ship at all. On the platform this project deliberately chose for its
packaged Qt6/KF6 (ADR 0008), omitting one flag makes the build impossible.

What makes that dangerous rather than merely annoying is the failure mode of the
*fix*: CMake ignores ``-D`` for a variable no CMakeLists declares, with a note at
the end of a log nobody reads. If the option is ever renamed upstream, passing it
would silently do nothing and the build would fall back to hunting for Qt5 —
the original failure, now wearing the disguise of a flag that was set. So the
option's presence in the checked-out tree is verified rather than assumed, and
the test for that check is here.

The disk is the third: the KStars build tree reached about 9 GB on the reference
rig, and a build that runs out of space dies at roughly 80%, an hour in.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "scripts" / "install.sh"

#: Line 17 of KStars 3.8.3, verbatim. The default is OFF, and that is the point.
KSTARS_OPTION_LINE = 'option(BUILD_WITH_QT6 "Build using Qt6" OFF)'


def installer_source() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def extract_function(name: str, source: str | None = None) -> str:
    body = source if source is not None else installer_source()
    match = re.search(rf"^{name}\(\) \{{$.*?^\}}$", body, re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name}() is no longer defined in scripts/install.sh"
    return match.group(0)


PRELUDE = """
set -euo pipefail
fail() { printf 'INSTALLER FAILED: %s\\n' "$*" >&2; exit 1; }
step() { :; }
info() { printf 'info: %s\\n' "$*"; }
ok()   { :; }
warn() { printf 'warn: %s\\n' "$*" >&2; }
"""


def run_snippet(
    functions: list[str], command: str, **variables: str
) -> subprocess.CompletedProcess[str]:
    script = "\n".join(
        [
            PRELUDE,
            *(f'{name}="{value}"' for name, value in variables.items()),
            *(extract_function(name) for name in functions),
            command,
        ]
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ),
    )


# --------------------------------------------------------------------------
# require_qt6_option
# --------------------------------------------------------------------------


def kstars_tree(directory: Path, cmakelists: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "CMakeLists.txt").write_text(cmakelists, encoding="utf-8")
    return directory


REAL_HEAD = f"""cmake_minimum_required(VERSION 3.16.0 FATAL_ERROR)
PROJECT(kstars VERSION 3.8.3 LANGUAGES CXX C)

{KSTARS_OPTION_LINE}
option(BUILD_TESTING "Build tests" ON)
"""


class TestTheQt6OptionIsVerifiedInTheCheckedOutTree:
    def test_it_accepts_the_real_3_8_3_declaration(self, tmp_path: Path) -> None:
        tree = kstars_tree(tmp_path / "kstars", REAL_HEAD)
        result = run_snippet(["require_qt6_option"], f'require_qt6_option "{tree}"')
        assert result.returncode == 0, result.stderr

    def test_it_refuses_a_tree_where_the_option_is_gone(self, tmp_path: Path) -> None:
        """The positive control.

        This is the case the check exists for: upstream renames the option,
        ``-DBUILD_WITH_QT6=ON`` becomes a no-op CMake mentions in passing, and
        the build silently looks for a Qt5 that is not installed.
        """
        renamed = REAL_HEAD.replace(
            KSTARS_OPTION_LINE, 'option(KSTARS_USE_QT6 "Build using Qt6" OFF)'
        )
        assert KSTARS_OPTION_LINE not in renamed, "the mutation did not apply"

        tree = kstars_tree(tmp_path / "kstars", renamed)
        result = run_snippet(["require_qt6_option"], f'require_qt6_option "{tree}"')

        assert result.returncode != 0
        assert "BUILD_WITH_QT6" in result.stderr
        assert "Nothing has been built" in result.stderr

    def test_it_refuses_a_directory_that_is_not_a_source_tree(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        result = run_snippet(["require_qt6_option"], f'require_qt6_option "{empty}"')
        assert result.returncode != 0
        assert "not a KStars source tree" in result.stderr

    def test_a_commented_out_option_does_not_count(self, tmp_path: Path) -> None:
        """``grep BUILD_WITH_QT6`` would pass on a tree that only mentions it."""
        mentioned = REAL_HEAD.replace(KSTARS_OPTION_LINE, f"# was: {KSTARS_OPTION_LINE}")
        tree = kstars_tree(tmp_path / "kstars", mentioned)
        result = run_snippet(["require_qt6_option"], f'require_qt6_option "{tree}"')
        assert result.returncode != 0


class TestTheFlagIsActuallyPassed:
    """Verifying the option exists is worthless if the flag is not then set."""

    def test_build_kstars_passes_the_flag(self) -> None:
        body = extract_function("build_kstars")
        assert "-DBUILD_WITH_QT6=ON" in body

    def test_build_kstars_verifies_the_option_before_configuring(self) -> None:
        """Order matters twice over: after the fetch, because there is no tree
        before it; before the build, because that is the point."""
        body = extract_function("build_kstars")
        positions = {
            name: body.index(name)
            for name in ("fetch_source", "require_qt6_option", "cmake_build_install")
        }
        assert positions["fetch_source"] < positions["require_qt6_option"]
        assert positions["require_qt6_option"] < positions["cmake_build_install"]

    def test_the_space_check_runs_before_anything_is_fetched(self) -> None:
        """Refusing after a clone has already used the disk helps nobody."""
        body = extract_function("build_kstars")
        assert body.index("require_space_for_kstars") < body.index("fetch_source")


# --------------------------------------------------------------------------
# Disk space
# --------------------------------------------------------------------------


class TestFreeSpaceIsMeasured:
    def test_it_reports_a_number_for_a_real_directory(self, tmp_path: Path) -> None:
        result = run_snippet(["free_gb_under"], f'free_gb_under "{tmp_path}"')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().isdigit(), result.stdout

    def test_it_walks_up_to_a_directory_that_exists(self, tmp_path: Path) -> None:
        """The build directory does not exist the first time this is asked."""
        missing = tmp_path / "not" / "created" / "yet"
        result = run_snippet(["free_gb_under"], f'free_gb_under "{missing}"')
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().isdigit(), result.stdout


class TestTheKStarsStageRefusesToStartWithoutRoom:
    """Measured: about 9 GB of build tree, plus ~0.5 GB for libopencv-dev."""

    def run_check(self, free_gb: str) -> subprocess.CompletedProcess[str]:
        # free_gb_under is replaced rather than extracted: what is under test is
        # the decision, and a real filesystem cannot be asked to have 8 GB free.
        script = "\n".join(
            [
                PRELUDE,
                'BUILD_DIR="/nonexistent/build"',
                "KSTARS_BUILD_GB=10",
                f'free_gb_under() {{ printf "%s" "{free_gb}"; }}',
                extract_function("require_space_for_kstars"),
                "require_space_for_kstars",
            ]
        )
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )

    def test_it_refuses_when_there_is_not_enough(self) -> None:
        """The positive control: 8 GB is where the build dies at 80%."""
        result = self.run_check("8")
        assert result.returncode != 0
        assert "8 GB free" in result.stderr
        assert "--skip-kstars" in result.stderr

    def test_it_proceeds_when_there_is_room(self) -> None:
        result = self.run_check("20")
        assert result.returncode == 0, result.stderr

    def test_the_boundary_is_the_configured_size(self) -> None:
        assert self.run_check("10").returncode == 0
        assert self.run_check("9").returncode != 0

    def test_it_projects_the_requirement_before_deciding(self) -> None:
        """The operator asked for the number in advance, not only on refusal."""
        result = self.run_check("20")
        assert "10 GB" in result.stdout
        assert "20 GB free" in result.stdout

    def test_it_does_not_refuse_when_the_size_is_unknown(self) -> None:
        """df failing is not evidence of a full disk. Warn and carry on."""
        result = self.run_check("")
        assert result.returncode == 0
        assert "cannot determine free space" in result.stderr

    @pytest.mark.parametrize("name", ["KSTARS_BUILD_GB", "REQUIRED_DISK_GB"])
    def test_the_size_is_configuration_not_a_literal(self, name: str) -> None:
        assert re.search(rf"^readonly {name}=\d+$", installer_source(), re.MULTILINE)
