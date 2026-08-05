"""INDI client behaviour — SPEC section 14, M1 AUTO criteria.

Connect, read and write properties, survive a driver restart, reconnect
automatically. These run against an in-process fake server so that failures can
be induced exactly when the test wants them; the same behaviours are verified
against the real simulator drivers in ``backend/tests/integration``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from nocturne.executor.indi.client import (
    ConnectionState,
    DeviceAppeared,
    DeviceVanished,
    IndiClient,
    IndiConnectionError,
    IndiError,
    IndiEvent,
    IndiTimeoutError,
    ServerDisconnected,
)
from nocturne.executor.indi.protocol import PropertyKind
from nocturne.executor.settings import IndiSettings
from tests.fixtures.fake_indi import FakeIndiServer, FakeProperty, focuser_device

DEVICE = "Focuser Simulator"
POSITION = "ABS_FOCUS_POSITION"
ELEMENT = "FOCUS_ABSOLUTE_POSITION"

#: Impatient settings: these tests must not spend real seconds waiting.
FAST = IndiSettings(
    connect_timeout_s=2.0,
    property_timeout_s=2.0,
    write_timeout_s=2.0,
    device_connect_timeout_s=2.0,
    reconnect_initial_delay_s=0.01,
    reconnect_max_delay_s=0.05,
)


@pytest.fixture
async def server() -> AsyncIterator[FakeIndiServer]:
    fake = FakeIndiServer()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def client(server: FakeIndiServer) -> AsyncIterator[IndiClient]:
    connected = IndiClient(FAST.model_copy(update={"port": server.port}))
    await connected.connect()
    try:
        yield connected
    finally:
        await connected.aclose()


async def wait_for(condition: object, timeout: float = 2.0) -> None:
    """Poll ``condition`` until it is true, or fail the test."""
    assert callable(condition)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not met in time")


class TestConnection:
    async def test_connects_and_reports_state(self, client: IndiClient) -> None:
        assert client.is_connected
        assert client.state is ConnectionState.CONNECTED

    async def test_receives_the_property_definitions(self, client: IndiClient) -> None:
        prop = await client.wait_for_property(DEVICE, POSITION)
        assert prop[ELEMENT] == pytest.approx(50000.0)
        assert client.devices() == (DEVICE,)

    async def test_refusing_connection_raises(self, server: FakeIndiServer) -> None:
        await server.stop()
        client = IndiClient(FAST.model_copy(update={"port": server.port}))
        with pytest.raises(IndiConnectionError, match="cannot reach indiserver"):
            await client.connect()
        await client.aclose()

    async def test_close_is_idempotent(self, server: FakeIndiServer) -> None:
        client = IndiClient(FAST.model_copy(update={"port": server.port}))
        await client.connect()
        await client.aclose()
        await client.aclose()
        assert client.state is ConnectionState.CLOSED

    async def test_a_closed_client_does_not_reconnect(self, server: FakeIndiServer) -> None:
        client = IndiClient(FAST.model_copy(update={"port": server.port}))
        await client.connect()
        await client.aclose()
        with pytest.raises(IndiConnectionError, match="closed"):
            await client.connect()

    async def test_context_manager_closes(self, server: FakeIndiServer) -> None:
        async with IndiClient(FAST.model_copy(update={"port": server.port})) as client:
            assert client.is_connected
        assert client.state is ConnectionState.CLOSED


class TestReadProperties:
    async def test_property_cache_is_populated(self, client: IndiClient) -> None:
        await client.wait_for_property(DEVICE, POSITION)
        assert set(client.properties(DEVICE)) == {
            (DEVICE, "CONNECTION"),
            (DEVICE, POSITION),
            (DEVICE, "DRIVER_INFO"),
        }

    async def test_get_returns_none_for_an_unknown_property(self, client: IndiClient) -> None:
        assert client.get(DEVICE, "NO_SUCH_PROPERTY") is None

    async def test_waiting_for_a_property_that_never_arrives_times_out(
        self, client: IndiClient
    ) -> None:
        with pytest.raises(IndiTimeoutError, match="NEVER"):
            await client.wait_for_property(DEVICE, "NEVER", timeout=0.05)

    async def test_wait_until_returns_immediately_when_already_satisfied(
        self, client: IndiClient
    ) -> None:
        await client.wait_for_property(DEVICE, POSITION)
        prop = await client.wait_until(
            DEVICE, POSITION, lambda p: p[ELEMENT] == pytest.approx(50000.0), timeout=0.05
        )
        assert prop[ELEMENT] == pytest.approx(50000.0)


class TestWriteProperties:
    async def test_writing_a_number_changes_the_value(self, client: IndiClient) -> None:
        prop = await client.write(DEVICE, POSITION, {ELEMENT: 42000.0})
        assert prop[ELEMENT] == pytest.approx(42000.0)
        assert client.get(DEVICE, POSITION) is not None

    async def test_writing_a_switch_connects_the_device(self, client: IndiClient) -> None:
        assert not client.is_device_connected(DEVICE)
        await client.connect_device(DEVICE)
        assert client.is_device_connected(DEVICE)

    async def test_disconnecting_a_device(self, client: IndiClient) -> None:
        await client.connect_device(DEVICE)
        await client.disconnect_device(DEVICE)
        assert not client.is_device_connected(DEVICE)

    async def test_writing_a_read_only_property_is_refused(self, client: IndiClient) -> None:
        await client.wait_for_property(DEVICE, "DRIVER_INFO")
        with pytest.raises(IndiError, match="cannot be written"):
            await client.write(DEVICE, "DRIVER_INFO", {"DRIVER_EXEC": "nope"})

    async def test_writing_an_unknown_element_is_refused(self, client: IndiClient) -> None:
        with pytest.raises(IndiError, match="NOT_AN_ELEMENT"):
            await client.write(DEVICE, POSITION, {"NOT_AN_ELEMENT": 1.0})

    async def test_write_waits_for_the_driver_to_answer(self, client: IndiClient) -> None:
        """A write that returns before the driver answered is a lie to the caller."""
        await client.write(DEVICE, POSITION, {ELEMENT: 12345.0})
        cached = client.get(DEVICE, POSITION)
        assert cached is not None
        assert cached[ELEMENT] == pytest.approx(12345.0)

    async def test_write_without_settle_does_not_wait(self, client: IndiClient) -> None:
        await client.wait_for_property(DEVICE, POSITION)
        await client.write(DEVICE, POSITION, {ELEMENT: 999.0}, await_settle=False)
        await wait_for(
            lambda: (
                (p := client.get(DEVICE, POSITION)) is not None
                and p[ELEMENT] == pytest.approx(999.0)
            )
        )


class TestDriverRestart:
    """SPEC section 14, M1 AUTO: survives driver restart, reconnects automatically."""

    async def test_device_vanishing_is_noticed(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        await client.connect_device(DEVICE)
        events: list[IndiEvent] = []
        client.subscribe(events.append)

        await server.kill_driver(DEVICE)
        await wait_for(lambda: any(isinstance(e, DeviceVanished) for e in events))
        assert client.get(DEVICE, POSITION) is None
        assert DEVICE not in client.devices()

    async def test_device_is_reconnected_after_its_driver_restarts(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        await client.connect_device(DEVICE)
        assert client.is_device_connected(DEVICE)

        await server.kill_driver(DEVICE)
        await wait_for(lambda: DEVICE not in client.devices())

        await server.restart_driver(DEVICE)
        await wait_for(lambda: client.is_device_connected(DEVICE), timeout=3.0)

    async def test_a_device_that_was_not_connected_is_not_connected_on_restart(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        """Recovery restores what was, it does not decide to connect things."""
        await client.wait_for_property(DEVICE, POSITION)
        await server.kill_driver(DEVICE)
        await wait_for(lambda: DEVICE not in client.devices())

        await server.restart_driver(DEVICE)
        await wait_for(lambda: client.get(DEVICE, POSITION) is not None)
        await asyncio.sleep(0.1)
        assert not client.is_device_connected(DEVICE)

    async def test_device_appeared_is_emitted_on_restart(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        await client.wait_for_property(DEVICE, POSITION)
        events: list[IndiEvent] = []
        client.subscribe(events.append)

        await server.kill_driver(DEVICE)
        await wait_for(lambda: DEVICE not in client.devices())
        await server.restart_driver(DEVICE)
        await wait_for(lambda: any(isinstance(e, DeviceAppeared) for e in events))

    async def test_deleting_one_property_does_not_remove_the_device(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        await client.wait_for_property(DEVICE, POSITION)
        await server.broadcast(f'<delProperty device="{DEVICE}" name="{POSITION}"/>'.encode())
        await wait_for(lambda: client.get(DEVICE, POSITION) is None)
        assert DEVICE in client.devices()


class TestServerReconnection:
    """SPEC section 14, M1 AUTO: reconnects automatically."""

    async def test_reconnects_after_the_server_drops_the_connection(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        await client.wait_for_property(DEVICE, POSITION)
        assert server.connection_count == 1

        await server.drop_clients()
        await wait_for(lambda: server.connection_count == 2, timeout=3.0)
        await wait_for(lambda: client.is_connected, timeout=3.0)
        await client.wait_for_property(DEVICE, POSITION, timeout=2.0)

    async def test_disconnection_is_announced_to_subscribers(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        events: list[IndiEvent] = []
        client.subscribe(events.append)
        await server.drop_clients()
        await wait_for(lambda: any(isinstance(e, ServerDisconnected) for e in events))

    async def test_connected_devices_are_reconnected_after_a_server_drop(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        await client.connect_device(DEVICE)
        # The fake server keeps its state, so a naive client could be fooled by a
        # stale cache; the client clears everything on a drop and rebuilds it.
        server.devices[DEVICE]["CONNECTION"].values = {
            "CONNECT": False,
            "DISCONNECT": True,
        }

        await server.drop_clients()
        await wait_for(lambda: client.is_device_connected(DEVICE), timeout=3.0)

    async def test_cache_is_cleared_while_reconnecting(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        """A cache that survives a drop would report a device that is not there."""
        await client.wait_for_property(DEVICE, POSITION)
        seen: list[int] = []
        client.subscribe(lambda _: seen.append(len(client.properties())))
        await server.drop_clients()
        await wait_for(lambda: 0 in seen, timeout=3.0)

    async def test_gives_up_after_the_configured_number_of_attempts(
        self, server: FakeIndiServer
    ) -> None:
        settings = FAST.model_copy(update={"port": server.port, "reconnect_max_attempts": 2})
        client = IndiClient(settings)
        await client.connect()
        await client.wait_for_property(DEVICE, POSITION)

        await server.stop()
        await wait_for(lambda: client.state is ConnectionState.DISCONNECTED, timeout=3.0)
        await client.aclose()

    async def test_a_pending_waiter_fails_when_the_client_gives_up(
        self, server: FakeIndiServer
    ) -> None:
        settings = FAST.model_copy(update={"port": server.port, "reconnect_max_attempts": 1})
        client = IndiClient(settings)
        await client.connect()
        await client.wait_for_property(DEVICE, POSITION)

        waiting = asyncio.ensure_future(
            client.wait_for_property(DEVICE, "NEVER_ARRIVES", timeout=5.0)
        )
        await asyncio.sleep(0)
        await server.stop()
        with pytest.raises(IndiConnectionError):
            await waiting
        await client.aclose()


class TestEvents:
    async def test_a_failing_subscriber_does_not_break_the_client(
        self, client: IndiClient
    ) -> None:
        def explode(_: IndiEvent) -> None:
            raise RuntimeError("subscriber is broken")

        client.subscribe(explode)
        await client.write(DEVICE, POSITION, {ELEMENT: 1234.0})
        assert client.is_connected

    async def test_unsubscribe_stops_delivery(self, client: IndiClient) -> None:
        events: list[IndiEvent] = []
        unsubscribe = client.subscribe(events.append)
        await client.write(DEVICE, POSITION, {ELEMENT: 1.0})
        count = len(events)
        unsubscribe()
        await client.write(DEVICE, POSITION, {ELEMENT: 2.0})
        assert len(events) == count


class TestMultipleDevices:
    async def test_every_device_class_is_tracked_independently(self) -> None:
        devices = {
            name: focuser_device()
            for name in ("Telescope Simulator", "CCD Simulator", "Filter Simulator")
        }
        devices["CCD Simulator"]["CCD_EXPOSURE"] = FakeProperty(
            name="CCD_EXPOSURE",
            kind=PropertyKind.NUMBER,
            values={"CCD_EXPOSURE_VALUE": 1.0},
        )
        server = FakeIndiServer(devices)
        await server.start()
        client = IndiClient(FAST.model_copy(update={"port": server.port}))
        try:
            await client.connect()
            await client.wait_for_property("CCD Simulator", "CCD_EXPOSURE")
            assert client.devices() == (
                "CCD Simulator",
                "Filter Simulator",
                "Telescope Simulator",
            )
        finally:
            await client.aclose()
            await server.stop()
