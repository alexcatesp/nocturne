"""The one part of install.sh that edits somebody else's source.

docs/FIELD-NOTES-M1.md section 5.2: INDI v2.2.4 forces ``-Werror`` and Trixie's
GCC emits warnings it treats as fatal, so the build dies at 32 % on a driver for
hardware we do not own. ``-DCMAKE_CXX_FLAGS="-Wno-error"`` does not help — the
project appends ``-Werror`` after user flags — so the installer strips it at
source.

The dangerous part is what it must *not* touch. The same file holds
``check_c_compiler_flag(...)`` probes that mention the same flags; patching one
of those changes what the build detects rather than what it enforces. The
distinction is the whole test.

The function is extracted from the shipped ``scripts/install.sh`` rather than
copied here, so this cannot pass against a version of it that no longer exists.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "scripts" / "install.sh"

#: The shape of indi-3rdparty's cmake_modules/CMakeCommon.cmake at v2.2.4:
#: four forced flags, and four capability probes that mention the same words.
THIRD_PARTY_CMAKE = """\
# Excerpt shaped like indi-3rdparty v2.2.4 cmake_modules/CMakeCommon.cmake.
include(CheckCCompilerFlag)

check_c_compiler_flag(-Werror COMPILER_SUPPORTS_WERROR)
set(COMP_FLAGS "${COMP_FLAGS} -Wall -Wextra")

if (COMPILER_SUPPORTS_WERROR)
    set(COMP_FLAGS "${COMP_FLAGS} -Werror")
endif ()

check_c_compiler_flag(-Werror=stringop-truncation SUPPORTS_STRINGOP)
if (SUPPORTS_STRINGOP)
    set(COMP_FLAGS "${COMP_FLAGS} -Werror=stringop-truncation")
endif ()

check_c_compiler_flag(-Werror=unused-parameter SUPPORTS_UNUSED_PARAM)
if (SUPPORTS_UNUSED_PARAM)
    set(COMP_FLAGS "${COMP_FLAGS} -Werror=unused-parameter")
endif ()

check_c_compiler_flag(-Werror=unused-but-set-variable SUPPORTS_UNUSED_SET)
if (SUPPORTS_UNUSED_SET)
    set(COMP_FLAGS "${COMP_FLAGS} -Werror=unused-but-set-variable")
endif ()
"""

#: indi core has one site, and spells the command in capitals.
CORE_CMAKE = """\
# Excerpt shaped like indi v2.2.4 cmake_modules/CMakeCommon.cmake.
CHECK_C_COMPILER_FLAG(-Werror COMPILER_SUPPORTS_WERROR)
SET(COMP_FLAGS "${COMP_FLAGS} -Wall")
SET(COMP_FLAGS "${COMP_FLAGS} -Werror")
"""


def extract_function(name: str) -> str:
    """Pull one shell function out of install.sh, verbatim."""
    source = INSTALLER.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{$.*?^\}}$", source, re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name}() is no longer defined in scripts/install.sh"
    return match.group(0)


#: Stubs for the helpers install.sh defines elsewhere. ``fail`` exits non-zero,
#: which is the behaviour under test.
HARNESS = """
set -euo pipefail
INDI_VERSION="v2.2.4"
info() { :; }
fail() { echo "FAIL: $*" >&2; exit 1; }
"""


def run_strip(source_dir: Path, expected: int) -> subprocess.CompletedProcess[str]:
    script = f"{HARNESS}\n{extract_function('strip_werror')}\n"
    script += f'strip_werror "{source_dir}" {expected}\n'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


@pytest.fixture
def third_party(tmp_path: Path) -> Path:
    modules = tmp_path / "cmake_modules"
    modules.mkdir()
    (modules / "CMakeCommon.cmake").write_text(THIRD_PARTY_CMAKE, encoding="utf-8")
    return tmp_path


def patched(source_dir: Path) -> str:
    return (source_dir / "cmake_modules" / "CMakeCommon.cmake").read_text(encoding="utf-8")


class TestStrippingWerror:
    def test_bash_is_available(self) -> None:
        """Guard: a missing shell would make every test below vacuously pass."""
        assert shutil.which("bash") is not None

    def test_all_four_forced_flags_are_removed(self, third_party: Path) -> None:
        result = run_strip(third_party, 4)
        assert result.returncode == 0, result.stderr

        for line in patched(third_party).splitlines():
            if line.lstrip().lower().startswith("set(comp_flags"):
                assert "-Werror" not in line, line

    def test_the_capability_probes_are_left_exactly_as_they_were(
        self, third_party: Path
    ) -> None:
        """Patching a probe changes what the build detects, not what it enforces."""
        run_strip(third_party, 4)
        probes = [
            line
            for line in patched(third_party).splitlines()
            if "check_c_compiler_flag" in line.lower()
        ]
        assert probes == [
            line
            for line in THIRD_PARTY_CMAKE.splitlines()
            if "check_c_compiler_flag" in line.lower()
        ]
        assert len(probes) == 4

    def test_the_other_warning_flags_survive(self, third_party: Path) -> None:
        """-Wall and -Wextra are wanted. Only the fatal part comes out."""
        run_strip(third_party, 4)
        assert "-Wall" in patched(third_party)
        assert "-Wextra" in patched(third_party)

    def test_the_single_site_in_indi_core_is_removed(self, tmp_path: Path) -> None:
        """The core spells it SET(...) in capitals."""
        modules = tmp_path / "cmake_modules"
        modules.mkdir()
        (modules / "CMakeCommon.cmake").write_text(CORE_CMAKE, encoding="utf-8")

        result = run_strip(tmp_path, 1)
        assert result.returncode == 0, result.stderr

        patched_text = patched(tmp_path)
        assert 'SET(COMP_FLAGS "${COMP_FLAGS} -Werror")' not in patched_text
        assert "CHECK_C_COMPILER_FLAG(-Werror" in patched_text
        assert "-Wall" in patched_text


class TestItRefusesRatherThanGuessing:
    """A different upstream tag may set the flag somewhere else entirely."""

    def test_a_different_count_stops_the_build_before_anything_is_compiled(
        self, third_party: Path
    ) -> None:
        result = run_strip(third_party, 1)

        assert result.returncode != 0
        assert "found 4" in result.stderr
        assert patched(third_party) == THIRD_PARTY_CMAKE, "the file was modified anyway"

    def test_a_missing_file_stops_the_build(self, tmp_path: Path) -> None:
        result = run_strip(tmp_path, 4)

        assert result.returncode != 0
        assert "does not exist" in result.stderr

    def test_the_refusal_says_nothing_has_been_built(self, third_party: Path) -> None:
        """An hour into a compile is the wrong place to discover this."""
        assert "Nothing has been built" in run_strip(third_party, 2).stderr
