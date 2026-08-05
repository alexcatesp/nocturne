"""Async INDI client — SPEC section 3 (layer 0) and section 14, M1.

Connects to indiserver, keeps a cache of every property every driver has
defined, writes properties, and — the part that matters at three in the
morning — notices when a driver or the server has gone away and puts it all
back together without help.

Two failure modes are handled distinctly:

**The server went away.** The socket drops. The client reconnects with
exponential backoff, re-issues ``getProperties``, and reconnects the devices
that were connected before.

**A driver restarted.** indiserver sends ``delProperty`` for the whole device
and, when it respawns the driver, the property definitions arrive again. The
socket never drops. The client notices the device vanish, notices it come back,
and reconnects it if it had been connected.

The client owns no policy: it does not decide whether a slew is safe. That is
the governor's job, and the executor facade is what enforces the order.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Final, final

from nocturne.executor.indi.protocol import (
    DefVector,
    DelProperty,
    ElementValue,
    IndiParser,
    IndiProtocolError,
    Message,
    Property,
    PropertyKind,
    PropertyState,
    SetVector,
    get_properties,
    new_vector,
)
from nocturne.executor.settings import IndiSettings

logger = logging.getLogger("nocturne.executor.indi")

#: The standard INDI property and elements through which a device is connected.
CONNECTION_PROPERTY: Final = "CONNECTION"
CONNECT_ELEMENT: Final = "CONNECT"
DISCONNECT_ELEMENT: Final = "DISCONNECT"


class IndiError(Exception):
    """Base class for INDI client failures."""


class IndiConnectionError(IndiError):
    """The client could not reach, or could not stay connected to, indiserver."""


class IndiTimeoutError(IndiError):
    """A property did not arrive, or did not settle, within its timeout."""


class IndiDeviceLostError(IndiError):
    """The device went away while a caller was waiting on it.

    Raised in place of letting the caller sit out a timeout that cannot now be
    met: the driver has gone, and the session layer should hear about it at
    once rather than in forty-five seconds.
    """


class ConnectionState(StrEnum):
    """Where the client is with respect to indiserver."""

    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ServerConnected:
    """The client attached to indiserver."""


@dataclass(frozen=True, slots=True)
class ServerDisconnected:
    """The connection to indiserver dropped."""

    reason: str


@dataclass(frozen=True, slots=True)
class DeviceAppeared:
    """A device started defining properties. Also fires after a driver restart."""

    device: str


@dataclass(frozen=True, slots=True)
class DeviceVanished:
    """A device's properties were all deleted — usually its driver died."""

    device: str


@dataclass(frozen=True, slots=True)
class PropertyChanged:
    """A property was defined or updated."""

    property: Property


@dataclass(frozen=True, slots=True)
class DriverMessage:
    """Free text from a driver."""

    text: str
    device: str | None


IndiEvent = (
    ServerConnected
    | ServerDisconnected
    | DeviceAppeared
    | DeviceVanished
    | PropertyChanged
    | DriverMessage
)

EventHandler = Callable[[IndiEvent], None]
Predicate = Callable[[Property], bool]


@final
class IndiClient:
    """An async client for one indiserver."""

    def __init__(self, settings: IndiSettings | None = None) -> None:
        self._settings = settings or IndiSettings()
        self._state = ConnectionState.DISCONNECTED
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._parser = IndiParser(self._settings.max_message_bytes)
        self._properties: dict[tuple[str, str], Property] = {}
        #: Bumped on every update to a property, so that a caller can tell
        #: "the driver answered" from "the value happened to be right already".
        self._revisions: dict[tuple[str, str], int] = {}
        self._connected_devices: set[str] = set()
        self._reader_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()
        self._waiters: list[tuple[Predicate, tuple[str, str], asyncio.Future[Property]]] = []
        self._handlers: list[EventHandler] = []
        # An Event rather than a bool: it is set from aclose() while the reader
        # task is suspended on a read, and a plain flag reads as constant to a
        # type checker that cannot see the concurrency.
        self._shutdown = asyncio.Event()

    # ------------------------------------------------------------------ state

    @property
    def settings(self) -> IndiSettings:
        """The transport settings in force."""
        return self._settings

    @property
    def state(self) -> ConnectionState:
        """Connection state with respect to indiserver."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Whether the client currently has a live connection."""
        return self._state is ConnectionState.CONNECTED

    def devices(self) -> tuple[str, ...]:
        """Names of every device that has defined at least one property."""
        return tuple(sorted({device for device, _ in self._properties}))

    def properties(self, device: str | None = None) -> Mapping[tuple[str, str], Property]:
        """The property cache, optionally narrowed to one device."""
        if device is None:
            return dict(self._properties)
        return {key: prop for key, prop in self._properties.items() if key[0] == device}

    def get(self, device: str, name: str) -> Property | None:
        """A property from the cache, or ``None`` if it has not been defined."""
        return self._properties.get((device, name))

    def is_device_connected(self, device: str) -> bool:
        """Whether ``device`` reports CONNECTION.CONNECT as On."""
        connection = self.get(device, CONNECTION_PROPERTY)
        return bool(connection and connection.get(CONNECT_ELEMENT) is True)

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Register ``handler`` for client events. Returns an unsubscribe callable."""
        self._handlers.append(handler)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._handlers.remove(handler)

        return unsubscribe

    # ------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> IndiClient:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def connect(self) -> None:
        """Open the connection and start reading.

        Raises:
            IndiConnectionError: if the connection cannot be opened.
        """
        if self._state is ConnectionState.CLOSED:
            raise IndiConnectionError("this client has been closed and cannot reconnect")
        if self._reader_task is not None:
            return
        await self._open()
        self._reader_task = asyncio.create_task(self._run(), name="indi-reader")

    async def aclose(self) -> None:
        """Close the connection and stop every background task."""
        if self._state is ConnectionState.CLOSED:
            return
        self._shutdown.set()
        self._state = ConnectionState.CLOSED

        for task in (self._reader_task, *self._background):
            if task is not None:
                task.cancel()
        pending = [task for task in (self._reader_task, *self._background) if task]
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._reader_task = None
        self._background.clear()

        await self._close_socket()
        self._fail_waiters(IndiConnectionError("client closed"))

    async def _open(self) -> None:
        settings = self._settings
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(settings.host, settings.port),
                timeout=settings.connect_timeout_s,
            )
        except (OSError, TimeoutError) as exc:
            raise IndiConnectionError(
                f"cannot reach indiserver at {settings.host}:{settings.port}: {exc}"
            ) from exc

        self._parser = IndiParser(settings.max_message_bytes)
        self._state = ConnectionState.CONNECTED
        logger.info(
            "connected to indiserver",
            extra={"host": settings.host, "port": settings.port},
        )
        await self._send(get_properties())
        self._emit(ServerConnected())

    async def _close_socket(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(OSError, asyncio.CancelledError):
            await writer.wait_closed()

    async def _send(self, payload: bytes) -> None:
        if self._writer is None:
            raise IndiConnectionError("not connected to indiserver")
        self._writer.write(payload)
        await self._writer.drain()

    # ---------------------------------------------------------------- reading

    async def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                await self._read_forever()
            except asyncio.CancelledError:
                raise
            except (OSError, IndiProtocolError, IndiConnectionError) as exc:
                if self._shutdown.is_set():
                    return
                if not await self._reconnect(str(exc)):
                    return

    async def _read_forever(self) -> None:
        reader = self._reader
        if reader is None:
            raise IndiConnectionError("not connected to indiserver")
        while True:
            data = await reader.read(self._settings.read_chunk_bytes)
            if not data:
                raise IndiConnectionError("indiserver closed the connection")
            for message in self._parser.feed(data):
                self._handle(message)

    async def _reconnect(self, reason: str) -> bool:
        """Rebuild the connection after ``reason``. Returns False if it gave up."""
        remembered = frozenset(self._connected_devices)
        self._forget_everything()
        self._state = ConnectionState.RECONNECTING
        logger.warning(
            "lost the connection to indiserver, reconnecting",
            extra={"reason": reason, "devices_to_restore": sorted(remembered)},
        )
        self._emit(ServerDisconnected(reason=reason))
        await self._close_socket()

        settings = self._settings
        delay = settings.reconnect_initial_delay_s
        attempt = 0
        while not self._shutdown.is_set():
            attempt += 1
            await asyncio.sleep(delay)
            if self._shutdown.is_set():
                return False
            try:
                await self._open()
            except IndiConnectionError as exc:
                if (
                    settings.reconnect_max_attempts is not None
                    and attempt >= settings.reconnect_max_attempts
                ):
                    self._state = ConnectionState.DISCONNECTED
                    logger.error(
                        "gave up reconnecting to indiserver",
                        extra={"attempts": attempt, "reason": str(exc)},
                    )
                    self._fail_waiters(
                        IndiConnectionError(f"gave up reconnecting after {attempt} attempts")
                    )
                    return False
                delay = min(
                    delay * settings.reconnect_backoff_factor, settings.reconnect_max_delay_s
                )
                continue
            logger.info("reconnected to indiserver", extra={"attempts": attempt})
            if remembered:
                self._spawn(self._restore_devices(remembered), "indi-restore")
            return True
        return False

    def _forget_everything(self) -> None:
        self._properties.clear()
        self._revisions.clear()
        self._connected_devices.clear()
        self._parser = IndiParser(self._settings.max_message_bytes)

    # --------------------------------------------------------------- messages

    def _handle(self, message: DefVector | SetVector | DelProperty | Message) -> None:
        match message:
            case DefVector(property=prop):
                is_new_device = prop.device not in self.devices()
                self._store(prop)
                if is_new_device:
                    logger.info("device appeared", extra={"device": prop.device})
                    self._emit(DeviceAppeared(device=prop.device))
            case SetVector():
                existing = self._properties.get((message.device, message.name))
                if existing is None:
                    # An update for a property we were never told about. Ask for
                    # the definition rather than guessing its shape.
                    self._spawn(self._request(message.device, message.name), "indi-getprops")
                    return
                self._store(existing.apply(message))
            case DelProperty():
                self._delete(message)
            case Message():
                logger.debug(
                    "driver message", extra={"device": message.device, "text": message.text}
                )
                self._emit(DriverMessage(text=message.text, device=message.device))

    def _store(self, prop: Property) -> None:
        key = (prop.device, prop.name)
        self._properties[key] = prop
        self._revisions[key] = self._revisions.get(key, 0) + 1
        if prop.name == CONNECTION_PROPERTY:
            self._track_connection(prop)
        self._emit(PropertyChanged(property=prop))
        self._wake_waiters(key, prop)

    def _track_connection(self, prop: Property) -> None:
        if prop.get(CONNECT_ELEMENT) is True:
            self._connected_devices.add(prop.device)
        else:
            self._connected_devices.discard(prop.device)

    def _delete(self, message: DelProperty) -> None:
        if message.is_whole_device:
            removed = [key for key in self._properties if key[0] == message.device]
            was_connected = message.device in self._connected_devices
            for key in removed:
                del self._properties[key]
                self._revisions.pop(key, None)
            self._connected_devices.discard(message.device)
            if removed:
                self._fail_waiters_for_device(
                    message.device,
                    IndiDeviceLostError(
                        f"{message.device} went away while waiting on it; its driver "
                        "has died. It will be reconnected if indiserver respawns it."
                    ),
                )
                logger.warning(
                    "device vanished; its driver has gone away",
                    extra={"device": message.device, "was_connected": was_connected},
                )
                self._emit(DeviceVanished(device=message.device))
                if was_connected:
                    self._spawn(self._await_restart(message.device), "indi-driver-restart")
            return
        key = (message.device, message.name or "")
        self._properties.pop(key, None)
        self._revisions.pop(key, None)

    async def _request(self, device: str, name: str) -> None:
        with contextlib.suppress(IndiConnectionError):
            await self._send(get_properties(device=device, name=name))

    # ------------------------------------------------------------- recovery

    async def _await_restart(self, device: str) -> None:
        """Reconnect ``device`` once indiserver has respawned its driver."""
        try:
            await self.wait_for_property(
                device,
                CONNECTION_PROPERTY,
                timeout=self._settings.device_connect_timeout_s,
            )
        except (IndiTimeoutError, IndiConnectionError) as exc:
            logger.error(
                "device did not come back after its driver died",
                extra={"device": device, "reason": str(exc)},
            )
            return
        logger.info("device came back; reconnecting it", extra={"device": device})
        try:
            await self.connect_device(device)
        except IndiError as exc:
            logger.error(
                "could not reconnect device after its driver restarted",
                extra={"device": device, "reason": str(exc)},
            )

    async def _restore_devices(self, devices: frozenset[str]) -> None:
        """Reconnect the devices that were connected before the server dropped."""
        for device in sorted(devices):
            try:
                await self.wait_for_property(
                    device,
                    CONNECTION_PROPERTY,
                    timeout=self._settings.device_connect_timeout_s,
                )
                await self.connect_device(device)
            except IndiError as exc:
                logger.error(
                    "could not restore device after reconnecting",
                    extra={"device": device, "reason": str(exc)},
                )

    def _spawn(self, coroutine: object, name: str) -> None:
        if self._shutdown.is_set():
            return
        # The coroutine type is erased here only to keep the call sites terse.
        task: asyncio.Task[None] = asyncio.create_task(coroutine, name=name)  # type: ignore[arg-type]
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --------------------------------------------------------------- waiting

    def _wake_waiters(self, key: tuple[str, str], prop: Property) -> None:
        if not self._waiters:
            return
        remaining: list[tuple[Predicate, tuple[str, str], asyncio.Future[Property]]] = []
        for predicate, waited_key, future in self._waiters:
            if future.done():
                continue
            if waited_key == key and predicate(prop):
                future.set_result(prop)
            else:
                remaining.append((predicate, waited_key, future))
        self._waiters = remaining

    def _fail_waiters_for_device(self, device: str, error: Exception) -> None:
        """Fail only the waiters watching ``device``; leave the others alone."""
        remaining: list[tuple[Predicate, tuple[str, str], asyncio.Future[Property]]] = []
        for entry in self._waiters:
            _, (waited_device, _), future = entry
            if waited_device == device and not future.done():
                future.set_exception(error)
            else:
                remaining.append(entry)
        self._waiters = remaining

    def _fail_waiters(self, error: Exception) -> None:
        waiters, self._waiters = self._waiters, []
        for _, _, future in waiters:
            if not future.done():
                future.set_exception(error)

    async def wait_until(
        self,
        device: str,
        name: str,
        predicate: Predicate,
        *,
        timeout: float | None = None,
    ) -> Property:
        """Wait until ``device``'s ``name`` property satisfies ``predicate``.

        Returns immediately if the cached property already satisfies it.

        Raises:
            IndiTimeoutError: if it does not within ``timeout`` seconds.
        """
        key = (device, name)
        cached = self._properties.get(key)
        if cached is not None and predicate(cached):
            return cached

        future: asyncio.Future[Property] = asyncio.get_running_loop().create_future()
        entry = (predicate, key, future)
        self._waiters.append(entry)
        limit = self._settings.property_timeout_s if timeout is None else timeout
        try:
            return await asyncio.wait_for(future, timeout=limit)
        except TimeoutError as exc:
            raise IndiTimeoutError(
                f"{device}.{name} did not reach the expected state within {limit} s"
            ) from exc
        finally:
            with contextlib.suppress(ValueError):
                self._waiters.remove(entry)

    async def wait_for_property(
        self, device: str, name: str, *, timeout: float | None = None
    ) -> Property:
        """Wait until ``device`` has defined its ``name`` property."""
        return await self.wait_until(device, name, lambda _: True, timeout=timeout)

    async def wait_for_device(self, device: str, *, timeout: float | None = None) -> None:
        """Wait until ``device`` exists, whatever property it defines first."""
        await self.wait_for_property(device, CONNECTION_PROPERTY, timeout=timeout)

    # --------------------------------------------------------------- writing

    async def write(
        self,
        device: str,
        name: str,
        values: Mapping[str, ElementValue],
        *,
        timeout: float | None = None,
        await_settle: bool = True,
    ) -> Property:
        """Write ``values`` into ``device``'s ``name`` vector.

        Waits for the driver to answer and for the vector to leave the Busy
        state, so that a caller that awaits this call knows the instrument has
        acted rather than merely been asked to.

        Raises:
            IndiError: if the property is unknown, is not writable, or names an
                element the driver did not define.
            IndiTimeoutError: if the driver does not settle in time.
        """
        prop = await self.wait_for_property(device, name, timeout=timeout)
        if not prop.is_writable:
            raise IndiError(f"{device}.{name} is {prop.permission.value}; it cannot be written")
        unknown = sorted(set(values) - set(prop.elements))
        if unknown:
            raise IndiError(
                f"{device}.{name} has no element(s) {', '.join(unknown)}; "
                f"it has: {', '.join(sorted(prop.elements))}"
            )

        key = (device, name)
        revision = self._revisions.get(key, 0)
        limit = self._settings.write_timeout_s if timeout is None else timeout

        def settled(candidate: Property) -> bool:
            return (
                self._revisions.get(key, 0) > revision
                and candidate.state is not PropertyState.BUSY
            )

        if not await_settle:
            await self._send(new_vector(prop.kind, device=device, name=name, values=values))
            return prop

        waiter = asyncio.ensure_future(self.wait_until(device, name, settled, timeout=limit))
        try:
            await self._send(new_vector(prop.kind, device=device, name=name, values=values))
        except BaseException:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
            raise
        return await waiter

    async def connect_device(self, device: str, *, timeout: float | None = None) -> None:
        """Connect ``device`` and wait until it reports itself connected."""
        limit = self._settings.device_connect_timeout_s if timeout is None else timeout
        await self.write(
            device,
            CONNECTION_PROPERTY,
            {CONNECT_ELEMENT: True, DISCONNECT_ELEMENT: False},
            timeout=limit,
        )
        await self.wait_until(
            device,
            CONNECTION_PROPERTY,
            lambda prop: prop.get(CONNECT_ELEMENT) is True,
            timeout=limit,
        )
        logger.info("device connected", extra={"device": device})

    async def disconnect_device(self, device: str, *, timeout: float | None = None) -> None:
        """Disconnect ``device`` and wait until it reports itself disconnected."""
        limit = self._settings.device_connect_timeout_s if timeout is None else timeout
        await self.write(
            device,
            CONNECTION_PROPERTY,
            {CONNECT_ELEMENT: False, DISCONNECT_ELEMENT: True},
            timeout=limit,
        )
        await self.wait_until(
            device,
            CONNECTION_PROPERTY,
            lambda prop: prop.get(CONNECT_ELEMENT) is not True,
            timeout=limit,
        )
        logger.info("device disconnected", extra={"device": device})

    async def refresh(self, device: str | None = None) -> None:
        """Ask the server to (re)define properties, for one device or for all."""
        await self._send(get_properties(device=device))

    # ---------------------------------------------------------------- events

    def _emit(self, event: IndiEvent) -> None:
        for handler in list(self._handlers):
            try:
                handler(event)
            except Exception:
                # A misbehaving subscriber must not take down the reader loop:
                # losing telemetry is survivable, losing mount control is not.
                logger.exception(
                    "INDI event handler raised", extra={"event": type(event).__name__}
                )


def switch_values(on: str, options: Iterator[str] | tuple[str, ...]) -> dict[str, ElementValue]:
    """Build the values for a OneOfMany switch: ``on`` On, every other Off."""
    return {name: (name == on) for name in options}


__all__ = [
    "CONNECTION_PROPERTY",
    "CONNECT_ELEMENT",
    "DISCONNECT_ELEMENT",
    "ConnectionState",
    "DeviceAppeared",
    "DeviceVanished",
    "DriverMessage",
    "EventHandler",
    "IndiClient",
    "IndiConnectionError",
    "IndiDeviceLostError",
    "IndiError",
    "IndiEvent",
    "IndiTimeoutError",
    "Predicate",
    "PropertyChanged",
    "PropertyKind",
    "ServerConnected",
    "ServerDisconnected",
    "switch_values",
]
