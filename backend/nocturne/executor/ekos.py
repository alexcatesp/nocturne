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
because M2's first moving parts need it, and **every one of the nine methods it
exports** is driven through a command the safety governor sees first — the five
ADR 0022 refused are bounded by rules of their own instead (ADR 0023).
``Align``, ``Focus``, ``Guide`` and ``Capture`` arrive with the work that uses
them, each with its own commands in :mod:`nocturne.safety.commands`; until then
this module does not name them, and a structural test in
``test_ekos_recorded_interface.py`` keeps it that way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Final, final

from dbus_next import BusType, Message, MessageType
from dbus_next.aio import MessageBus, ProxyInterface

from nocturne.executor.settings import EkosSettings
from nocturne.safety.commands import ResolvedTarget
from nocturne.safety.sky import DEGREES_PER_HOUR, SkyPosition, datetime_from_julian_date

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

#: The mount methods the bridge calls: **every one the module declares**.
#:
#: ADR 0022 called four of them and refused five. ADR 0023 supersedes that at the
#: operator's decision: all nine are driven, and each of the five that was
#: refused carries a rule of its own instead of a refusal — a sync is bounded by
#: how far it moves the mount's model, a named target is resolved and its
#: coordinates validated before anything moves, a model reset requires a parked
#: mount, and Ekos' own flip must fire before the governor's keep-out band
#: rather than after.
#:
#: ``test_ekos_recorded_interface.py`` asserts this set is exactly the recorded
#: interface, so a KStars release that adds a method fails the suite rather than
#: leaving a call nobody decided about.
REQUIRED_MOUNT_METHODS: Final[frozenset[str]] = frozenset(
    {
        "slew",
        "sync",
        "gotoTarget",
        "syncTarget",
        "park",
        "unpark",
        "abort",
        "resetModel",
        "setMeridianFlipValues",
    }
)

#: Mount-module properties the bridge reads. Reads are not commands and are not
#: gated — but two of them are what make three of the commands *judgeable* at
#: all, so they are named and checked against the recording like the methods.
REQUIRED_MOUNT_PROPERTIES: Final[frozenset[str]] = frozenset({"equatorialCoords", "parkStatus"})

#: ``ISD::ParkStatus``, from ``kstars/indi/indistd.h`` at the pinned commit:
#: ``{PARK_UNKNOWN, PARK_PARKED, PARK_PARKING, PARK_UNPARKING, PARK_UNPARKED,
#: PARK_ERROR}``. The DBus type is a bare ``i`` and the introspection XML says
#: only ``ISD::Mount::ParkStatus``, so the value is recorded here and in
#: ``kstars-mount-dbus-units.txt`` rather than counted from memory.
#:
#: **Only this one value means parked.** Everything else — including
#: ``PARK_UNKNOWN``, which is what the property reads before the driver has said
#: anything — means not parked, because a model reset against an unknown state
#: is the thing ``require_the_mount_to_be_parked`` exists to prevent.
PARK_STATUS_PARKED: Final = 1

#: The Ekos align module. Created when Ekos starts, like the mount module.
EKOS_ALIGN_PATH: Final = "/KStars/Ekos/Align"
EKOS_ALIGN_INTERFACE: Final = "org.kde.kstars.Ekos.Align"

#: The align methods the bridge calls. Twelve of the thirteen the module
#: declares; ``loadAndSlew`` is the one exception and the reason is below.
REQUIRED_ALIGN_METHODS: Final[frozenset[str]] = frozenset(
    {
        "captureAndSolve",
        "abort",
        "setSolverAction",
        "setSolverMode",
        "setTargetCoords",
        "setTargetPositionAngle",
        "setBinningIndex",
        "getSolutionResult",
        "getTargetCoords",
        "getLoadAndSlewStatus",
        "cameraInfo",
        "telescopeInfo",
    }
)

#: Align-module methods Nocturne must not call, and why. One entry, and it is
#: not caution — it is the source (ADR 0024).
FORBIDDEN_ALIGN_METHODS: Final[Mapping[str, str]] = {
    "loadAndSlew": "solves an image off disk and slews to whatever it finds, so the "
    "governor cannot know the destination until the mount is already there. Worse, "
    "align_goto.cpp shows it sets m_SolveFromFile, which makes executeGOTO() slew "
    "regardless of the goto mode, and leaves m_CurrentGotoMode at GOTO_SLEW — so it "
    "would turn the next ordinary solve into a slew as well",
}

#: Align-module properties the bridge reads. One, deliberately.
#:
#: The module declares seven, and listing all seven would look thorough and be
#: worse: the containment check exists to refuse a KStars whose surface has
#: moved out from under a call Nocturne makes, and a property nothing reads
#: cannot break a call. Requiring it would turn an irrelevant upstream rename
#: into a rig that will not start.
REQUIRED_ALIGN_PROPERTIES: Final[frozenset[str]] = frozenset({"status"})

#: ``Ekos::Align::GotoMode``, from ``kstars/ekos/align/align.h``:
#: ``{GOTO_SYNC, GOTO_SLEW, GOTO_NOTHING}``. What Ekos does with a solution once
#: it has one.
#:
#: **Nocturne uses the third and only the third.** With ``GOTO_NOTHING``,
#: ``Align::executeGOTO()`` does nothing at all: solving becomes a read, and what
#: happens next is a command the safety governor sees. The other two make Ekos
#: sync or slew the mount itself, behind the governor, which is the second door
#: CLAUDE.md invariant 1 forbids (ADR 0024).
GOTO_MODE_SYNC: Final = 0
GOTO_MODE_SLEW: Final = 1
GOTO_MODE_NOTHING: Final = 2

#: ``SolverMode``: 0 local (StellarSolver, on this Pi), 1 remote (an astrometry
#: driver on the indiserver). Nocturne solves locally — SPEC section 4 builds
#: StellarSolver for exactly this — and says so explicitly rather than inheriting
#: whatever the profile was last left on.
SOLVER_MODE_LOCAL: Final = 0
SOLVER_MODE_REMOTE: Final = 1

#: ``Ekos::AlignState``, from ``kstars/ekos/ekos.h`` at the pinned commit. The
#: DBus property is a bare ``i``, so the names live here.
#:
#: ``ALIGN_SYNCING`` and ``ALIGN_SLEWING`` are in this list and Nocturne should
#: never see either: they are the states Ekos enters when it acts on a solution
#: itself, which the goto mode set by every solve rules out. Seeing one means
#: something put the module into a mode Nocturne did not ask for.
ALIGN_STATE_NAMES: Final[Mapping[int, str]] = {
    0: "idle",
    1: "complete",
    2: "failed",
    3: "aborted",
    4: "in progress",
    5: "successful",
    6: "syncing",
    7: "slewing",
    8: "rotating",
    9: "suspended",
}

#: The one state that means a solution is in hand.
ALIGN_STATE_SUCCESSFUL: Final = 5

#: The two Nocturne must never see, because they are Ekos moving the mount.
ALIGN_STATES_THAT_MOVE_THE_MOUNT: Final[frozenset[int]] = frozenset({6, 7})

#: ``getSolutionResult()`` returns **three** doubles, and the first is not what
#: KStars' own documentation says it is. ``align_goto.cpp`` returns
#: ``sOrientation << sRA << sDEC``; the doxygen comment on the declaration says
#: "First item is RA in degrees. Second item is DEC in degrees."
#:
#: Following the comment would read the field orientation as a right ascension
#: and the right ascension as a declination — a fabricated position, syncable
#: into the mount's model, with nothing in the ``ad`` signature to object. The
#: indices are therefore named here, from the implementation, and asserted in CI
#: against the excerpt in ``kstars-mount-dbus-units.txt``.
SOLUTION_ORIENTATION_INDEX: Final = 0
SOLUTION_RA_INDEX: Final = 1
SOLUTION_DEC_INDEX: Final = 2

#: The main KStars object. Not Ekos': it exists from the moment KStars starts,
#: and it is where the sky catalogue lives.
KSTARS_PATH: Final = "/KStars"
KSTARS_INTERFACE: Final = "org.kde.kstars"

#: What the bridge asks the catalogue for. ``getObjectPositionInfo`` returns a
#: position precessed to KStars' *current* clock, and reports that clock beside
#: it; ``getObjectDataXML`` reports the object's type, which is how a body that
#: does not hold still is caught before it becomes a slew (SPEC section 1.2).
REQUIRED_KSTARS_METHODS: Final[frozenset[str]] = frozenset(
    {"getObjectPositionInfo", "getObjectDataXML"}
)

#: What KStars returns from a lookup it could not resolve. Not an error and not
#: an empty string: a well-formed document with nothing in it.
UNRESOLVED_OBJECT_XML: Final = "<xml></xml>"

_DBUS_SERVICE: Final = "org.freedesktop.DBus"
_DBUS_PATH: Final = "/org/freedesktop/DBus"


@final
@dataclass(frozen=True, slots=True)
class AlignSolution:
    """What the plate solver found, as it reports it.

    ``position_j2000`` is deliberately named for its frame. The align module
    answers in J2000 and the mount points in the equinox of date; a solution
    handed to ``Mount.sync`` without precessing writes 0.3 degrees of frame
    error into the mount's model, every time.
    """

    position_j2000: SkyPosition
    #: The field's orientation in degrees, as the solver measured it. Carried
    #: because it is the first element of the answer and dropping it silently is
    #: how the index mistake gets made again.
    orientation_deg: float


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


def _object_fields(document: str, name: str) -> dict[str, str]:
    """The text elements of one of KStars' object documents, flattened.

    Both ``getObjectPositionInfo`` and ``getObjectDataXML`` return a single
    element with one level of text children, so a dictionary is the whole of the
    structure.

    Raises:
        EkosError: if the document does not parse, or carries no fields. An
            empty mapping would make every lookup below report a missing field
            rather than a broken answer, and those want different fixes.
    """
    # S314: the input comes from KStars over the session bus, which is the same
    # trust boundary as every other call in this module. defusedxml would be a
    # new dependency and therefore an ADR (CLAUDE.md section 6).
    try:
        root = ElementTree.fromstring(document)  # noqa: S314
    except ElementTree.ParseError as exc:
        raise EkosError(
            f"KStars answered the lookup for {name!r} with something that is not XML: {exc}"
        ) from exc
    fields = {
        child.tag: (child.text or "").strip() for child in root if isinstance(child.tag, str)
    }
    if not fields:
        raise EkosError(
            f"KStars answered the lookup for {name!r} with an empty document. It "
            "returns one for a name it does not know, and this is not that — so the "
            "shape of the answer has changed."
        )
    return fields


def _field_float(fields: Mapping[str, str], key: str, name: str) -> float:
    """One numeric field, or an error naming the field and the target."""
    raw = fields.get(key)
    if raw is None:
        raise EkosError(
            f"KStars' answer for {name!r} carries no {key}. This is the document "
            "getObjectPositionInfo is documented to return; it no longer is."
        )
    try:
        return float(raw)
    except ValueError as exc:
        raise EkosError(f"{key} for {name!r} is {raw!r}, which is not a number") from exc


#: dbus-next converts a DBus member name to the Python accessor it generates
#: with these two substitutions, in this order. They are copied from
#: ``dbus_next.proxy_object.BaseProxyInterface``, and
#: ``test_ekos_recorded_interface.py`` asserts this module agrees with it over
#: every member name in the recorded interface rather than over an example.
#:
#: **Runs of capitals are why this is not a one-line loop.** A rule that
#: inserted an underscore before every capital turns ``getObjectDataXML`` into
#: ``get_object_data_x_m_l``; dbus-next keeps the run together and produces
#: ``get_object_data_xml``. Every member the bridge called before that one
#: happened to have no capital run, so the naive version was correct for
#: everything it was ever asked and wrong for the first name with an acronym in
#: it — which surfaced as "not available on this KStars build" for a method
#: KStars plainly has.
_SNAKE_BEFORE_WORD = re.compile(r"(.)([A-Z][a-z]+)")
_SNAKE_BETWEEN = re.compile(r"([a-z0-9])([A-Z])")


def _snake(member: str) -> str:
    """DBus member name to the accessor dbus-next generates for it."""
    subbed = _SNAKE_BEFORE_WORD.sub(r"\1_\2", member)
    return _SNAKE_BETWEEN.sub(r"\1_\2", subbed).lower()


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
        self._kstars: ProxyInterface | None = None
        self._mount: ProxyInterface | None = None
        self._align: ProxyInterface | None = None
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
        # The catalogue. Unlike the module objects this exists from the moment
        # KStars starts, so it attaches here rather than in attach_modules().
        self._kstars = await self._interface(
            bus, KSTARS_PATH, KSTARS_INTERFACE, REQUIRED_KSTARS_METHODS
        )
        # A fresh KStars has not started Ekos, so whatever module proxies this
        # bridge held belong to a process that is gone. Dropping them here is
        # what makes a mount call after a restart raise rather than address a
        # dead object: attach_modules() has to be run again.
        self._mount = None
        self._align = None
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
        self._kstars = None
        self._mount = None
        self._align = None
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
        self,
        bus: MessageBus,
        path: str,
        interface: str,
        required: frozenset[str],
        *,
        properties: frozenset[str] = frozenset(),
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
        offered_properties = {item.name for item in node.properties}
        missing_properties = sorted(properties - offered_properties)
        if missing_properties:
            raise EkosInterfaceError(
                f"{interface} is missing the propert(ies) "
                f"{', '.join(missing_properties)}. It offers: "
                f"{', '.join(sorted(offered_properties)) or '<nothing>'}. Nothing "
                "here is read for decoration: a command that cannot be judged "
                "without one of these would otherwise be judged without it, so the "
                "bridge refuses to attach instead."
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
                self._kstars = None
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

    @property
    def is_align_attached(self) -> bool:
        """Whether the bridge holds a live handle on the Ekos align module."""
        return self._align is not None and self.is_connected

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
                    bus,
                    EKOS_MOUNT_PATH,
                    EKOS_MOUNT_INTERFACE,
                    REQUIRED_MOUNT_METHODS,
                    properties=REQUIRED_MOUNT_PROPERTIES,
                )
                self._align = await self._interface(
                    bus,
                    EKOS_ALIGN_PATH,
                    EKOS_ALIGN_INTERFACE,
                    REQUIRED_ALIGN_METHODS,
                    properties=REQUIRED_ALIGN_PROPERTIES,
                )
            except EkosInterfaceError as exc:
                last = exc
            else:
                logger.info(
                    "Ekos modules attached",
                    extra={"paths": [EKOS_MOUNT_PATH, EKOS_ALIGN_PATH]},
                )
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

    def _kstars_interface(self) -> ProxyInterface:
        """The main KStars object, or a refusal saying why it is not there."""
        if not self.is_connected or self._kstars is None:
            raise EkosError(
                f"not connected to KStars; the bridge is {self._state.value}. "
                "Call connect() first, or wait for KStars to come back."
            )
        return self._kstars

    async def _get_property(self, interface: ProxyInterface, member: str) -> Any:
        """Read a DBus property. The value type is dynamic, like ``_call``."""
        getter = getattr(interface, f"get_{_snake(member)}", None)
        if getter is None:
            raise EkosInterfaceError(f"{member} is not readable on this KStars build")
        try:
            return await getter()
        except Exception as exc:
            raise EkosError(f"could not read {member}: {exc}") from exc

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

    async def mount_sync(self, ra_hours: float, dec_deg: float) -> bool:
        """Tell the mount it is at this equatorial position, equinox of date.

        ``Mount.sync(RA: d, DEC: d) -> b``, the same units and frame as
        :meth:`mount_slew` — hours, degrees, JNow. This moves nothing: it moves
        the mount's model. Which is why the governor bounds how far.
        """
        sent = await self._call(
            self._mount_interface(), "sync", float(ra_hours), float(dec_deg)
        )
        return bool(sent)

    async def mount_goto_target(self, name: str) -> bool:
        """Slew to a target KStars resolves by name.

        The name is resolved a second time here, inside KStars, having already
        been resolved by :meth:`resolve_target` for the governor to validate.
        That is deliberate and it is not redundant: KStars' own lookup is what
        actually decides where the mount goes, so what the governor approved has
        to be what KStars will find — and the same in-process catalogue, asked
        twice within a session, answers the same for the fixed objects
        SPEC section 1.2 restricts this to. A body that does *not* hold still is
        refused by the governor before reaching here, on the object type this
        same catalogue reported.
        """
        return bool(await self._call(self._mount_interface(), "gotoTarget", name))

    async def mount_sync_target(self, name: str) -> bool:
        """Tell the mount it is pointing at a target KStars resolves by name."""
        return bool(await self._call(self._mount_interface(), "syncTarget", name))

    async def mount_reset_model(self) -> bool:
        """Discard the mount's alignment model.

        Returns false when the mount has no alignment model to discard, which
        KStars checks before doing anything — so a false here is "nothing to
        do", not "it failed". The distinction matters to a caller deciding
        whether to retry.
        """
        return bool(await self._call(self._mount_interface(), "resetModel"))

    async def mount_set_meridian_flip(self, enabled: bool, offset_deg: float) -> bool:
        """Configure Ekos' own automatic meridian flip.

        ``Mount.setMeridianFlipValues(activate: b, hours: d)`` — and the second
        argument is **degrees**, not hours, despite that name. KStars' own
        adaptor XML says ``hours``; the implementation names the parameter
        ``degrees``, writes it into ``meridianFlipOffsetDegrees``, and divides by
        fifteen when it uses it. The excerpt proving this is in
        ``backend/tests/fixtures/hardware/kstars-mount-dbus-units.txt``, and it
        is why this method's argument is named for the unit rather than for the
        DBus signature.

        Returns True once the call has been sent. The method is declared
        ``NoReply`` in the adaptor, so there is nothing to report but that.
        """
        await self._call(
            self._mount_interface(), "setMeridianFlipValues", bool(enabled), float(offset_deg)
        )
        return True

    # ------------------------------------------------- reading the mount

    # Not commands. These are what let the governor judge a sync and a model
    # reset, by travelling with the command that needs them (ADR 0023).

    async def mount_equatorial_coords(self) -> tuple[float, float]:
        """Where the mount says it is: ``(RA hours, DEC degrees)``, JNow.

        KStars documents the order and the units on ``equatorialCoords``: "First
        element is RA in hours. Second elements is DEC in degrees."

        Raises:
            EkosError: if the mount answers with anything but two numbers. A
                pair that cannot be read is not defaulted to zero — that is a
                real position, on the celestial equator at the vernal equinox,
                and a sync measured against it would look small from some parts
                of the sky and enormous from others.
        """
        raw = await self._get_property(self._mount_interface(), "equatorialCoords")
        try:
            ra_hours, dec_deg = (float(value) for value in raw)
        except (TypeError, ValueError) as exc:
            raise EkosError(
                f"the mount reported its position as {raw!r}, which is not a right "
                f"ascension in hours and a declination in degrees: {exc}"
            ) from exc
        return ra_hours, dec_deg

    async def mount_is_parked(self) -> bool:
        """Whether the mount reports itself parked, and only that.

        ``PARK_PARKING``, ``PARK_UNPARKING``, ``PARK_ERROR`` and the
        ``PARK_UNKNOWN`` the property holds before the driver has said anything
        all answer False. Parked is a state to be *in*, not one to be heading
        towards.
        """
        raw = await self._get_property(self._mount_interface(), "parkStatus")
        return int(raw) == PARK_STATUS_PARKED

    # ---------------------------------------------------------- the align

    # Twelve calls. What the module also offers and Nocturne does not use is
    # FORBIDDEN_ALIGN_METHODS above, with the reason from KStars' own source.
    #
    # **Solving is a read.** Every one of these either configures the solver or
    # asks it a question; none of them moves the instrument, and that is not an
    # accident of which ones were chosen. Ekos acts on a solution in
    # Align::executeGOTO(), which does nothing at all while the goto mode is
    # GOTO_NOTHING — so the mode is set to GOTO_NOTHING as part of every solve,
    # by the Executor, in the same governed command. What happens to the mount
    # afterwards is a decision Nocturne takes and the governor sees (ADR 0024).

    def _align_interface(self) -> ProxyInterface:
        """The align module, or a refusal that says which step was skipped."""
        if not self.is_connected:
            raise EkosError(
                f"not connected to KStars; the bridge is {self._state.value}. "
                "Call connect() first, or wait for KStars to come back."
            )
        if self._align is None:
            raise EkosError(
                "the Ekos align module is not attached. Ekos creates its module "
                "objects when it starts, so call start_ekos() and then "
                "attach_modules() before solving."
            )
        return self._align

    async def align_set_solver_action(self, mode: int) -> None:
        """Choose what Ekos does with a solution once it has one.

        Nocturne passes :data:`GOTO_MODE_NOTHING` and nothing else; the argument
        exists because the DBus method takes one, and a structural test asserts
        that no other value is ever passed.
        """
        await self._call(self._align_interface(), "setSolverAction", int(mode))

    async def align_set_solver_mode(self, mode: int) -> None:
        """Solve locally or on a remote astrometry driver."""
        await self._call(self._align_interface(), "setSolverMode", int(mode))

    async def align_capture_and_solve(self) -> bool:
        """Expose a frame and plate-solve it.

        Returns whether the procedure *started*. KStars is explicit that a true
        return means "started successfully", and it returns true when retrying —
        so this says nothing about whether a solution was found. That is read
        from :meth:`align_status` and :meth:`align_solution`.
        """
        return bool(await self._call(self._align_interface(), "captureAndSolve"))

    async def align_abort(self) -> None:
        """Stop the solver. Declared NoReply, so there is nothing to report."""
        await self._call(self._align_interface(), "abort")

    async def align_set_target_coords(self, ra_hours: float, dec_deg: float) -> None:
        """Set the coordinates the solver measures its solution against.

        **J2000**, right ascension in hours — not the equinox of date the mount
        works in, and not the degrees ``getSolutionResult`` answers in. Three
        conventions on one interface; this is the one that takes hours and J2000,
        and ``align_goto.cpp`` setting ``ra0``/``dec0`` is where that is settled.
        """
        await self._call(
            self._align_interface(), "setTargetCoords", float(ra_hours), float(dec_deg)
        )

    async def align_set_target_position_angle(self, angle_deg: float) -> None:
        """Set the position angle the rotator should reach, in degrees."""
        await self._call(self._align_interface(), "setTargetPositionAngle", float(angle_deg))

    async def align_set_binning_index(self, index: int) -> None:
        """Set the solver's capture binning, by index into the camera's list."""
        await self._call(self._align_interface(), "setBinningIndex", int(index))

    async def align_solution(self) -> AlignSolution | None:
        """The last solution, or ``None`` if the solver has not produced one.

        The position is **J2000**, because that is what the solver reports and
        what ``align_solver.cpp`` divides by fifteen before storing as ``ra0``.
        Converting it to the mount's frame is
        :func:`~nocturne.safety.sky.precess_j2000_to_date`, and it happens in the
        executor rather than here so that the boundary has exactly one place.

        Raises:
            EkosError: if the answer is not the three doubles it is documented —
                wrongly — to be. See :data:`SOLUTION_RA_INDEX`.
        """
        raw = await self._get_property_or_call(self._align_interface(), "getSolutionResult")
        values = [float(item) for item in raw or ()]
        if len(values) <= SOLUTION_DEC_INDEX:
            raise EkosError(
                f"the align module answered getSolutionResult with {raw!r}: "
                f"{len(values)} values where the implementation returns three "
                "(orientation, RA, DEC). The shape of the answer has changed."
            )
        ra_degrees = values[SOLUTION_RA_INDEX]
        dec_degrees = values[SOLUTION_DEC_INDEX]
        if not (math.isfinite(ra_degrees) and math.isfinite(dec_degrees)):
            return None
        try:
            position = SkyPosition(ra_hours=ra_degrees / DEGREES_PER_HOUR, dec_deg=dec_degrees)
        except ValueError:
            # INVALID_VALUE, which is what the fields hold before a solve.
            return None
        return AlignSolution(
            position_j2000=position, orientation_deg=values[SOLUTION_ORIENTATION_INDEX]
        )

    async def align_target_coords(self) -> SkyPosition | None:
        """The target the solver is measuring against, J2000, or ``None``."""
        raw = await self._get_property_or_call(self._align_interface(), "getTargetCoords")
        values = [float(item) for item in raw or ()]
        if len(values) < 2:
            return None
        try:
            return SkyPosition(ra_hours=values[0], dec_deg=values[1])
        except ValueError:
            return None

    async def align_status(self) -> int:
        """``Ekos::AlignState``. See :data:`ALIGN_STATE_NAMES`."""
        return int(await self._get_property(self._align_interface(), "status"))

    async def align_load_and_slew_status(self) -> int:
        """The state of a load-and-slew procedure. Nocturne never starts one."""
        return int(
            await self._get_property_or_call(self._align_interface(), "getLoadAndSlewStatus")
        )

    async def align_camera_info(self) -> Sequence[float]:
        """Width, height, pixel size x, pixel size y — of the solving camera."""
        raw = await self._get_property_or_call(self._align_interface(), "cameraInfo")
        return [float(item) for item in raw or ()]

    async def align_telescope_info(self) -> Sequence[float]:
        """Focal length and aperture, as Ekos has them for the active train."""
        raw = await self._get_property_or_call(self._align_interface(), "telescopeInfo")
        return [float(item) for item in raw or ()]

    async def _get_property_or_call(self, interface: ProxyInterface, member: str) -> Any:
        """Read ``member`` whether KStars exports it as a property or a method.

        The align module declares ``status`` as a property and
        ``getSolutionResult`` as a method, and which of the two a given accessor
        is has changed between KStars releases. Asking for whichever exists costs
        one ``getattr`` and removes a class of upgrade breakage that would
        otherwise present as "not available on this KStars build".
        """
        getter = getattr(interface, f"get_{_snake(member)}", None)
        if getter is not None:
            try:
                return await getter()
            except Exception as exc:
                raise EkosError(f"could not read {member}: {exc}") from exc
        return await self._call(interface, member)

    # ------------------------------------------------------ the catalogue

    async def resolve_target(self, name: str) -> ResolvedTarget | None:
        """Look ``name`` up in KStars' catalogue, or ``None`` if it is not there.

        Two calls, because one document does not carry both halves: the position
        precessed to now, and the type of thing it is. Both are needed before a
        name may become a slew — the position so the governor has a place to
        validate, the type so a body that moves against the stars is refused
        rather than approved for an instant that has passed by the time the
        mount arrives.

        The instant KStars precessed to comes back with the position, and it is
        **KStars' clock**, not this process's. That is the point of carrying it:
        KStars' clock is simulated and can be stopped or moved, and a position
        precessed to the wrong instant looks exactly like a correct one.

        Raises:
            EkosError: if KStars answers with something that is not the document
                it documents.
        """
        position = await self._call(self._kstars_interface(), "getObjectPositionInfo", name)
        if not position or str(position).strip() == UNRESOLVED_OBJECT_XML:
            logger.info("KStars does not know %r", name, extra={"target": name})
            return None
        data = await self._call(self._kstars_interface(), "getObjectDataXML", name)
        fields = _object_fields(str(position), name) | {
            f"Data_{key}": value for key, value in _object_fields(str(data), name).items()
        }
        return ResolvedTarget(
            name=fields.get("Name", name),
            # RA_Degrees, not hours: KStars reports this document in degrees and
            # takes Mount.slew in hours. Converting here, at the one boundary
            # the two meet, is the whole reason this is not a straight copy.
            ra_hours=_field_float(fields, "RA_Degrees", name) / DEGREES_PER_HOUR,
            dec_deg=_field_float(fields, "Dec_Degrees", name),
            object_type=fields.get("Data_Type", ""),
            resolved_at=datetime_from_julian_date(_field_float(fields, "AltAz_JD", name)),
        )
