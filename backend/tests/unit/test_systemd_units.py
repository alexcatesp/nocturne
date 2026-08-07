"""The shipped systemd units must point at where the installer actually installs.

`nocturne-kstars.service` named `/usr/local/bin/kstars` while `install.sh` builds
with `CMAKE_INSTALL_PREFIX=/usr` — deliberately, so that one libindi exists on the
machine instead of two. Nothing puts a kstars at `/usr/local/bin`, so the unit
would have failed with `status=203/EXEC`: at night, unattended, on the one process
the Ekos bridge needs.

Nothing in the test suite could have caught that, because nothing looked at the
units at all. This does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
UNIT_DIR = REPO_ROOT / "scripts" / "systemd"


def unit_files() -> list[Path]:
    """Every shipped unit.

    Raises:
        AssertionError: if there are none. A test that walks an empty list passes
            while checking nothing (CLAUDE.md section 2).
    """
    units = sorted(UNIT_DIR.glob("*.service"))
    assert units, f"no unit files under {UNIT_DIR}"
    return units


def exec_paths(text: str) -> list[str]:
    """Absolute paths named in Exec* directives."""
    return [
        word
        for line in text.splitlines()
        if re.match(r"^Exec(Start|StartPre|StartPost|Stop|Reload)=", line)
        for word in line.split("=", 1)[1].split()
        if word.startswith("/")
    ]


def installer_prefix() -> str:
    source = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    match = re.search(
        r'^readonly INSTALL_PREFIX="\$\{NOCTURNE_INSTALL_PREFIX:-([^}]*)\}"',
        source,
        re.MULTILINE,
    )
    assert match is not None, "INSTALL_PREFIX is no longer declared in scripts/install.sh"
    return match.group(1)


#: Paths that come from the OS rather than from anything this project builds.
#: dbus-run-session is Debian's, at a location Debian controls.
DISTRIBUTION_PATHS = frozenset({"/usr/bin/dbus-run-session"})


class TestTheDetectorWorks:
    def test_there_are_units_to_check(self) -> None:
        assert [unit.name for unit in unit_files()] == ["nocturne-kstars.service"]

    def test_exec_paths_are_found(self) -> None:
        """Assert the scan sees something: a regex that matched nothing would
        make every test below pass over an empty list."""
        found = exec_paths((UNIT_DIR / "nocturne-kstars.service").read_text(encoding="utf-8"))
        assert found == ["/usr/bin/dbus-run-session"]

    def test_a_wrong_prefix_is_detected(self) -> None:
        """The positive control: the line exactly as it was before the fix."""
        offender = "[Service]\nExecStart=/usr/bin/dbus-run-session -- /usr/local/bin/kstars\n"
        found = [path for path in exec_paths(offender) if path not in DISTRIBUTION_PATHS]
        assert found == ["/usr/local/bin/kstars"]
        assert not found[0].startswith(installer_prefix() + "/bin/")


class TestNoUnitNamesAPathTheInstallerDoesNotUse:
    @pytest.mark.parametrize("unit", unit_files(), ids=lambda unit: unit.name)
    def test_project_binaries_are_not_hardcoded_to_the_wrong_prefix(self, unit: Path) -> None:
        prefix = installer_prefix()
        for path in exec_paths(unit.read_text(encoding="utf-8")):
            if path in DISTRIBUTION_PATHS:
                continue
            assert path.startswith(f"{prefix}/"), (
                f"{unit.name} runs {path}, but install.sh installs under {prefix}. "
                "Either use the bare name and let PATH resolve it, or fix the prefix."
            )

    @pytest.mark.parametrize("unit", unit_files(), ids=lambda unit: unit.name)
    def test_the_unit_parses_as_ini_with_the_sections_systemd_needs(self, unit: Path) -> None:
        text = unit.read_text(encoding="utf-8")
        for section in ("[Unit]", "[Service]", "[Install]"):
            assert section in text, f"{unit.name} has no {section}"
        assert re.search(r"^ExecStart=", text, re.MULTILINE), f"{unit.name} has no ExecStart"


class TestKStarsRunsHeadlessOnItsOwnBus:
    """The two things that make KStars work with no display and no login.

    Both are load-bearing and neither is obvious, so both are asserted rather
    than left to be rediscovered. docs/ekos-dbus-capture.md uses the same pair
    by hand.
    """

    def unit(self) -> str:
        return (UNIT_DIR / "nocturne-kstars.service").read_text(encoding="utf-8")

    def test_it_uses_the_offscreen_platform_plugin(self) -> None:
        assert "QT_QPA_PLATFORM=offscreen" in self.unit()

    def test_it_creates_its_own_session_bus(self) -> None:
        assert "dbus-run-session" in self.unit()
