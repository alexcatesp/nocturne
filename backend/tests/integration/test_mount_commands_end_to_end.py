"""A mount command, from the executor's public API to the Ekos module — SPEC §9.

The unit tests establish two things separately: that the governor decides a
``SlewToCoordinates`` exactly as it decides the equivalent property write
(``test_safety_mount_commands.py``), and that the bridge's mount calls reach
KStars with their arguments intact (``test_ekos_bridge.py``). Neither says
anything about the wiring between them.

This does, over a real DBus session bus and a real INDI transport, and the claim
it exists for is the one that matters at three in the morning: **a refused slew
does not reach the mount.** Not "raises", not "logs" — the stub on the other end
of the bus records every call it receives, and after a refusal that record is
empty.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from dbus_next import BusType

from nocturne.executor import EkosBridge, Executor, IndiClient, IndiSettings
from nocturne.executor.settings import EkosSettings
from nocturne.safety import SafetyGovernor, SafetyViolation
from nocturne.schemas import SafetyConfig, load_config_bundle
from tests.fixtures.fake_indi import FakeIndiServer
from tests.fixtures.fake_kstars import (
    FakeKStars,
    FakeMountInterface,
    dbus_available,
    session_bus,
)
from tests.fixtures.observing import (
    NIGHT,
    TEST_SITE,
    calibrated_safety_config,
    target_at,
)

pytestmark = [
    pytest.mark.dbus,
    pytest.mark.skipif(not dbus_available(), reason="dbus-daemon is not installed"),
]

FAST = IndiSettings(
    connect_timeout_s=2.0,
    property_timeout_s=2.0,
    write_timeout_s=2.0,
    device_connect_timeout_s=2.0,
    reconnect_initial_delay_s=0.01,
    reconnect_max_delay_s=0.05,
)
IMPATIENT = EkosSettings(module_attach_timeout_s=2.0, module_poll_interval_s=0.02)

#: Clear of the forbidden band and inside the altitude window, at NIGHT.
CLEAR_OF_THE_BAND = -60.0
USABLE_DECLINATION = 20.0


@pytest.fixture
async def indi_server() -> AsyncIterator[FakeIndiServer]:
    fake = FakeIndiServer()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def bus_address() -> AsyncIterator[str]:
    async with session_bus() as address:
        yield address


@pytest.fixture
async def kstars(bus_address: str) -> AsyncIterator[FakeKStars]:
    fake = FakeKStars(bus_address)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
def calibrated(config_dir: Path) -> SafetyConfig:
    return calibrated_safety_config(config_dir)


@pytest.fixture
def uncalibrated(config_dir: Path) -> SafetyConfig:
    return load_config_bundle(config_dir).safety


def mount_of(kstars: FakeKStars) -> FakeMountInterface:
    """The stub mount, typed. The fixture declares the base class."""
    assert isinstance(kstars.mount, FakeMountInterface)
    return kstars.mount


async def executor_for(
    config: SafetyConfig,
    indi_server: FakeIndiServer,
    bus_address: str,
) -> Executor:
    """A wired executor: real client, real bridge, governor at a frozen night."""
    governor = SafetyGovernor(config, site=TEST_SITE, clock=lambda: NIGHT)
    client = IndiClient(FAST.model_copy(update={"port": indi_server.port}))
    bridge = EkosBridge(bus_type=BusType.SESSION, bus_address=bus_address, settings=IMPATIENT)
    running = Executor(client, governor, bridge)
    await running.start()
    await bridge.start_ekos()
    await bridge.attach_modules()
    return running


@pytest.fixture
async def executor(
    calibrated: SafetyConfig,
    indi_server: FakeIndiServer,
    bus_address: str,
    kstars: FakeKStars,
) -> AsyncIterator[Executor]:
    running = await executor_for(calibrated, indi_server, bus_address)
    try:
        yield running
    finally:
        await running.aclose()


class TestAPermittedSlewReachesTheMount:
    async def test_the_coordinates_arrive_unchanged(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        target = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        assert await executor.slew(target.ra_hours, target.dec_deg)
        assert len(mount_of(kstars).slews) == 1
        sent_ra, sent_dec = mount_of(kstars).slews[0]
        assert sent_ra == pytest.approx(target.ra_hours)
        assert sent_dec == pytest.approx(target.dec_deg)

    @pytest.mark.parametrize(
        ("method_name", "recorded"),
        [("park", "park"), ("unpark", "unpark"), ("abort_motion", "abort")],
    )
    async def test_the_other_three_reach_it_too(
        self, executor: Executor, kstars: FakeKStars, method_name: str, recorded: str
    ) -> None:
        assert await getattr(executor, method_name)()
        assert mount_of(kstars).calls == [recorded]

    async def test_a_refusal_from_ekos_is_not_dressed_up_as_success(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        mount_of(kstars).accepts = False
        target = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        assert await executor.slew(target.ra_hours, target.dec_deg) is False


class TestARefusedSlewNeverReachesTheMount:
    """The claim this whole file exists for.

    Every case below asserts the same two things: the call raised, and the stub
    on the far end of the bus saw nothing at all. The second is what makes the
    first mean something — an exception raised *after* the DBus call went out
    would satisfy the first and be a collision.
    """

    async def test_a_target_inside_the_meridian_band(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        on_the_meridian = target_at(0.0, USABLE_DECLINATION, NIGHT)
        with pytest.raises(SafetyViolation, match="meridian"):
            await executor.slew(on_the_meridian.ra_hours, on_the_meridian.dec_deg)
        assert mount_of(kstars).slews == []
        assert mount_of(kstars).calls == []

    async def test_a_target_below_the_altitude_floor(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        below = target_at(CLEAR_OF_THE_BAND, -60.0, NIGHT)
        with pytest.raises(SafetyViolation, match="altitude"):
            await executor.slew(below.ra_hours, below.dec_deg)
        assert mount_of(kstars).slews == []

    async def test_coordinates_that_are_not_a_position(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        with pytest.raises(SafetyViolation):
            await executor.slew(99.0, 0.0)
        assert mount_of(kstars).slews == []

    async def test_nothing_slews_while_the_limits_are_uncalibrated(
        self,
        uncalibrated: SafetyConfig,
        indi_server: FakeIndiServer,
        bus_address: str,
        kstars: FakeKStars,
    ) -> None:
        """ADR 0019, end to end and against the shipped configuration."""
        running = await executor_for(uncalibrated, indi_server, bus_address)
        try:
            target = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
            with pytest.raises(SafetyViolation, match="meridian"):
                await running.slew(target.ra_hours, target.dec_deg)
            assert mount_of(kstars).slews == []
        finally:
            await running.aclose()

    async def test_but_it_still_parks(
        self,
        uncalibrated: SafetyConfig,
        indi_server: FakeIndiServer,
        bus_address: str,
        kstars: FakeKStars,
    ) -> None:
        """The safe state stays reachable on an uncalibrated rig (ADR 0022).

        This is also the positive control for the test above: the two run the
        same executor over the same bus, so "nothing reached the mount" cannot
        be the transport quietly failing.
        """
        running = await executor_for(uncalibrated, indi_server, bus_address)
        try:
            assert await running.park()
            assert await running.abort_motion()
            assert mount_of(kstars).calls == ["park", "abort"]
        finally:
            await running.aclose()


class TestTheOnlyDoorIsTheGovernor:
    async def test_the_executor_hands_out_no_bridge(self, executor: Executor) -> None:
        """A public attribute holding the bridge would be the second door.

        ``test_safety_boundaries.py`` asserts this structurally, over the source.
        This asserts it on a live, fully wired executor, where an accessor added
        by a mixin or a property would also show up.
        """
        exposed = [
            name
            for name in dir(executor)
            if not name.startswith("_")
            and isinstance(getattr(executor, name, None), EkosBridge)
        ]
        assert exposed == []

    async def test_a_forged_approval_performs_nothing(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        class NotAnApproval:
            def __init__(self) -> None:
                self.command = None

        with pytest.raises(SafetyViolation, match="Approval"):
            await executor._perform(NotAnApproval(), timeout=1.0)  # type: ignore[arg-type]
        assert mount_of(kstars).calls == []

    async def test_the_same_path_works_with_a_real_approval(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        """Positive control: ``_perform`` is not simply broken for everything."""
        target = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        assert await executor.slew(target.ra_hours, target.dec_deg)
        assert mount_of(kstars).calls == ["slew"]
