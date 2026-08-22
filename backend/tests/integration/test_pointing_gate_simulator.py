"""The pointing gate, against a real driver — SPEC sections 9.1 and 9.2, issue #1.

``test_safety_pointing.py`` asks the governor what it decides. This asks the
question that matters on the rig: when the governor refuses a slew, **does
anything reach the mount?**

The distinction is not academic. A gate that rejects after the write has left
would pass every decision test in the suite and protect nothing, and the only
way to tell the two apart is to watch the far end of a real connection. So
these run ``indiserver`` with ``indi_simulator_telescope``, command a slew that
breaks a limit, and then look at what the driver is reporting.

The positive control is the same path with a permitted target: the mount must
actually move. Without it, a test asserting "the coordinates did not change"
would pass just as well against a driver that was never connected.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from nocturne.executor import Executor, IndiClient, IndiSettings
from nocturne.safety import SafetyGovernor, SafetyViolation
from nocturne.schemas import load_config_bundle
from tests.fixtures.indi_server import (
    SIMULATOR_DEVICES,
    IndiServerProcess,
    simulators_available,
)
from tests.fixtures.observing import NIGHT, TEST_SITE, calibrated_safety_config, target_at

pytestmark = [
    pytest.mark.indi,
    pytest.mark.skipif(
        not simulators_available(),
        reason="indiserver and the INDI simulator drivers are not installed",
    ),
]

MOUNT = SIMULATOR_DEVICES["mount"]
COORDINATES = "EQUATORIAL_EOD_COORD"

#: Real processes, and a permitted slew really does drive the simulated mount
#: across the sky: measured at 12.5 seconds for 90 degrees.
SETTINGS = IndiSettings(
    connect_timeout_s=15.0,
    property_timeout_s=30.0,
    write_timeout_s=60.0,
    device_connect_timeout_s=45.0,
    reconnect_initial_delay_s=0.25,
    reconnect_max_delay_s=2.0,
)

#: Below the 25-degree altitude floor at the test site, at every hour angle.
FAR_SOUTH_DEC = -60.0

#: Comfortably inside every limit at NIGHT: 5 degrees east of the meridian.
PERMITTED_HOUR_ANGLE = -5.0
PERMITTED_DEC = 20.0


async def settle(seconds: float = 2.0) -> None:
    """Long enough for a slew this driver had accepted to become visible."""
    await asyncio.sleep(seconds)


@pytest.fixture(scope="module")
def indi_server() -> Iterator[IndiServerProcess]:
    with IndiServerProcess(["indi_simulator_telescope"]) as server:
        yield server


async def _executor(server: IndiServerProcess, governor: SafetyGovernor) -> Executor:
    running = Executor(IndiClient(SETTINGS.model_copy(update={"port": server.port})), governor)
    await running.start()
    await running.wait_for_device(MOUNT)
    if not running.is_device_connected(MOUNT):
        await running.connect_device(MOUNT)
    await running.wait_for_property(MOUNT, COORDINATES)
    # The vector is defined at 0h 0deg and then corrected to where the mount
    # actually is — parked at the pole — a moment later. Reading it before that
    # arrives gives a "before" the driver has already left behind, and the test
    # then reports a slew that never happened.
    await settle()
    return running


@pytest.fixture
async def calibrated_executor(
    indi_server: IndiServerProcess, config_dir: Path
) -> AsyncIterator[Executor]:
    """An executor whose governor knows the site, the time and the limits."""
    governor = SafetyGovernor(
        calibrated_safety_config(config_dir), site=TEST_SITE, clock=lambda: NIGHT
    )
    running = await _executor(indi_server, governor)
    try:
        yield running
    finally:
        await running.aclose()


@pytest.fixture
async def uncalibrated_executor(
    indi_server: IndiServerProcess, config_dir: Path
) -> AsyncIterator[Executor]:
    """The same, over the configuration the repository actually ships."""
    governor = SafetyGovernor(
        load_config_bundle(config_dir).safety, site=TEST_SITE, clock=lambda: NIGHT
    )
    running = await _executor(indi_server, governor)
    try:
        yield running
    finally:
        await running.aclose()


def coordinates(executor: Executor) -> dict[str, float]:
    vector = executor.get_property(MOUNT, COORDINATES)
    assert vector is not None, "the mount never defined its coordinate vector"
    return {name: float(element.value) for name, element in vector.elements.items()}


#: A mount that is holding position still reports a right ascension that moves:
#: parked at the pole, this driver reports the local sidereal time, which
#: advances by 15 arcseconds a second of real time. So "did not move" is a
#: tolerance, not an equality — and the tolerance is three orders of magnitude
#: below the tens of degrees a refused slew would have produced.
DRIFT_TOLERANCE_HOURS = 0.02
DRIFT_TOLERANCE_DEGREES = 0.5


def assert_did_not_move(before: dict[str, float], after: dict[str, float]) -> None:
    assert after["RA"] == pytest.approx(before["RA"], abs=DRIFT_TOLERANCE_HOURS), (
        "the driver's right ascension moved, so the write reached it before the "
        "governor refused it — the gate is in the wrong place"
    )
    assert after["DEC"] == pytest.approx(before["DEC"], abs=DRIFT_TOLERANCE_DEGREES), (
        "the driver's declination moved, so the write reached it before the "
        "governor refused it — the gate is in the wrong place"
    )


class TestARefusedSlewNeverReachesTheDriver:
    async def test_below_the_altitude_floor(self, calibrated_executor: Executor) -> None:
        before = coordinates(calibrated_executor)
        target = target_at(0.0, FAR_SOUTH_DEC, NIGHT)

        with pytest.raises(SafetyViolation, match="altitude"):
            await calibrated_executor.set_property(
                MOUNT, COORDINATES, {"RA": target.ra_hours, "DEC": target.dec_deg}
            )

        await settle()
        assert_did_not_move(before, coordinates(calibrated_executor))

    async def test_while_the_meridian_limits_are_uncalibrated(
        self, uncalibrated_executor: Executor
    ) -> None:
        """ADR 0019, on the shipped configuration, through the real path."""
        before = coordinates(uncalibrated_executor)
        target = target_at(PERMITTED_HOUR_ANGLE, PERMITTED_DEC, NIGHT)

        with pytest.raises(SafetyViolation, match="meridian"):
            await uncalibrated_executor.set_property(
                MOUNT, COORDINATES, {"RA": target.ra_hours, "DEC": target.dec_deg}
            )

        await settle()
        assert_did_not_move(before, coordinates(uncalibrated_executor))

    async def test_open_loop_motion(self, calibrated_executor: Executor) -> None:
        """No coordinate to check, so no way to permit it (SPEC section 9.2)."""
        with pytest.raises(SafetyViolation, match="TELESCOPE_MOTION_NS"):
            await calibrated_executor.set_property(
                MOUNT, "TELESCOPE_MOTION_NS", {"MOTION_NORTH": True, "MOTION_SOUTH": False}
            )


class TestAPermittedSlewDoesReachIt:
    """The positive control. Without this, the tests above prove nothing."""

    async def test_the_mount_moves_to_a_permitted_target(
        self, calibrated_executor: Executor
    ) -> None:
        target = target_at(PERMITTED_HOUR_ANGLE, PERMITTED_DEC, NIGHT)
        before = coordinates(calibrated_executor)
        assert abs(before["DEC"] - target.dec_deg) > DRIFT_TOLERANCE_DEGREES * 2, (
            "the mount already sits at the target, so arriving there would prove "
            "nothing about the command"
        )

        result = await calibrated_executor.set_property(
            MOUNT, COORDINATES, {"RA": target.ra_hours, "DEC": target.dec_deg}
        )

        assert result is not None
        arrived = coordinates(calibrated_executor)
        assert arrived["DEC"] == pytest.approx(target.dec_deg, abs=0.5)
        assert arrived["RA"] == pytest.approx(target.ra_hours, abs=0.05)
