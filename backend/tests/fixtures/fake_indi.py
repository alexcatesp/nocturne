"""A minimal in-process INDI server, for deterministic client tests.

The real simulator drivers are exercised in ``backend/tests/integration``. This
fake exists for the cases the real thing cannot be made to perform on demand:
dropping the socket at a chosen moment, killing a driver, bringing it back.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Self
from xml.sax.saxutils import escape, quoteattr

from nocturne.executor.indi.protocol import IndiParser, PropertyKind


@dataclass
class FakeProperty:
    """One property vector the fake server offers."""

    name: str
    kind: PropertyKind
    values: dict[str, float | str | bool]
    permission: str = "rw"
    state: str = "Idle"
    rule: str | None = None
    labels: dict[str, str] = field(default_factory=dict)

    def _render_value(self, value: float | str | bool) -> str:
        if self.kind is PropertyKind.SWITCH:
            return "On" if value else "Off"
        return escape(str(value))

    def define(self, device: str) -> bytes:
        one = f"def{self.kind.value}"
        attributes = (
            f"device={quoteattr(device)} name={quoteattr(self.name)} "
            f'label={quoteattr(self.name)} group="Main Control" '
            f'state={quoteattr(self.state)} perm={quoteattr(self.permission)} timeout="60"'
        )
        if self.rule is not None:
            attributes += f" rule={quoteattr(self.rule)}"
        body = "".join(
            f"<{one} name={quoteattr(element)} "
            f"label={quoteattr(self.labels.get(element, element))}>"
            f"{self._render_value(value)}</{one}>"
            for element, value in self.values.items()
        )
        tag = f"def{self.kind.value}Vector"
        return f"<{tag} {attributes}>{body}</{tag}>".encode()

    def update(self, device: str, state: str) -> bytes:
        one = f"one{self.kind.value}"
        body = "".join(
            f"<{one} name={quoteattr(element)}>{self._render_value(value)}</{one}>"
            for element, value in self.values.items()
        )
        return (
            f"<set{self.kind.value}Vector device={quoteattr(device)} "
            f"name={quoteattr(self.name)} state={quoteattr(state)}>"
            f"{body}</set{self.kind.value}Vector>"
        ).encode()


def connection_property() -> FakeProperty:
    """The standard CONNECTION switch, disconnected."""
    return FakeProperty(
        name="CONNECTION",
        kind=PropertyKind.SWITCH,
        values={"CONNECT": False, "DISCONNECT": True},
        rule="OneOfMany",
    )


def focuser_device() -> dict[str, FakeProperty]:
    """A device that looks enough like indi_simulator_focus to test against."""
    return {
        "CONNECTION": connection_property(),
        "ABS_FOCUS_POSITION": FakeProperty(
            name="ABS_FOCUS_POSITION",
            kind=PropertyKind.NUMBER,
            values={"FOCUS_ABSOLUTE_POSITION": 50000.0},
        ),
        "DRIVER_INFO": FakeProperty(
            name="DRIVER_INFO",
            kind=PropertyKind.TEXT,
            values={"DRIVER_EXEC": "fake_focus"},
            permission="ro",
        ),
    }


class FakeIndiServer:
    """An INDI server that answers getProperties and accepts writes."""

    def __init__(self, devices: dict[str, dict[str, FakeProperty]] | None = None) -> None:
        self.devices = (
            devices if devices is not None else {"Focuser Simulator": focuser_device()}
        )
        #: Set False to leave a write unanswered, so a test can catch one in flight.
        self.answer_writes = True
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self.port = 0
        #: Number of client connections accepted, so a test can assert a reconnect.
        self.connection_count = 0

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> int:
        """Listen on an ephemeral port and return it."""
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        """Close every client and stop listening."""
        await self.drop_clients()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def drop_clients(self) -> None:
        """Close every client connection without warning."""
        writers, self._writers = self._writers, []
        for writer in writers:
            writer.close()
        for writer in writers:
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    async def broadcast(self, payload: bytes) -> None:
        """Send ``payload`` to every connected client."""
        for writer in list(self._writers):
            try:
                writer.write(payload)
                await writer.drain()
            except (OSError, ConnectionError):
                pass

    async def kill_driver(self, device: str) -> None:
        """Announce that ``device`` has gone away, as indiserver does."""
        self.devices.pop(device, None)
        await self.broadcast(f"<delProperty device={quoteattr(device)}/>".encode())

    async def restart_driver(
        self, device: str, properties: dict[str, FakeProperty] | None = None
    ) -> None:
        """Bring ``device`` back with fresh, disconnected properties."""
        self.devices[device] = properties if properties is not None else focuser_device()
        for prop in self.devices[device].values():
            await self.broadcast(prop.define(device))

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        self.connection_count += 1
        parser = IndiParser()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                for fragment in parser._framer.feed(data):
                    await self._handle(fragment, writer)
        except (OSError, ConnectionError, asyncio.CancelledError):
            return
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            writer.close()

    async def _handle(self, fragment: bytes, writer: asyncio.StreamWriter) -> None:
        from xml.etree import ElementTree

        node = ElementTree.fromstring(fragment)  # noqa: S314
        if node.tag == "getProperties":
            wanted = node.get("device")
            for device, properties in self.devices.items():
                if wanted is not None and device != wanted:
                    continue
                for prop in properties.values():
                    writer.write(prop.define(device))
            await writer.drain()
            return

        if not node.tag.startswith("new"):
            return

        device = node.get("device") or ""
        name = node.get("name") or ""
        device_properties = self.devices.get(device)
        if device_properties is None or name not in device_properties:
            return
        prop = device_properties[name]
        for child in node:
            element = child.get("name") or ""
            if element not in prop.values:
                continue
            raw = (child.text or "").strip()
            if prop.kind is PropertyKind.SWITCH:
                prop.values[element] = raw == "On"
            elif prop.kind is PropertyKind.NUMBER:
                prop.values[element] = float(raw)
            else:
                prop.values[element] = raw
        if not self.answer_writes:
            return
        prop.state = "Ok"
        writer.write(prop.update(device, "Ok"))
        await writer.drain()
