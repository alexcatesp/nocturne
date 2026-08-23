"""Ekos DBus bridge — SPEC sections 3 and 4.

Connect, disconnect, drive Ekos, and recover when KStars leaves the bus and
comes back. Run against a stub service on a private session bus; see
``tests/fixtures/fake_kstars`` for why.

What this file does **not** establish is that the stub resembles KStars. That is
``tests/unit/test_ekos_recorded_interface.py``, which checks both the bridge and
the stub against the interface recorded from the rig — and which exists because
this file was fully green while the bridge called ``INDI.connect`` with a device
name (issue #2).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from dbus_next import BusType

from nocturne.executor.ekos import (
    BridgeState,
    EkosBridge,
    EkosError,
    EkosInterfaceError,
    EkosUnavailableError,
)
from nocturne.executor.settings import EkosSettings
from tests.fixtures.fake_kstars import (
    FakeKStars,
    FakeMountInterface,
    dbus_available,
    session_bus,
)

pytestmark = [
    pytest.mark.dbus,
    pytest.mark.skipif(not dbus_available(), reason="dbus-daemon is not installed"),
]


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


#: Waiting for module objects that a stub creates synchronously. Long enough
#: that a slow machine does not fail, short enough that a test asserting the
#: refusal does not take a minute to do it.
IMPATIENT = EkosSettings(module_attach_timeout_s=2.0, module_poll_interval_s=0.02)


@pytest.fixture
async def bridge(bus_address: str, kstars: FakeKStars) -> AsyncIterator[EkosBridge]:
    connected = EkosBridge(
        bus_type=BusType.SESSION,
        bus_address=bus_address,
        reconnect_delay_s=0.05,
        settings=IMPATIENT,
    )
    await connected.connect()
    try:
        yield connected
    finally:
        await connected.aclose()


async def wait_for(condition: object, timeout: float = 5.0) -> None:
    assert callable(condition)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met in time")


class TestConnection:
    async def test_connects_to_ekos(self, bridge: EkosBridge) -> None:
        assert bridge.is_connected
        assert bridge.state is BridgeState.CONNECTED

    async def test_refuses_when_kstars_is_absent(self, bus_address: str) -> None:
        bridge = EkosBridge(bus_type=BusType.SESSION, bus_address=bus_address)
        with pytest.raises(EkosUnavailableError, match=r"org\.kde\.kstars"):
            await bridge.connect()
        await bridge.aclose()

    async def test_refuses_an_interface_missing_methods(self, bus_address: str) -> None:
        """A KStars whose DBus surface has moved must fail loudly, not silently."""
        fake = FakeKStars(bus_address, minimal=True)
        await fake.start()
        bridge = EkosBridge(bus_type=BusType.SESSION, bus_address=bus_address)
        try:
            with pytest.raises(EkosInterfaceError) as excinfo:
                await bridge.connect()
            message = str(excinfo.value)
            assert "connectDevices" in message
            assert "nocturne/executor/ekos.py" in message
        finally:
            await bridge.aclose()
            await fake.stop()

    async def test_close_is_idempotent(self, bridge: EkosBridge) -> None:
        await bridge.aclose()
        await bridge.aclose()
        assert bridge.state is BridgeState.CLOSED

    async def test_a_closed_bridge_does_not_reconnect(self, bridge: EkosBridge) -> None:
        await bridge.aclose()
        with pytest.raises(EkosError, match="closed"):
            await bridge.connect()


class TestCalls:
    async def test_start_and_stop_ekos(self, bridge: EkosBridge, kstars: FakeKStars) -> None:
        await bridge.start_ekos()
        await bridge.stop_ekos()
        assert getattr(kstars.ekos, "calls", []) == ["start", "stop"]

    async def test_connect_and_disconnect_devices(
        self, bridge: EkosBridge, kstars: FakeKStars
    ) -> None:
        await bridge.connect_devices()
        await bridge.disconnect_devices()
        assert getattr(kstars.ekos, "calls", []) == ["connectDevices", "disconnectDevices"]

    async def test_lists_devices(self, bridge: EkosBridge) -> None:
        devices = await bridge.devices()
        assert "Telescope Simulator" in devices
        assert len(devices) == 5

    async def test_attaches_to_an_indiserver(
        self, bridge: EkosBridge, kstars: FakeKStars
    ) -> None:
        """``INDI.connect`` takes a host and a port, not a device name.

        This test used to be ``test_connect_one_device`` and passed a device
        name, because both the bridge and the stub believed the same wrong
        thing. The recorded interface says
        ``connect(host: s, port: i) -> b`` (issue #2).
        """
        assert await bridge.connect_indiserver("localhost", 7624) is True
        assert kstars.indi.servers == {("localhost", 7624)}

        assert await bridge.disconnect_indiserver("localhost", 7624) is True
        assert kstars.indi.servers == set()

    async def test_there_is_no_per_device_connect_on_the_bridge(
        self, bridge: EkosBridge
    ) -> None:
        """The positive control for the correction above.

        If ``connect_device`` ever comes back, it is the old misconception
        returning: ``org.kde.kstars.INDI`` has no per-device connect, and the
        one Nocturne uses is the governed ``CONNECTION`` write in
        ``IndiClient`` — not anything on this bridge.
        """
        assert not hasattr(bridge, "connect_device")
        assert not hasattr(bridge, "disconnect_device")
        assert hasattr(bridge, "connect_indiserver")

    async def test_calling_while_disconnected_is_refused(self, bus_address: str) -> None:
        bridge = EkosBridge(bus_type=BusType.SESSION, bus_address=bus_address)
        with pytest.raises(EkosError, match="not connected"):
            await bridge.start_ekos()
        await bridge.aclose()


class TestKStarsRestart:
    async def test_notices_kstars_leaving_the_bus(
        self, bridge: EkosBridge, kstars: FakeKStars
    ) -> None:
        states: list[BridgeState] = []
        bridge.subscribe(states.append)

        await kstars.stop()
        await wait_for(lambda: bridge.state is BridgeState.RECONNECTING)
        assert BridgeState.RECONNECTING in states
        assert not bridge.is_connected

    async def test_reattaches_when_kstars_comes_back(
        self, bridge: EkosBridge, bus_address: str, kstars: FakeKStars
    ) -> None:
        await kstars.stop()
        await wait_for(lambda: bridge.state is BridgeState.RECONNECTING)

        replacement = FakeKStars(bus_address)
        await replacement.start()
        try:
            await wait_for(lambda: bridge.state is BridgeState.CONNECTED)
            await bridge.start_ekos()
            assert getattr(replacement.ekos, "calls", []) == ["start"]
        finally:
            await replacement.stop()

    async def test_calls_are_refused_while_kstars_is_away(
        self, bridge: EkosBridge, kstars: FakeKStars
    ) -> None:
        """Fail safe: a command must not be silently dropped while Ekos is gone."""
        await kstars.stop()
        await wait_for(lambda: bridge.state is BridgeState.RECONNECTING)
        with pytest.raises(EkosError, match="not connected"):
            await bridge.connect_devices()


def mount_of(kstars: FakeKStars) -> FakeMountInterface:
    """The stub mount, typed. The fixture declares the base class."""
    assert isinstance(kstars.mount, FakeMountInterface)
    return kstars.mount


class TestTheModuleObjectsAppearOnlyWhenEkosStarts:
    """The order the recorded captures show, exercised rather than assumed."""

    async def test_the_mount_is_not_attached_by_connecting(self, bridge: EkosBridge) -> None:
        assert bridge.is_connected
        assert not bridge.is_mount_attached

    async def test_attaching_before_ekos_starts_is_refused(
        self, bridge: EkosBridge, kstars: FakeKStars
    ) -> None:
        assert not kstars.modules_exported
        with pytest.raises(EkosInterfaceError, match="did not appear"):
            await bridge.attach_modules()

    async def test_the_refusal_names_the_step_that_was_skipped(
        self, bridge: EkosBridge
    ) -> None:
        with pytest.raises(EkosInterfaceError) as raised:
            await bridge.attach_modules(timeout_s=0.1)
        assert "start_ekos" in str(raised.value)

    async def test_attaching_after_ekos_starts_succeeds(
        self, bridge: EkosBridge, kstars: FakeKStars
    ) -> None:
        await bridge.start_ekos()
        await bridge.attach_modules()
        assert bridge.is_mount_attached
        assert kstars.modules_exported

    async def test_attaching_twice_is_harmless(self, bridge: EkosBridge) -> None:
        await bridge.start_ekos()
        await bridge.attach_modules()
        await bridge.attach_modules()
        assert bridge.is_mount_attached

    async def test_a_mount_module_missing_methods_is_refused(self, bus_address: str) -> None:
        """The containment check, on the module as well as on Ekos itself."""
        fake = FakeKStars(bus_address, minimal_mount=True)
        await fake.start()
        connected = EkosBridge(
            bus_type=BusType.SESSION, bus_address=bus_address, settings=IMPATIENT
        )
        try:
            await connected.connect()
            await connected.start_ekos()
            with pytest.raises(EkosInterfaceError) as raised:
                await connected.attach_modules(timeout_s=0.1)
            assert "slew" in str(raised.value)
        finally:
            await connected.aclose()
            await fake.stop()

    async def test_the_same_stub_attaches_when_it_is_complete(self, bridge: EkosBridge) -> None:
        """Positive control for the refusal above: only the missing methods
        distinguish the two, so the complete one has to succeed."""
        await bridge.start_ekos()
        await bridge.attach_modules()
        assert bridge.is_mount_attached


class TestTheMountCalls:
    @pytest.fixture
    async def attached(self, bridge: EkosBridge) -> EkosBridge:
        await bridge.start_ekos()
        await bridge.attach_modules()
        return bridge

    async def test_a_slew_arrives_with_the_coordinates_unchanged(
        self, attached: EkosBridge, kstars: FakeKStars
    ) -> None:
        """The factor-of-fifteen test.

        Right ascension goes out in hours (``kstars-mount-dbus-units.txt``).
        Anything that converted it to degrees on the way would show up here as
        5.5 becoming 82.5, and nowhere else — the signature is two doubles
        either way.
        """
        assert await attached.mount_slew(5.5, -12.25)
        assert len(mount_of(kstars).slews) == 1
        sent_ra, sent_dec = mount_of(kstars).slews[0]
        assert sent_ra == pytest.approx(5.5)
        assert sent_dec == pytest.approx(-12.25)

    @pytest.mark.parametrize(
        ("method_name", "recorded"),
        [("mount_park", "park"), ("mount_unpark", "unpark"), ("mount_abort", "abort")],
    )
    async def test_each_reaches_the_module(
        self, attached: EkosBridge, kstars: FakeKStars, method_name: str, recorded: str
    ) -> None:
        assert await getattr(attached, method_name)()
        assert mount_of(kstars).calls == [recorded]

    async def test_a_refusal_from_ekos_is_reported_as_one(
        self, attached: EkosBridge, kstars: FakeKStars
    ) -> None:
        """KStars returns false when it could not send the command on. Nothing
        above may read that as success."""
        mount_of(kstars).accepts = False
        assert await attached.mount_slew(5.5, -12.25) is False
        assert await attached.mount_park() is False

    async def test_calling_before_attaching_is_refused(self, bridge: EkosBridge) -> None:
        with pytest.raises(EkosError, match="not attached"):
            await bridge.mount_slew(5.5, -12.25)

    async def test_that_refusal_names_the_two_calls_that_fix_it(
        self, bridge: EkosBridge
    ) -> None:
        with pytest.raises(EkosError) as raised:
            await bridge.mount_park()
        assert "start_ekos" in str(raised.value)
        assert "attach_modules" in str(raised.value)

    async def test_nothing_reached_the_mount(
        self, bridge: EkosBridge, kstars: FakeKStars
    ) -> None:
        """The positive control for the two refusals above: a call that raises
        must also be a call that did not happen."""
        with pytest.raises(EkosError):
            await bridge.mount_slew(5.5, -12.25)
        assert mount_of(kstars).calls == []
        assert mount_of(kstars).slews == []


class TestTheModuleHandleDoesNotSurviveKStars:
    """A proxy to an object in a dead process is the worst kind of handle.

    It looks attached. Every call on it fails at the transport, or — worse, if
    a new KStars took the name — reaches a different object than the one the
    caller believes it is addressing.
    """

    async def test_stopping_ekos_withdraws_the_module(
        self, bridge: EkosBridge, kstars: FakeKStars
    ) -> None:
        await bridge.start_ekos()
        await bridge.attach_modules()
        await bridge.stop_ekos()
        assert not kstars.modules_exported

    async def test_a_restart_drops_the_handle(
        self, bridge: EkosBridge, bus_address: str, kstars: FakeKStars
    ) -> None:
        await bridge.start_ekos()
        await bridge.attach_modules()
        # Read into locals: EkosBridge is @final, so mypy narrows the property
        # itself on an assert and then calls the second one unreachable.
        attached_while_running = bridge.is_mount_attached
        assert attached_while_running

        await kstars.stop()
        await wait_for(lambda: bridge.state is BridgeState.RECONNECTING)
        attached_after_the_restart = bridge.is_mount_attached
        assert not attached_after_the_restart

        replacement = FakeKStars(bus_address)
        await replacement.start()
        try:
            await wait_for(lambda: bridge.state is BridgeState.CONNECTED)
            # Reattached to KStars, but Ekos has not been started in the new
            # process, so there is no module object to hold.
            assert not bridge.is_mount_attached
            with pytest.raises(EkosError, match="not attached"):
                await bridge.mount_abort()
        finally:
            await replacement.stop()

    async def test_it_can_be_attached_again_after_the_restart(
        self, bridge: EkosBridge, bus_address: str, kstars: FakeKStars
    ) -> None:
        """Positive control: the handle is dropped, not permanently lost."""
        await bridge.start_ekos()
        await bridge.attach_modules()
        await kstars.stop()
        await wait_for(lambda: bridge.state is BridgeState.RECONNECTING)

        replacement = FakeKStars(bus_address)
        await replacement.start()
        try:
            await wait_for(lambda: bridge.state is BridgeState.CONNECTED)
            await bridge.start_ekos()
            await bridge.attach_modules()
            assert bridge.is_mount_attached
            assert await bridge.mount_abort()
        finally:
            await replacement.stop()

    async def test_attaching_is_refused_while_kstars_is_away(
        self, bridge: EkosBridge, kstars: FakeKStars
    ) -> None:
        await kstars.stop()
        await wait_for(lambda: bridge.state is BridgeState.RECONNECTING)
        with pytest.raises(EkosError, match="not connected"):
            await bridge.attach_modules(timeout_s=0.1)
