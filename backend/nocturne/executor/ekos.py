"""The Ekos DBus bridge — SPEC section 3 (layer 1) and section 4.

KStars/Ekos runs headless on the Pi and exposes its control surface on the
session bus as ``org.kde.kstars``. Nocturne drives it from here rather than
reimplementing polar alignment, autofocus, guiding and meridian handling
(SPEC section 3, rationale for Ekos as executor).

**On method names — THESE ARE UNVERIFIED.** Every path, interface and method
name below was written from expectation, not read from a live KStars. The DBus
surface is not a published stable contract and has moved between releases.

The bridge introspects the remote objects when it connects and refuses to start
if what it needs is absent, naming what it wanted and what it found. That is
*containment*: a wrong name becomes a loud startup failure instead of a call
that silently does nothing at two in the morning. **It is not verification.**

Verifying them is https://github.com/alexcatesp/nocturne/issues/2, and it is the
first executor task once KStars builds — which is blocked on Trixie (ADR 0008)
and on the KStars tag (ADR 0006). The Optical Trains interface added in KStars
3.8.2 is absent from this module entirely and is part of that work.

**On what the bridge is for.** Generic property read/write against the drivers
goes through :class:`~nocturne.executor.indi.client.IndiClient`; SPEC section 4
reserves direct INDI access for "properties Ekos does not expose", and an
arbitrary vector on an arbitrary driver is exactly that. This bridge owns the
Ekos-level lifecycle: is Ekos up, are the devices connected, and — in M2 — the
align, focus, guide and capture modules.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from enum import StrEnum
from types import TracebackType
from typing import Any, Final, final

from dbus_next import BusType, Message, MessageType
from dbus_next.aio import MessageBus, ProxyInterface

logger = logging.getLogger("nocturne.executor.ekos")

#: The well-known bus name KStars claims.
KSTARS_BUS_NAME: Final = "org.kde.kstars"

#: Object paths and interfaces. Adjust here if a KStars release moves them; the
#: bridge verifies them at connect time and will tell you if they are wrong.
EKOS_PATH: Final = "/KStars/Ekos"
EKOS_INTERFACE: Final = "org.kde.kstars.Ekos"
INDI_PATH: Final = "/KStars/INDI"
INDI_INTERFACE: Final = "org.kde.kstars.INDI"

#: Methods the bridge calls. Verified by introspection before first use.
REQUIRED_EKOS_METHODS: Final[frozenset[str]] = frozenset(
    {"start", "stop", "connectDevices", "disconnectDevices"}
)
REQUIRED_INDI_METHODS: Final[frozenset[str]] = frozenset(
    {"getDevices", "connect", "disconnect"}
)

_DBUS_SERVICE: Final = "org.freedesktop.DBus"
_DBUS_PATH: Final = "/org/freedesktop/DBus"


class EkosError(Exception):
    """The bridge could not reach Ekos, or Ekos refused a call."""


class EkosUnavailableError(EkosError):
    """KStars is not on the session bus."""


class EkosInterfaceError(EkosError):
    """KStars is on the bus but does not offer the interface the bridge needs."""


class BridgeState(StrEnum):
    """Where the bridge is with respect to KStars."""

    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"


#: Called with the new state whenever it changes, so the session layer can
#: react to KStars disappearing without polling for it.
StateHandler = Callable[[BridgeState], None]


#: How long to wait for dbus-next to finish tearing a bus down before closing
#: its socket by hand.
BUS_TEARDOWN_TIMEOUT_S: Final = 2.0


async def close_bus(bus: MessageBus) -> None:
    """Disconnect ``bus`` and release its socket.

    dbus-next's ``disconnect()`` only shuts the socket down; the descriptor is
    released when the object is garbage collected, which shows up as a
    ResourceWarning and, over a night of KStars restarts, as leaked
    descriptors.

    The wait matters: ``disconnect()`` is asynchronous, and dbus-next removes
    its reader from the event loop in the teardown that follows. Closing the
    descriptor before that happens leaves the loop polling a closed file
    descriptor, which does not raise — it spins. So: ask it to disconnect, wait
    for it to finish, and only then close. The attributes are read defensively
    so that a future dbus-next which closes them itself still works.
    """
    with contextlib.suppress(Exception):
        bus.disconnect()  # type: ignore[no-untyped-call]  # dbus-next is untyped
    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            bus.wait_for_disconnect(),  # type: ignore[no-untyped-call]
            timeout=BUS_TEARDOWN_TIMEOUT_S,
        )
    for attribute in ("_stream", "_sock"):
        resource = getattr(bus, attribute, None)
        if resource is not None:
            with contextlib.suppress(Exception):
                resource.close()


def _snake(member: str) -> str:
    """DBus member name to the accessor dbus-next generates for it."""
    out: list[str] = []
    for index, character in enumerate(member):
        if character.isupper() and index:
            out.append("_")
        out.append(character.lower())
    return "".join(out)


@final
class EkosBridge:
    """Connects to KStars/Ekos over DBus and survives it restarting."""

    def __init__(
        self,
        *,
        bus_type: BusType = BusType.SESSION,
        bus_address: str | None = None,
        reconnect_delay_s: float = 2.0,
    ) -> None:
        self._bus_type = bus_type
        self._bus_address = bus_address
        self._reconnect_delay_s = reconnect_delay_s
        self._bus: MessageBus | None = None
        self._ekos: ProxyInterface | None = None
        self._indi: ProxyInterface | None = None
        self._state = BridgeState.DISCONNECTED
        self._handlers: list[StateHandler] = []
        self._watchdog: asyncio.Task[None] | None = None
        self._owner_changes: asyncio.Queue[str] = asyncio.Queue()
        self._shutdown = asyncio.Event()

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> BridgeState:
        """Bridge state with respect to KStars."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Whether the bridge currently has a usable connection to Ekos."""
        return self._state is BridgeState.CONNECTED

    def subscribe(self, handler: StateHandler) -> Callable[[], None]:
        """Register ``handler`` for state changes. Returns an unsubscribe callable."""
        self._handlers.append(handler)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._handlers.remove(handler)

        return unsubscribe

    def _set_state(self, state: BridgeState) -> None:
        if state is self._state:
            return
        self._state = state
        logger.info("Ekos bridge state", extra={"state": state.value})
        for handler in list(self._handlers):
            try:
                handler(state)
            except Exception:
                logger.exception("Ekos bridge state handler raised")

    # -------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> EkosBridge:
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
        """Attach to the session bus and verify the Ekos interface.

        Raises:
            EkosUnavailableError: if KStars is not on the bus.
            EkosInterfaceError: if it is, but without the expected methods.
        """
        if self._state is BridgeState.CLOSED:
            raise EkosError("this bridge has been closed and cannot reconnect")
        await self._attach()
        if self._watchdog is None:
            self._watchdog = asyncio.create_task(self._watch_owner(), name="ekos-watchdog")

    async def aclose(self) -> None:
        """Detach from the bus and stop watching for KStars."""
        if self._state is BridgeState.CLOSED:
            return
        self._shutdown.set()
        self._set_state(BridgeState.CLOSED)
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog
            self._watchdog = None
        await self._detach()

    async def _attach(self) -> None:
        bus = await self._open_bus()
        if not await self._name_has_owner(bus, KSTARS_BUS_NAME):
            raise EkosUnavailableError(
                f"{KSTARS_BUS_NAME} is not on the session bus. Start KStars headless "
                "(see scripts/systemd/nocturne-kstars.service) before connecting."
            )
        self._ekos = await self._interface(
            bus, EKOS_PATH, EKOS_INTERFACE, REQUIRED_EKOS_METHODS
        )
        self._indi = await self._interface(
            bus, INDI_PATH, INDI_INTERFACE, REQUIRED_INDI_METHODS
        )
        self._set_state(BridgeState.CONNECTED)

    async def _open_bus(self) -> MessageBus:
        if self._bus is not None and self._bus.connected:
            return self._bus
        try:
            bus = (
                MessageBus(bus_type=self._bus_type)
                if self._bus_address is None
                else MessageBus(bus_type=self._bus_type, bus_address=self._bus_address)
            )
            self._bus = await bus.connect()
        except Exception as exc:  # dbus-next raises a variety of transport errors
            raise EkosUnavailableError(f"cannot reach the DBus session bus: {exc}") from exc
        await self._add_owner_match(self._bus)
        return self._bus

    async def _detach(self) -> None:
        self._ekos = None
        self._indi = None
        if self._bus is not None:
            await close_bus(self._bus)
            self._bus = None

    @staticmethod
    async def _name_has_owner(bus: MessageBus, name: str) -> bool:
        reply = await bus.call(
            Message(
                destination=_DBUS_SERVICE,
                path=_DBUS_PATH,
                interface=_DBUS_SERVICE,
                member="NameHasOwner",
                signature="s",
                body=[name],
            )
        )
        if reply is None or reply.message_type is not MessageType.METHOD_RETURN:
            return False
        return bool(reply.body[0])

    async def _interface(
        self, bus: MessageBus, path: str, interface: str, required: frozenset[str]
    ) -> ProxyInterface:
        try:
            introspection = await bus.introspect(KSTARS_BUS_NAME, path)
        except Exception as exc:
            raise EkosInterfaceError(
                f"cannot introspect {KSTARS_BUS_NAME}{path}: {exc}"
            ) from exc

        node = next((item for item in introspection.interfaces if item.name == interface), None)
        if node is None:
            offered = ", ".join(sorted(item.name for item in introspection.interfaces))
            raise EkosInterfaceError(
                f"{KSTARS_BUS_NAME}{path} does not offer {interface}. "
                f"It offers: {offered or '<nothing>'}. This KStars build is not the one "
                "Nocturne expects; correct the constants in nocturne/executor/ekos.py."
            )

        offered_methods = {method.name for method in node.methods}
        missing = sorted(required - offered_methods)
        if missing:
            raise EkosInterfaceError(
                f"{interface} is missing the method(s) {', '.join(missing)}. "
                f"It offers: {', '.join(sorted(offered_methods)) or '<nothing>'}. "
                "Correct the constants in nocturne/executor/ekos.py for this KStars "
                "release."
            )
        proxy = bus.get_proxy_object(KSTARS_BUS_NAME, path, introspection)
        return proxy.get_interface(interface)

    # -------------------------------------------------------------- watchdog

    async def _add_owner_match(self, bus: MessageBus) -> None:
        await bus.call(
            Message(
                destination=_DBUS_SERVICE,
                path=_DBUS_PATH,
                interface=_DBUS_SERVICE,
                member="AddMatch",
                signature="s",
                body=[
                    "type='signal',interface='org.freedesktop.DBus',"
                    f"member='NameOwnerChanged',arg0='{KSTARS_BUS_NAME}'"
                ],
            )
        )
        bus.add_message_handler(self._on_bus_message)

    def _on_bus_message(self, message: Message) -> None:
        if (
            message.message_type is not MessageType.SIGNAL
            or message.member != "NameOwnerChanged"
            or not message.body
            or message.body[0] != KSTARS_BUS_NAME
        ):
            return
        new_owner = message.body[2] if len(message.body) > 2 else ""
        self._owner_changes.put_nowait(new_owner)

    async def _watch_owner(self) -> None:
        """React to KStars leaving and rejoining the bus."""
        while not self._shutdown.is_set():
            new_owner = await self._owner_changes.get()
            if new_owner:
                logger.info("KStars reappeared on the bus; reattaching")
                await self._reattach()
            else:
                logger.warning("KStars left the session bus")
                self._ekos = None
                self._indi = None
                self._set_state(BridgeState.RECONNECTING)

    async def _reattach(self) -> None:
        while not self._shutdown.is_set():
            try:
                await self._attach()
                return
            except EkosError as exc:
                logger.warning("could not reattach to Ekos", extra={"reason": str(exc)})
                await asyncio.sleep(self._reconnect_delay_s)

    # ------------------------------------------------------------------ calls

    def _require(self, interface: ProxyInterface | None, what: str) -> ProxyInterface:
        if interface is None or not self.is_connected:
            raise EkosError(
                f"not connected to {what}; the bridge is {self._state.value}. "
                "Call connect() first, or wait for KStars to come back."
            )
        return interface

    async def _call(self, interface: ProxyInterface | None, member: str, *args: object) -> Any:
        """Invoke ``member`` on ``interface``. The DBus return type is dynamic."""
        proxy = self._require(interface, member)
        method = getattr(proxy, f"call_{_snake(member)}", None)
        if method is None:
            raise EkosInterfaceError(f"{member} is not available on this KStars build")
        try:
            return await method(*args)
        except Exception as exc:
            raise EkosError(f"Ekos rejected {member}: {exc}") from exc

    async def start_ekos(self) -> None:
        """Start the Ekos manager with the configured profile."""
        await self._call(self._ekos, "start")

    async def stop_ekos(self) -> None:
        """Stop the Ekos manager."""
        await self._call(self._ekos, "stop")

    async def connect_devices(self) -> None:
        """Ask Ekos to connect every device in the active profile."""
        await self._call(self._ekos, "connectDevices")

    async def disconnect_devices(self) -> None:
        """Ask Ekos to disconnect every device in the active profile."""
        await self._call(self._ekos, "disconnectDevices")

    async def devices(self) -> Sequence[str]:
        """Names of the devices Ekos currently knows about."""
        result = await self._call(self._indi, "getDevices")
        return [str(name) for name in result or ()]

    async def connect_device(self, device: str) -> None:
        """Connect one device through Ekos."""
        await self._call(self._indi, "connect", device)

    async def disconnect_device(self, device: str) -> None:
        """Disconnect one device through Ekos."""
        await self._call(self._indi, "disconnect", device)
