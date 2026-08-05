"""Limits that M1 does not enforce yet — the red flag for M2.

https://github.com/alexcatesp/nocturne/issues/1

M1 registers no rules against ``SetProperty``, so a raw write to the mount's
coordinate vector commands a slew with nothing checking the altitude floor, the
meridian hour angle or the Sun. On a 200PDS with no tripod extension that is
the collision case ``docs/meridian-calibration.md`` exists to prevent. Nothing
in the M1 codebase issues such a write, but the door is open. ADR 0007 records
why it cannot be closed before the limits exist.

These tests are marked ``xfail(strict=True)``. They report as expected failures
today. The moment M2 registers the rules and one of them starts passing,
``strict=True`` turns the **whole suite red** until the marker is deliberately
removed — so the limits cannot land half-done, and the reminder cannot be lost
the way a docstring can.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nocturne.executor import Executor, IndiClient, IndiSettings, PropertyKind
from nocturne.safety import SafetyGovernor, SafetyViolation
from nocturne.schemas import load_config_bundle
from tests.fixtures.fake_indi import FakeIndiServer, FakeProperty
from tests.fixtures.indi_server import SIMULATOR_DEVICES

ISSUE = "https://github.com/alexcatesp/nocturne/issues/1"

MOUNT = SIMULATOR_DEVICES["mount"]
COORDINATES = "EQUATORIAL_EOD_COORD"

FAST = IndiSettings(
    connect_timeout_s=2.0,
    property_timeout_s=2.0,
    write_timeout_s=2.0,
    device_connect_timeout_s=2.0,
    reconnect_initial_delay_s=0.01,
    reconnect_max_delay_s=0.05,
)

deferred_to_m2 = pytest.mark.xfail(
    strict=True,
    reason=(
        f"M1 registers no rules against SetProperty; the limits land in M2. {ISSUE}. "
        "When this starts passing, strict=True fails the suite until the marker is "
        "removed on purpose."
    ),
)


@pytest.fixture
async def executor(config_dir: Path) -> AsyncIterator[Executor]:
    """An executor over a fake mount that accepts a coordinate write."""
    mount = {
        "CONNECTION": FakeProperty(
            name="CONNECTION",
            kind=PropertyKind.SWITCH,
            values={"CONNECT": True, "DISCONNECT": False},
            rule="OneOfMany",
        ),
        COORDINATES: FakeProperty(
            name=COORDINATES,
            kind=PropertyKind.NUMBER,
            values={"RA": 5.0, "DEC": 20.0},
        ),
    }
    server = FakeIndiServer({MOUNT: mount})
    await server.start()
    governor = SafetyGovernor(load_config_bundle(config_dir).safety)
    running = Executor(IndiClient(FAST.model_copy(update={"port": server.port})), governor)
    await running.start()
    await running.wait_for_property(MOUNT, COORDINATES)
    try:
        yield running
    finally:
        await running.aclose()
        await server.stop()


class TestPointingLimitsAreNotEnforcedYet:
    """SPEC sections 9.1 and 9.2, deferred to M2 with the rules that need them."""

    @deferred_to_m2
    async def test_a_coordinate_below_the_altitude_floor_is_refused(
        self, executor: Executor
    ) -> None:
        """altitude_min_deg is 25 on this rig; this target never rises above it."""
        with pytest.raises(SafetyViolation, match="altitude"):
            await executor.set_property(MOUNT, COORDINATES, {"RA": 0.0, "DEC": -85.0})

    @deferred_to_m2
    async def test_a_coordinate_past_the_meridian_limit_is_refused(
        self, executor: Executor
    ) -> None:
        """While meridian.calibrated is false, no pointing command may be permitted."""
        with pytest.raises(SafetyViolation, match="meridian"):
            await executor.set_property(MOUNT, COORDINATES, {"RA": 23.5, "DEC": 60.0})

    @deferred_to_m2
    async def test_a_coordinate_near_the_sun_is_refused(self, executor: Executor) -> None:
        """sun_avoidance_deg is 30, enforced always — day or night."""
        with pytest.raises(SafetyViolation, match="sun"):
            await executor.set_property(MOUNT, COORDINATES, {"RA": 8.9, "DEC": 17.5})


class TestTheGapIsRecordedWhereItWillBeSeen:
    """The documentation half of the reminder. These pass now and must keep passing."""

    def test_the_governor_registers_no_rules_for_set_property(self) -> None:
        from nocturne.safety import COMMAND_RULES, SetProperty

        assert COMMAND_RULES[SetProperty] == (), (
            "rules now exist for SetProperty. Remove the xfail markers in this "
            f"module and close {ISSUE}."
        )

    def test_an_adr_records_the_limitation(self, repo_root: Path) -> None:
        adr = repo_root / "docs" / "decisions" / "0007-m1-pointing-is-ungated.md"
        assert adr.exists(), "ADR 0007 must exist while this limitation stands"
        text = adr.read_text(encoding="utf-8")
        assert "issues/1" in text
        assert "EQUATORIAL_EOD_COORD" in text
