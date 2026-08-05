"""Failure paths of the INDI client — ADR 0002.

Owning the protocol implementation means owning its failure behaviour. These
tests cover the cases a live night will produce and that the happy-path suite
does not: a driver dying with a transaction in flight, malformed XML arriving
mid-stream, BLOB payloads, and reads that arrive in unhelpful pieces.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator

import pytest

from nocturne.executor.indi.client import (
    IndiClient,
    IndiConnectionError,
    IndiError,
    IndiTimeoutError,
)
from nocturne.executor.indi.protocol import (
    BLOB_MODES,
    DefVector,
    IndiParser,
    IndiProtocolError,
    PropertyKind,
    SetVector,
    enable_blob,
)
from nocturne.executor.settings import IndiSettings
from tests.fixtures.fake_indi import FakeIndiServer, FakeProperty, focuser_device

DEVICE = "Focuser Simulator"
POSITION = "ABS_FOCUS_POSITION"
ELEMENT = "FOCUS_ABSOLUTE_POSITION"

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
    await connected.wait_for_property(DEVICE, POSITION)
    try:
        yield connected
    finally:
        await connected.aclose()


class TestDriverRestartMidTransaction:
    """A driver that dies while a caller is waiting on it must not hang them."""

    async def test_a_pending_wait_fails_when_the_device_vanishes(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        waiting = asyncio.ensure_future(
            client.wait_until(
                DEVICE, POSITION, lambda p: float(p[ELEMENT]) == 12345.0, timeout=30.0
            )
        )
        await asyncio.sleep(0)

        await server.kill_driver(DEVICE)

        # Fails promptly with a diagnosis, rather than sitting out the timeout.
        with pytest.raises(IndiError) as excinfo:
            await asyncio.wait_for(waiting, timeout=2.0)
        assert DEVICE in str(excinfo.value)

    async def test_a_write_in_flight_fails_when_the_device_vanishes(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        server.answer_writes = False
        writing = asyncio.ensure_future(
            client.write(DEVICE, POSITION, {ELEMENT: 999.0}, timeout=30.0)
        )
        await asyncio.sleep(0.05)

        await server.kill_driver(DEVICE)

        with pytest.raises(IndiError):
            await asyncio.wait_for(writing, timeout=2.0)

    async def test_a_wait_on_another_device_is_unaffected(self, server: FakeIndiServer) -> None:
        """Losing one driver must not fail waiters on the other four."""
        server.devices["Filter Simulator"] = focuser_device()
        client = IndiClient(FAST.model_copy(update={"port": server.port}))
        await client.connect()
        try:
            await client.wait_for_property("Filter Simulator", POSITION)
            waiting = asyncio.ensure_future(
                client.wait_until(
                    "Filter Simulator",
                    POSITION,
                    lambda p: float(p[ELEMENT]) == 7.0,
                    timeout=1.0,
                )
            )
            await asyncio.sleep(0)
            await server.kill_driver(DEVICE)
            # Still waiting on its own device, so it times out normally.
            with pytest.raises(IndiTimeoutError):
                await waiting
        finally:
            await client.aclose()

    async def test_the_device_is_usable_again_after_the_driver_returns(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        await client.connect_device(DEVICE)
        await server.kill_driver(DEVICE)
        with pytest.raises(IndiError):
            await client.write(DEVICE, POSITION, {ELEMENT: 1.0}, timeout=0.3)

        await server.restart_driver(DEVICE)
        result = await client.write(DEVICE, POSITION, {ELEMENT: 4242.0}, timeout=5.0)
        assert float(result[ELEMENT]) == pytest.approx(4242.0)


class TestMalformedInputMidStream:
    async def test_malformed_xml_drops_the_connection_and_reconnects(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        """Garbage on the wire is treated as a lost connection, not ignored."""
        assert server.connection_count == 1
        await server.broadcast(b"<defTextVector device=unquoted></defTextVector>")
        await asyncio.sleep(0.3)
        assert server.connection_count >= 2
        await client.wait_for_property(DEVICE, POSITION, timeout=2.0)

    async def test_an_update_for_an_unknown_property_is_not_invented(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        await server.broadcast(
            b'<setNumberVector device="Focuser Simulator" name="GHOST" state="Ok">'
            b'<oneNumber name="X">1</oneNumber></setNumberVector>'
        )
        await asyncio.sleep(0.2)
        assert client.get(DEVICE, "GHOST") is None

    async def test_a_truncated_message_leaves_the_client_usable(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        await server.broadcast(b'<defNumberVector device="D" name="P" state="Ok"')
        await asyncio.sleep(0.1)
        assert client.is_connected
        assert client.get(DEVICE, POSITION) is not None


class TestPartialReads:
    """The socket delivers whatever it delivers; framing must not care."""

    async def test_a_definition_split_across_two_writes(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        payload = FakeProperty(
            name="SPLIT_PROPERTY",
            kind=PropertyKind.NUMBER,
            values={"VALUE": 7.0},
        ).define(DEVICE)
        half = len(payload) // 2
        await server.broadcast(payload[:half])
        await asyncio.sleep(0.05)
        assert client.get(DEVICE, "SPLIT_PROPERTY") is None
        await server.broadcast(payload[half:])
        await client.wait_for_property(DEVICE, "SPLIT_PROPERTY", timeout=2.0)

    async def test_two_messages_in_one_write(
        self, client: IndiClient, server: FakeIndiServer
    ) -> None:
        first = FakeProperty(name="A", kind=PropertyKind.NUMBER, values={"V": 1.0})
        second = FakeProperty(name="B", kind=PropertyKind.NUMBER, values={"V": 2.0})
        await server.broadcast(first.define(DEVICE) + second.define(DEVICE))
        await client.wait_for_property(DEVICE, "A", timeout=2.0)
        await client.wait_for_property(DEVICE, "B", timeout=2.0)


class TestBlobHandling:
    """SPEC section 11.3: FITS is read from disk, never pulled over the wire."""

    def test_the_client_never_asks_for_blobs(self) -> None:
        """enableBLOB is not sent, so indiserver defaults to Never."""
        import inspect

        from nocturne.executor.indi import client as client_module

        assert "enable_blob" not in inspect.getsource(client_module)

    def test_enable_blob_modes_are_the_three_indi_defines(self) -> None:
        assert {"Never", "Also", "Only"} == BLOB_MODES
        assert enable_blob("Never") == b"<enableBLOB>Never</enableBLOB>"

    def test_a_blob_definition_carries_no_payload(self) -> None:
        message = next(
            IndiParser().feed(
                b'<defBLOBVector device="CCD Simulator" name="CCD1" state="Idle" perm="ro">'
                b'<defBLOB name="CCD1" label="Image"/></defBLOBVector>'
            )
        )
        assert isinstance(message, DefVector)
        assert message.property.kind is PropertyKind.BLOB
        assert message.property["CCD1"] == b""

    def test_a_blob_payload_is_base64_decoded(self) -> None:
        payload = b"SIMPLE  =                    T"
        encoded = base64.b64encode(payload).decode("ascii")
        message = next(
            IndiParser().feed(
                f'<setBLOBVector device="CCD Simulator" name="CCD1" state="Ok">'
                f'<oneBLOB name="CCD1" size="{len(payload)}" format=".fits">'
                f"{encoded}</oneBLOB></setBLOBVector>".encode()
            )
        )
        assert isinstance(message, SetVector)
        assert message.values["CCD1"] == payload

    def test_a_blob_split_across_chunks_is_reassembled(self) -> None:
        payload = bytes(range(256)) * 40
        encoded = base64.b64encode(payload).decode("ascii")
        raw = (
            f'<setBLOBVector device="CCD Simulator" name="CCD1" state="Ok">'
            f'<oneBLOB name="CCD1" size="{len(payload)}" format=".fits">'
            f"{encoded}</oneBLOB></setBLOBVector>"
        ).encode()
        parser = IndiParser()
        messages = [m for i in range(0, len(raw), 997) for m in parser.feed(raw[i : i + 997])]
        assert len(messages) == 1
        assert isinstance(messages[0], SetVector)
        assert messages[0].values["CCD1"] == payload

    def test_invalid_base64_in_a_blob_is_a_protocol_error(self) -> None:
        with pytest.raises(IndiProtocolError, match="base64"):
            list(
                IndiParser().feed(
                    b'<setBLOBVector device="D" name="B" state="Ok">'
                    b'<oneBLOB name="B" size="4" format=".fits">not!valid!</oneBLOB>'
                    b"</setBLOBVector>"
                )
            )

    def test_an_oversized_blob_is_refused_rather_than_buffered(self) -> None:
        """A camera streaming into a client that never asked must not fill RAM."""
        parser = IndiParser(max_message_bytes=4096)
        with pytest.raises(IndiProtocolError, match="lost framing"):
            list(parser.feed(b'<setBLOBVector device="D" name="B">' + b"A" * 8192))


class TestClosedClient:
    async def test_operations_after_close_are_refused(self, server: FakeIndiServer) -> None:
        client = IndiClient(FAST.model_copy(update={"port": server.port}))
        await client.connect()
        await client.wait_for_property(DEVICE, POSITION)
        await client.aclose()
        with pytest.raises(IndiConnectionError):
            await client.write(DEVICE, POSITION, {ELEMENT: 1.0}, timeout=0.5)
