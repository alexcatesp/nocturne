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

from nocturne.executor import (
    EkosBridge,
    Executor,
    IndiClient,
    IndiSettings,
    UnknownTargetError,
)
from nocturne.executor.settings import EkosSettings
from nocturne.safety import SafetyGovernor, SafetyViolation
from nocturne.safety.sky import DEGREES_PER_HOUR, julian_date
from nocturne.schemas import SafetyConfig, load_config_bundle
from tests.fixtures.fake_indi import FakeIndiServer
from tests.fixtures.fake_kstars import (
    CatalogueEntry,
    FakeKStars,
    FakeMountInterface,
    dbus_available,
    session_bus,
)
from tests.fixtures.observing import (
    EAST_LIMIT_DEG,
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
    # The stub's KStars runs the same clock as the governor. A test that wants
    # them to disagree says so; the default is that they do not, because a
    # freshness rule firing everywhere would hide every other refusal.
    fake.kstars.julian_date = julian_date(NIGHT)
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


def place_target_in_the_catalogue(
    kstars: FakeKStars,
    name: str,
    position: object,
    *,
    object_type: str = "Galaxy",
) -> None:
    """Put ``position`` in the stub catalogue under ``name``.

    KStars reports the catalogue in **degrees** and takes ``Mount.slew`` in
    **hours**, so the entry is written in degrees here — the same way round as
    the real one. A bridge that forgot to divide by fifteen would resolve M 31
    to a right ascension fifteen times too large and be caught by the tests
    below rather than by a telescope pointing at the wrong part of the sky.
    """
    entry = CatalogueEntry(
        name=name,
        ra_degrees=position.ra_hours * DEGREES_PER_HOUR,  # type: ignore[attr-defined]
        dec_degrees=position.dec_deg,  # type: ignore[attr-defined]
        type_name=object_type,
    )
    kstars.kstars.catalogue[name.upper()] = entry


class TestASyncReachesTheMountAndIsBounded:
    async def test_a_small_sync_arrives_with_its_coordinates(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        mount_of(kstars).position = (here.ra_hours, here.dec_deg)
        solved_dec = here.dec_deg + 1.0

        assert await executor.sync(here.ra_hours, solved_dec)
        assert len(mount_of(kstars).syncs) == 1
        sent_ra, sent_dec = mount_of(kstars).syncs[0]
        assert sent_ra == pytest.approx(here.ra_hours)
        assert sent_dec == pytest.approx(solved_dec)

    async def test_a_sync_is_not_a_slew(self, executor: Executor, kstars: FakeKStars) -> None:
        """They take the same two doubles. Sending one as the other would move
        the mount instead of correcting its model, and nothing in the signature
        would notice."""
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        mount_of(kstars).position = (here.ra_hours, here.dec_deg)
        await executor.sync(here.ra_hours, here.dec_deg + 1.0)
        assert mount_of(kstars).calls == ["sync"]
        assert mount_of(kstars).slews == []

    async def test_a_large_sync_never_reaches_the_mount(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        mount_of(kstars).position = (here.ra_hours, here.dec_deg)
        with pytest.raises(SafetyViolation, match="sync_tolerance"):
            await executor.sync(here.ra_hours, here.dec_deg + 40.0)
        assert mount_of(kstars).syncs == []

    async def test_the_mount_is_read_before_the_command_is_built(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        """How far a sync moves the model is measured against what the mount
        says *now*, not against anything cached. Moving the stub mount and
        repeating the identical call must change the verdict."""
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        solved_dec = here.dec_deg + 1.0

        mount_of(kstars).position = (here.ra_hours, here.dec_deg)
        assert await executor.sync(here.ra_hours, solved_dec)

        mount_of(kstars).position = (here.ra_hours, here.dec_deg - 40.0)
        with pytest.raises(SafetyViolation):
            await executor.sync(here.ra_hours, solved_dec)


class TestANamedTargetIsResolvedThroughKStars:
    async def test_a_known_target_is_resolved_and_slewed_to_by_name(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        place_target_in_the_catalogue(kstars, "NGC 7000", here)

        assert await executor.slew_to_target("NGC 7000")
        assert mount_of(kstars).named == ["NGC 7000"]
        assert mount_of(kstars).calls == ["gotoTarget"]

    async def test_the_resolution_converts_degrees_to_hours(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        """The factor of fifteen, at the other end of the same operation.

        KStars reports the catalogue in degrees. If the bridge passed that
        through, a target at RA 4 h would be validated as though it were at
        RA 60 h — which is not a position at all, so the governor would refuse
        a perfectly good target and the failure would look like a limit.
        """
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        place_target_in_the_catalogue(kstars, "NGC 7000", here)

        resolved = await executor._mount().resolve_target("NGC 7000")
        assert resolved is not None
        assert resolved.ra_hours == pytest.approx(here.ra_hours, abs=1e-3)
        assert resolved.dec_deg == pytest.approx(here.dec_deg, abs=1e-3)

    async def test_a_target_below_the_floor_never_reaches_the_mount(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        below = target_at(CLEAR_OF_THE_BAND, -70.0, NIGHT)
        place_target_in_the_catalogue(kstars, "NGC 0000", below)

        with pytest.raises(SafetyViolation, match="altitude"):
            await executor.slew_to_target("NGC 0000")
        assert mount_of(kstars).named == []
        assert mount_of(kstars).calls == []

    async def test_a_planet_is_refused_for_moving(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        """It sits at a perfectly legal place in the stub catalogue; the type is
        the only thing wrong with it (SPEC section 1.2)."""
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        place_target_in_the_catalogue(kstars, "Neptune", here, object_type="Planet")

        with pytest.raises(SafetyViolation, match="deep sky"):
            await executor.slew_to_target("Neptune")
        assert mount_of(kstars).calls == []

    async def test_the_same_place_as_a_galaxy_is_permitted(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        """Positive control for the refusal above."""
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        place_target_in_the_catalogue(kstars, "Neptune", here, object_type="Galaxy")
        assert await executor.slew_to_target("Neptune")

    async def test_a_kstars_clock_that_has_drifted_is_refused(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        """The check that cannot be made any other way.

        The coordinates are fine, the object holds still, the target is well
        placed. KStars simply precessed it to three hours ago, because its
        simulated clock is stopped — and nothing about the answer says so.
        """
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        place_target_in_the_catalogue(kstars, "NGC 7000", here)
        kstars.kstars.julian_date = julian_date(NIGHT) - 0.125  # three hours

        with pytest.raises(SafetyViolation, match="clock"):
            await executor.slew_to_target("NGC 7000")
        assert mount_of(kstars).calls == []

    async def test_an_unknown_name_is_not_a_safety_refusal(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        """Nothing unsafe happened. Dressing a miss up as a safety violation
        would make the safety log useless for reading what was actually
        stopped."""
        with pytest.raises(UnknownTargetError, match="does not contain"):
            await executor.slew_to_target("NGC 99999")
        assert mount_of(kstars).calls == []

    async def test_a_named_sync_reaches_the_mount(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        mount_of(kstars).position = (here.ra_hours, here.dec_deg)
        place_target_in_the_catalogue(
            kstars, "NGC 7000", target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION + 1.0, NIGHT)
        )

        assert await executor.sync_to_target("NGC 7000")
        assert mount_of(kstars).calls == ["syncTarget"]
        assert mount_of(kstars).named == ["NGC 7000"]

    async def test_a_named_sync_that_is_too_large_never_reaches_it(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        mount_of(kstars).position = (here.ra_hours, here.dec_deg)
        place_target_in_the_catalogue(
            kstars, "NGC 7000", target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION + 40.0, NIGHT)
        )

        with pytest.raises(SafetyViolation):
            await executor.sync_to_target("NGC 7000")
        assert mount_of(kstars).calls == []


class TestTheModelResetAndTheFlipOffset:
    async def test_a_reset_is_refused_while_the_mount_is_not_parked(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        mount_of(kstars).park_status = 0  # PARK_UNKNOWN
        with pytest.raises(SafetyViolation, match="not parked"):
            await executor.reset_mount_model()
        assert mount_of(kstars).calls == []

    async def test_a_reset_reaches_the_mount_when_parked(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        mount_of(kstars).park_status = 1  # PARK_PARKED
        assert await executor.reset_mount_model()
        assert mount_of(kstars).calls == ["resetModel"]

    @pytest.mark.parametrize("park_status", [0, 2, 3, 4, 5])
    async def test_no_other_park_status_counts_as_parked(
        self, executor: Executor, kstars: FakeKStars, park_status: int
    ) -> None:
        """PARK_UNKNOWN, PARKING, UNPARKING, UNPARKED and ERROR. Parked is a
        state to be in, not one to be heading towards."""
        mount_of(kstars).park_status = park_status
        with pytest.raises(SafetyViolation, match="not parked"):
            await executor.reset_mount_model()
        assert mount_of(kstars).calls == []

    async def test_a_reset_reports_false_when_there_is_no_model(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        """KStars checks first and does nothing. That is not a failure, and the
        caller has to be able to tell the difference."""
        mount_of(kstars).park_status = 1
        mount_of(kstars).has_alignment_model = False
        assert await executor.reset_mount_model() is False

    async def test_the_flip_offset_reaches_ekos_in_degrees(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        """The number that goes over the bus is the number in degrees.

        KStars' own DBus definition names this argument ``hours``. It is
        degrees, and if anything here converted "helpfully" to match the name
        the flip would be configured fifteen times too early or too late.
        """
        margin = calibrated_safety_config(Path("config")).limits.meridian.safety_margin_deg
        east_edge = EAST_LIMIT_DEG - margin

        assert await executor.set_meridian_flip(enabled=True, offset_deg=east_edge)
        sent = mount_of(kstars).flip
        assert sent is not None
        assert sent[0] is True
        assert sent[1] == pytest.approx(east_edge)

    async def test_a_late_flip_never_reaches_ekos(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        """Ekos' own idiom — a small positive number of degrees past the
        meridian — is refused on this rig, because the tube is inside the band
        thirty degrees before the meridian."""
        with pytest.raises(SafetyViolation, match="keep-out band"):
            await executor.set_meridian_flip(enabled=True, offset_deg=1.0)
        assert mount_of(kstars).flip is None
        assert mount_of(kstars).calls == []

    async def test_disabling_it_always_reaches_ekos(
        self, executor: Executor, kstars: FakeKStars
    ) -> None:
        assert await executor.set_meridian_flip(enabled=False)
        sent = mount_of(kstars).flip
        assert sent is not None
        assert sent[0] is False
        assert sent[1] == pytest.approx(0.0)
