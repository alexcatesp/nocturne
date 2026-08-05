"""Ekos DBus bridge — SPEC sections 3 and 4.

Connect, disconnect, drive Ekos, and recover when KStars leaves the bus and
comes back. Run against a stub service on a private session bus; see
``tests/fixtures/fake_kstars`` for why, and the M1 HITL procedure for how the
real KStars surface is confirmed.
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
from tests.fixtures.fake_kstars import FakeKStars, dbus_available, session_bus

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


@pytest.fixture
async def bridge(bus_address: str, kstars: FakeKStars) -> AsyncIterator[EkosBridge]:
    connected = EkosBridge(
        bus_type=BusType.SESSION, bus_address=bus_address, reconnect_delay_s=0.05
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

    async def test_connect_one_device(self, bridge: EkosBridge, kstars: FakeKStars) -> None:
        await bridge.connect_device("Focuser Simulator")
        assert kstars.indi.connected == {"Focuser Simulator"}
        await bridge.disconnect_device("Focuser Simulator")
        assert kstars.indi.connected == set()

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
