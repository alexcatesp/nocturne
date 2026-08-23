"""The Ekos DBus bridge — SPEC section 3 (layer 1) and section 4.

KStars/Ekos runs headless on the Pi and exposes its control surface on the
session bus as ``org.kde.kstars``. Nocturne drives it from here rather than
reimplementing polar alignment, autofocus, guiding and meridian handling
(SPEC section 3, rationale for Ekos as executor).

**On method names — THESE ARE NOW VERIFIED.** Every path, interface and method
name below was read off the reference rig on 2026-08-07, running KStars 3.8.3
(commit ``61d849b0``), and is recorded in
``backend/tests/fixtures/hardware/kstars-dbus-interfaces.xml`` and
``kstars-live-at-rest.xml``. ``test_ekos_recorded_interface.py`` asserts that
every constant here appears in those files, so a name that drifts from the
recorded interface fails in CI rather than at two in the morning
(https://github.com/alexcatesp/nocturne/issues/2).

**One of the guesses was wrong, and it is worth knowing which.** This module had
``connect_device(device)`` and ``disconnect_device(device)`` calling
``org.kde.kstars.INDI.connect``. That method is
``connect(host: s, port: i) -> b``: it attaches KStars' INDI *client* to an
indiserver, and has nothing to do with connecting a device. There is no
per-device connect on this interface at all — that is ``Ekos.connectDevices()``
for the whole profile, or a ``CONNECTION`` property write through
:class:`~nocturne.executor.indi.client.IndiClient` for one device. The stub in
``fake_kstars.py`` had implemented ``connect(device: s)``, agreeing with the
bridge because the same person wrote both, which is precisely why the tests
were green and the code was wrong.

The bridge still introspects the remote objects when it connects and refuses to
start if what it needs is absent, naming what it wanted and what it found. That
containment was never verification, and it is not redundant now: it is what
catches a KStars upgrade that moves the surface.

The Optical Trains interface (``org.kde.kstars.Ekos.OpticalTrain``, confirmed
present in the built tree) is deliberately absent from this module. Its objects
do not exist until Ekos has been started, and using them is M2.

**On what the bridge is for.** Generic property read/write against the drivers
goes through :class:`~nocturne.executor.indi.client.IndiClient`; SPEC section 4
reserves direct INDI access for "properties Ekos does not expose", and an
arbitrary vector on an arbitrary driver is exactly that. This bridge owns the
Ekos-level lifecycle: is Ekos up, are the devices connected, and the module
objects Ekos creates when it starts.

**The mount module is attached; the others are not yet.** ``Mount`` is here
because M2's first moving parts need it, and the four calls Nocturne makes on it
are gated by the safety governor like everything else that moves the
instrument. ``Align``, ``Focus``, ``Guide`` and ``Capture`` arrive with the work
that uses them, each with its own commands in :mod:`nocturne.safety.commands`;
until then this module does not name them, and a structural test in
``test_ekos_recorded_interface.py`` keeps it that way. Which of the mount
module's own methods are deliberately *not* called is ADR 0022.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from types import TracebackType
from typing import Any, Final, final

from dbus_next import BusType, Message, MessageType
from dbus_next.aio import MessageBus, ProxyInterface

from nocturne.executor.settings import EkosSettings

logger = logging.getLogger("nocturne.executor.ekos")

#: The well-known bus name KStars claims. Verified on the rig: ``ListNames``
#: returns this and no per-process ``org.kde.kstars-<pid>`` beside it, so
#: attaching to a fixed name is correct.
KSTARS_BUS_NAME: Final = "org.kde.kstars"

#: Object paths and interfaces. Adjust here if a KStars release moves them; the
#: bridge verifies them at connect time and will tell you if they are wrong.
EKOS_PATH: Final = "/KStars/Ekos"
EKOS_INTERFACE: Final = "org.kde.kstars.Ekos"
INDI_PATH: Final = "/KStars/INDI"
INDI_INTERFACE: Final = "org.kde.kstars.INDI"

#: Methods the bridge calls, checked by introspection before first use and
#: checked against the recorded interface in CI.
REQUIRED_EKOS_METHODS: Final[frozenset[str]] = frozenset(
    {"start", "stop", "connectDevices", "disconnectDevices"}
)
REQUIRED_INDI_METHODS: Final[frozenset[str]] = frozenset(
    {"getDevices", "connect", "disconnect"}
)

#: The Ekos mount module. **This object does not exist until ``Ekos.start`` has
#: run**, which is recorded rather than supposed: the two introspection captures
#: in ``backend/tests/fixtures/hardware/`` are the same KStars process before and
#: after that call, and it exports five paths in the first and twenty-four in the
#: second. :meth:`EkosBridge.attach_modules` is therefore separate from
#: :meth:`EkosBridge.connect`, and waits.
EKOS_MOUNT_PATH: Final = "/KStars/Ekos/Mount"
EKOS_MOUNT_INTERFACE: Final = "org.kde.kstars.Ekos.Mount"

#: The mount methods the bridge calls, checked by introspection when the module
#: is attached and checked against the recorded interface in CI.
#:
#: The four here are the whole of what Nocturne asks the mount module for, and
#: which of the nine it declares are deliberately absent — ``gotoTarget``,
#: ``syncTarget``, ``sync``, ``resetModel`` and ``setMeridianFlipValues`` — is
#: ADR 0022, enforced by ``test_ekos_recorded_interface.py`` rather than left to
#: memory.
REQUIRED_MOUNT_METHODS: Final[frozenset[str]] = frozenset({"slew", "park", "unpark", "abort"})

#: Mount-module methods Nocturne must not call, and the reason in one line each.
#: Named here so that the structural test has something to check the source
#: against; see ADR 0022 for the argument.
FORBIDDEN_MOUNT_METHODS: Final[Mapping[str, str]] = {
    "gotoTarget": "resolves a target name inside KStars, so the governor never sees "
    "the coordinates it would have to check",
    "syncTarget": "the same, and it moves the frame every limit is measured in",
    "sync": "moves the mount's model of where it is; plate-solve syncs belong to the "
    "align module, with the solved position in hand",
    "resetModel": "discards the mount's alignment model, which is the frame the "
    "meridian band is expressed in",
    "setMeridianFlipValues": "would make Ekos a third actor deciding when to flip, "
    "beside the governor and the driver limits of ADR 0010",
}

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
        settings: EkosSettings | None = None,
    ) -> None:
        self._bus_type = bus_type
        self._bus_address = bus_address
        self._reconnect_delay_s = reconnect_delay_s
        self._settings = settings if settings is not None else EkosSettings()
        self._bus: MessageBus | None = None
        self._ekos: ProxyInterface | None = None
        self._indi: ProxyInterface | None = None
        self._mount: ProxyInterface | None = None
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
        # A fresh KStars has not started Ekos, so whatever module proxies this
        # bridge held belong to a process that is gone. Dropping them here is
        # what makes a mount call after a restart raise rather than address a
        # dead object: attach_modules() has to be run again.
        self._mount = None
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
        self._mount = None
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
                self._mount = None
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

    @property
    def is_mount_attached(self) -> bool:
        """Whether the bridge holds a live handle on the Ekos mount module."""
        return self._mount is not None and self.is_connected

    async def attach_modules(self, *, timeout_s: float | None = None) -> None:
        """Attach to the Ekos module objects, waiting for Ekos to create them.

        Call after :meth:`start_ekos`. ``Ekos.start`` returns immediately and
        the module objects appear on the bus some time later, so this polls
        introspection until ``/KStars/Ekos/Mount`` is there and offers every
        method in :data:`REQUIRED_MOUNT_METHODS`.

        Idempotent: attaching twice re-introspects and replaces the handle,
        which is what a caller recovering from a KStars restart wants.

        Args:
            timeout_s: how long to wait. Defaults to the bridge's
                ``EkosSettings.module_attach_timeout_s``.

        Raises:
            EkosError: if the bridge is not connected to KStars at all.
            EkosInterfaceError: if the modules have not appeared within the
                timeout, or have appeared without the methods the bridge calls.
        """
        bus = self._bus
        if bus is None or not self.is_connected:
            raise EkosError(
                "not connected to KStars, so its module objects cannot be attached; "
                f"the bridge is {self._state.value}"
            )
        limit = timeout_s if timeout_s is not None else self._settings.module_attach_timeout_s
        deadline = asyncio.get_running_loop().time() + limit
        last: EkosInterfaceError | None = None
        while True:
            try:
                self._mount = await self._interface(
                    bus, EKOS_MOUNT_PATH, EKOS_MOUNT_INTERFACE, REQUIRED_MOUNT_METHODS
                )
            except EkosInterfaceError as exc:
                last = exc
            else:
                logger.info("Ekos mount module attached", extra={"path": EKOS_MOUNT_PATH})
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise EkosInterfaceError(
                    f"the Ekos module objects did not appear within {limit} s of "
                    "Ekos being started. They are created by Ekos, not by KStars, so "
                    "the usual cause is that Ekos never started — check that a profile "
                    f"is set and that start_ekos() was called. Last attempt: {last}"
                )
            await asyncio.sleep(self._settings.module_poll_interval_s)

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

    async def connect_indiserver(self, host: str, port: int) -> bool:
        """Attach KStars' INDI client to an indiserver at ``host:port``.

        ``org.kde.kstars.INDI.connect(host: s, port: i) -> b``, read from the
        recorded interface. **This does not connect a device**, despite the
        name, and this module used to assume it did — see the note at the top.
        To connect the devices in the active profile, use
        :meth:`connect_devices`; to connect one device,
        :class:`~nocturne.executor.indi.client.IndiClient` writes its
        ``CONNECTION`` property, and that path is the one the safety governor
        gates.
        """
        return bool(await self._call(self._indi, "connect", host, port))

    async def disconnect_indiserver(self, host: str, port: int) -> bool:
        """Detach KStars' INDI client from the indiserver at ``host:port``."""
        return bool(await self._call(self._indi, "disconnect", host, port))

    # ------------------------------------------------------------ the mount

    # Four calls, and no more. What the mount module also offers and Nocturne
    # does not use is FORBIDDEN_MOUNT_METHODS above, with the reason for each.
    #
    # **On the return value of all four.** KStars documents it as "true if the
    # command is sent successfully" — not that the mount accepted it, and
    # certainly not that the motion finished. A slew that returns True has been
    # handed to the driver and nothing more; whether the mount arrived is read
    # from its coordinates, which is the session layer's business.
    #
    # None of these is reachable from outside nocturne.executor: the Executor
    # holds the bridge privately and exposes no accessor, so the only way to
    # any of them is through a command the safety governor has approved
    # (CLAUDE.md invariant 1, enforced by tests/unit/test_safety_boundaries.py).

    def _mount_interface(self) -> ProxyInterface:
        """The mount module, or a refusal that says which step was skipped.

        Two different failures wear the same mask otherwise: KStars gone, and
        KStars present with Ekos never started. The first is waited out; the
        second is a call the caller has to make.
        """
        if not self.is_connected:
            raise EkosError(
                f"not connected to KStars; the bridge is {self._state.value}. "
                "Call connect() first, or wait for KStars to come back."
            )
        if self._mount is None:
            raise EkosError(
                "the Ekos mount module is not attached. Ekos creates its module "
                "objects when it starts, so call start_ekos() and then "
                "attach_modules() before commanding the mount."
            )
        return self._mount

    async def mount_slew(self, ra_hours: float, dec_deg: float) -> bool:
        """Slew the mount to an equatorial position, equinox of date.

        ``org.kde.kstars.Ekos.Mount.slew(RA: d, DEC: d) -> b``. Right ascension
        in **hours**, declination in **degrees**, in the **JNow** frame — read
        off the KStars source at the commit ``scripts/install.sh`` pins and
        recorded in ``backend/tests/fixtures/hardware/kstars-mount-dbus-units.txt``.
        The introspection XML carries neither the units nor the frame, and a
        right ascension sent in degrees is a slew to somewhere else entirely.
        """
        sent = await self._call(
            self._mount_interface(), "slew", float(ra_hours), float(dec_deg)
        )
        return bool(sent)

    async def mount_park(self) -> bool:
        """Drive the mount to its park position."""
        return bool(await self._call(self._mount_interface(), "park"))

    async def mount_unpark(self) -> bool:
        """Release the mount from its park position."""
        return bool(await self._call(self._mount_interface(), "unpark"))

    async def mount_abort(self) -> bool:
        """Stop the mount moving."""
        return bool(await self._call(self._mount_interface(), "abort"))
