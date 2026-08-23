"""The executor facade — SPEC section 3 (layers 1 and 2) and section 9.

Everything above this layer talks to the instrument through this object, and
this object performs nothing that the safety governor has not approved.

The invariant, in code rather than in prose: the only method that touches the
instrument is :meth:`Executor._perform`, it accepts an
:class:`~nocturne.safety.Approval` and nothing else, and only
:meth:`~nocturne.safety.SafetyGovernor.approve` can mint one. Every public
mutating method builds a command, hands it to the governor and passes on what
comes back. There is no argument, no keyword, and no subclass hook that skips
that step (CLAUDE.md invariant 1).

Reads are not commands. Looking at a cached property moves nothing, so it is
not gated; SPEC section 9 gates commands.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import final

from nocturne.executor.ekos import (
    GOTO_MODE_NOTHING,
    SOLVER_MODE_LOCAL,
    AlignSolution,
    EkosBridge,
    EkosError,
)
from nocturne.executor.indi.client import EventHandler, IndiClient
from nocturne.executor.indi.protocol import ElementValue, Property
from nocturne.safety import (
    AbortAlign,
    AbortMotion,
    Approval,
    Command,
    ConnectDevice,
    DisconnectDevice,
    MountReport,
    ParkMount,
    ResetMountModel,
    ResolvedTarget,
    SafetyGovernor,
    SafetyViolation,
    SetAlignTarget,
    SetMeridianFlipOffset,
    SetProperty,
    SlewToCoordinates,
    SlewToTarget,
    SolveHere,
    SyncToCoordinates,
    SyncToTarget,
    UnparkMount,
)
from nocturne.safety.sky import (
    SkyPosition,
    precess_date_to_j2000,
    precess_j2000_to_date,
)

logger = logging.getLogger("nocturne.executor")

#: What performing a command gives back. A property write returns the vector as
#: the driver echoed it; a mount command returns whether Ekos says it sent the
#: command on — which is not that the mount accepted it, and not that the motion
#: finished. Anything that waits for a slew to complete watches the mount's
#: coordinates, and that belongs to the session layer.
CommandResult = Property | bool | None


class UnknownTargetError(LookupError):
    """A target name KStars' catalogue does not contain.

    Not a :class:`~nocturne.safety.SafetyViolation`. Nothing unsafe happened —
    the operator or the agent asked for something that is not there — and
    dressing an ordinary miss up as a safety refusal makes the safety log
    useless for reading what the safety layer actually stopped.
    """


@final
class Executor:
    """The single door between Nocturne and the instrument."""

    def __init__(
        self,
        client: IndiClient,
        governor: SafetyGovernor,
        ekos: EkosBridge | None = None,
    ) -> None:
        self._client = client
        self._governor = governor
        self._ekos = ekos

    # ------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> Executor:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def start(self) -> None:
        """Connect to indiserver, and to Ekos if a bridge was supplied."""
        await self._client.connect()
        if self._ekos is not None:
            await self._ekos.connect()

    async def aclose(self) -> None:
        """Close every connection."""
        if self._ekos is not None:
            await self._ekos.aclose()
        await self._client.aclose()

    @property
    def governor(self) -> SafetyGovernor:
        """The governor every command passes through."""
        return self._governor

    # There is deliberately no accessor returning the INDI client or the Ekos
    # bridge. Both carry mutating methods that the governor does not see, so
    # handing one to a caller would be the second door CLAUDE.md invariant 1
    # says must not exist. Reads are proxied below; writes go through
    # _execute(). Enforced by tests/unit/test_safety_boundaries.py.

    def _mount(self) -> EkosBridge:
        """The bridge, when there is one to command the mount through.

        Deliberately not a property and deliberately private: it returns the
        object with the mutating calls on it, and the only caller is
        :meth:`_perform`, which has an approval in hand. Handing this to anyone
        else would be the second door.
        """
        if self._ekos is None:
            raise EkosError(
                "this executor was built without an Ekos bridge, so it cannot command "
                "the mount. Construct Executor(client, governor, ekos=EkosBridge(...)) "
                "and attach the module objects after Ekos is started."
            )
        return self._ekos

    @property
    def is_ekos_connected(self) -> bool:
        """Whether the Ekos bridge, if one was supplied, is attached to KStars."""
        return self._ekos is not None and self._ekos.is_connected

    # ----------------------------------------------------------------- reads

    def devices(self) -> tuple[str, ...]:
        """Devices that have defined at least one property."""
        return self._client.devices()

    def get_property(self, device: str, name: str) -> Property | None:
        """A property from the cache, or ``None``."""
        return self._client.get(device, name)

    def properties(self, device: str | None = None) -> Mapping[tuple[str, str], Property]:
        """The property cache, optionally narrowed to one device."""
        return self._client.properties(device)

    def is_device_connected(self, device: str) -> bool:
        """Whether ``device`` reports itself connected."""
        return self._client.is_device_connected(device)

    async def wait_for_property(
        self, device: str, name: str, *, timeout: float | None = None
    ) -> Property:
        """Wait until ``device`` has defined ``name``."""
        return await self._client.wait_for_property(device, name, timeout=timeout)

    async def wait_for_device(self, device: str, *, timeout: float | None = None) -> None:
        """Wait until ``device`` exists."""
        await self._client.wait_for_device(device, timeout=timeout)

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Watch transport events. Returns an unsubscribe callable.

        Listening is a read: an event says what happened, and a subscriber that
        wants to act on one comes back through the command methods above like
        everyone else. The callable returned unsubscribes; it carries no other
        capability.
        """
        return self._client.subscribe(handler)

    # -------------------------------------------------------------- commands

    async def connect_device(
        self, device: str, *, timeout: float | None = None
    ) -> Property | None:
        """Connect ``device``, if the governor permits it."""
        return await self._execute_property(ConnectDevice(device=device), timeout=timeout)

    async def disconnect_device(
        self, device: str, *, timeout: float | None = None
    ) -> Property | None:
        """Disconnect ``device``, if the governor permits it."""
        return await self._execute_property(DisconnectDevice(device=device), timeout=timeout)

    async def set_property(
        self,
        device: str,
        name: str,
        values: Mapping[str, ElementValue],
        *,
        timeout: float | None = None,
    ) -> Property | None:
        """Write ``values`` into ``device``'s ``name`` vector, if permitted."""
        command = SetProperty(device=device, property=name, values=dict(values))
        return await self._execute_property(command, timeout=timeout)

    # ---------------------------------------------------------- the mount

    # The whole of what Nocturne asks the mount to do, and each one is a command
    # object before it is a DBus call: the governor sees a SlewToCoordinates,
    # not a pair of floats already on their way to KStars. ADR 0023 records the
    # rule each one carries, and ADR 0022 — which it supersedes — records the
    # argument those rules had to answer.

    async def slew(
        self, ra_hours: float, dec_deg: float, *, timeout: float | None = None
    ) -> bool:
        """Slew the mount to an equatorial position, if the governor permits it.

        Right ascension in hours, declination in degrees, equinox of date — the
        units both the safety limits and Ekos are stated in.

        Returns whether Ekos reports the command was sent. It is not a promise
        that the mount arrived.

        Raises:
            SafetyViolation: if the altitude, Sun or meridian limits refuse this
                target, or the meridian limits are uncalibrated. Nothing is sent.
        """
        command = SlewToCoordinates(ra_hours=ra_hours, dec_deg=dec_deg)
        return await self._execute_mount(command, timeout=timeout)

    async def park(self, *, timeout: float | None = None) -> bool:
        """Drive the mount to its park position."""
        return await self._execute_mount(ParkMount(), timeout=timeout)

    async def unpark(self, *, timeout: float | None = None) -> bool:
        """Release the mount from its park position."""
        return await self._execute_mount(UnparkMount(), timeout=timeout)

    async def abort_motion(self, *, timeout: float | None = None) -> bool:
        """Stop the mount moving."""
        return await self._execute_mount(AbortMotion(), timeout=timeout)

    async def sync(
        self, ra_hours: float, dec_deg: float, *, timeout: float | None = None
    ) -> bool:
        """Tell the mount it is at this equatorial position, if permitted.

        This is how a plate solve corrects the mount's pointing. It moves
        nothing; it moves the mount's model of where it is, which is the frame
        every limit in SPEC section 9 is measured in — so the governor bounds
        how far, against ``limits.targeting.sync_tolerance_deg``.

        Reads the mount's current position first and sends it with the command:
        how far a sync moves the model is the question, and neither coordinate
        pair answers it alone.

        Raises:
            SafetyViolation: if the sync is larger than the tolerance, or either
                position is not a place on the sky. Nothing is sent.
        """
        command = SyncToCoordinates(
            ra_hours=ra_hours, dec_deg=dec_deg, reported=await self._mount_report()
        )
        return await self._execute_mount(command, timeout=timeout)

    async def slew_to_target(self, name: str, *, timeout: float | None = None) -> bool:
        """Slew to a target named rather than pointed at, if permitted.

        The name is resolved through KStars *before* the governor sees the
        command, so what is validated is a place on the sky and not a string.
        Every pointing limit then applies exactly as it does to
        :meth:`slew`, with two more in front of them: the object must hold still
        (SPEC section 1.2), and the resolution must be fresh.

        Raises:
            UnknownTargetError: if KStars' catalogue does not know the name.
            SafetyViolation: if the resolved position is refused. Nothing is sent.
        """
        command = SlewToTarget(target=await self._resolve(name))
        return await self._execute_mount(command, timeout=timeout)

    async def sync_to_target(self, name: str, *, timeout: float | None = None) -> bool:
        """Tell the mount it is pointing at a named target, if permitted.

        The rough startup alignment: point at something bright by eye, then say
        what it was. Carries the rules of a resolution and of a sync.

        Raises:
            UnknownTargetError: if KStars' catalogue does not know the name.
            SafetyViolation: if the resolution or the sync is refused.
        """
        command = SyncToTarget(
            target=await self._resolve(name), reported=await self._mount_report()
        )
        return await self._execute_mount(command, timeout=timeout)

    async def reset_mount_model(self, *, timeout: float | None = None) -> bool:
        """Discard the mount's alignment model, if permitted.

        Permitted only with the mount parked: parked, the position afterwards is
        one the driver knows without the model; tracking, it is nothing at all.

        Returns False when the mount had no alignment model to discard, which is
        "nothing to do" rather than a failure.

        Raises:
            SafetyViolation: if the mount is not parked. Nothing is sent.
        """
        command = ResetMountModel(reported=await self._mount_report())
        return await self._execute_mount(command, timeout=timeout)

    async def set_meridian_flip(
        self, *, enabled: bool, offset_deg: float = 0.0, timeout: float | None = None
    ) -> bool:
        """Configure Ekos' own automatic meridian flip, if permitted.

        ``offset_deg`` is the hour angle at which Ekos flips, in **degrees**,
        negative east of the meridian. Under ADR 0020 the keep-out band brackets
        the meridian, so a flip that is to happen before the tube enters it is a
        *negative* offset — which is the opposite of what Ekos' own default
        suggests, and the reason the governor checks rather than trusts.

        Raises:
            SafetyViolation: if Ekos would flip after the tube reaches the
                governor's keep-out band, or while the limits are uncalibrated.
        """
        command = SetMeridianFlipOffset(enabled=enabled, offset_deg=offset_deg)
        return await self._execute_mount(command, timeout=timeout)

    # ----------------------------------------------------------- the align

    async def solve(
        self, *, binning_index: int | None = None, timeout: float | None = None
    ) -> bool:
        """Expose a frame, plate-solve it, and touch nothing else.

        Returns whether the solve *started*. Whether it found anything is read
        from :meth:`align_status` and :meth:`solution`, because KStars returns
        true here when it is retrying.

        The mount does not move. Ekos acts on a solution in ``executeGOTO()``,
        which does nothing while the goto mode is "nothing" — and this sets that
        mode as part of the same command rather than once at startup, because
        Ekos changes it behind you: its polar-alignment path sets it, and
        ``loadAndSlew`` leaves it on "slew" (ADR 0024).

        What to do with the answer is a separate, governed decision:
        :meth:`sync` to correct the mount's model, :meth:`slew` to go somewhere.
        """
        return await self._execute_mount(
            SolveHere(binning_index=binning_index), timeout=timeout
        )

    async def set_align_target(
        self, ra_hours_j2000: float, dec_deg_j2000: float, *, timeout: float | None = None
    ) -> bool:
        """Set the coordinates the solver measures itself against, if permitted.

        Takes **J2000**, because that is what the align module takes. The
        governor works in the equinox of date, so this precesses first and the
        command carries the converted position — the conversion happens once,
        here, where the frames meet.

        Raises:
            SafetyViolation: if that place is one the limits forbid.
        """
        of_date = precess_j2000_to_date(
            SkyPosition(ra_hours=ra_hours_j2000, dec_deg=dec_deg_j2000), self._now()
        )
        command = SetAlignTarget(ra_hours=of_date.ra_hours, dec_deg=of_date.dec_deg)
        return await self._execute_mount(command, timeout=timeout)

    async def abort_align(self, *, timeout: float | None = None) -> bool:
        """Stop the plate solver."""
        return await self._execute_mount(AbortAlign(), timeout=timeout)

    # Reads. Solving produces an answer and reading it moves nothing.

    async def solution(self) -> AlignSolution | None:
        """The last plate solution, in J2000, or ``None`` if there is not one."""
        return await self._mount().align_solution()

    async def solution_of_date(self) -> SkyPosition | None:
        """The last plate solution in the frame the mount works in.

        The align module answers in J2000 and the mount points in the equinox of
        date — 0.3 degrees apart at this epoch. This is the form to hand to
        :meth:`sync`, and it exists so that nobody has to remember to precess.
        """
        solved = await self.solution()
        if solved is None:
            return None
        return precess_j2000_to_date(solved.position_j2000, self._now())

    async def align_status(self) -> int:
        """``Ekos::AlignState`` — see ``ALIGN_STATE_NAMES``."""
        return await self._mount().align_status()

    async def align_target(self) -> SkyPosition | None:
        """The target the solver is measuring against, in J2000."""
        return await self._mount().align_target_coords()

    async def align_camera_info(self) -> Sequence[float]:
        """Width, height and pixel size of the camera the solver is using."""
        return await self._mount().align_camera_info()

    async def align_telescope_info(self) -> Sequence[float]:
        """Focal length and aperture, as Ekos has them for the active train."""
        return await self._mount().align_telescope_info()

    # --------------------------------------------- what a command carries

    async def _mount_report(self) -> MountReport:
        """What the mount says about itself, read now, for the governor to judge.

        Read here rather than inside the governor, and passed *with* the command
        (ADR 0023): the governor stays a function of what it was handed, and the
        decision stays reproducible from its log line.
        """
        ra_hours, dec_deg = await self._mount().mount_equatorial_coords()
        return MountReport(
            ra_hours=ra_hours, dec_deg=dec_deg, parked=await self._mount().mount_is_parked()
        )

    async def _solve(self, binning_index: int | None) -> bool:
        """Configure the solver so that it moves nothing, then start it.

        The order is the safety property. ``setSolverAction(GOTO_NOTHING)``
        comes first and every time, because Ekos changes that mode on its own
        and a solve started under the wrong one moves the mount without the
        governor seeing a thing (ADR 0024).
        """
        bridge = self._mount()
        await bridge.align_set_solver_action(GOTO_MODE_NOTHING)
        await bridge.align_set_solver_mode(SOLVER_MODE_LOCAL)
        if binning_index is not None:
            await bridge.align_set_binning_index(binning_index)
        return await bridge.align_capture_and_solve()

    async def _set_align_target(self, ra_hours: float, dec_deg: float) -> bool:
        """Hand the align module a target, converted back to the frame it takes.

        The command carries the equinox of date, because that is the frame the
        governor validates in. ``setTargetCoords`` takes J2000. So this is the
        inverse of the conversion :meth:`set_align_target` did on the way in,
        and it is here rather than there so that what the governor approved and
        what KStars is told are the same position rather than two readings of
        one number.
        """
        j2000 = precess_date_to_j2000(
            SkyPosition(ra_hours=ra_hours, dec_deg=dec_deg), self._now()
        )
        await self._mount().align_set_target_coords(j2000.ra_hours, j2000.dec_deg)
        return True

    def _now(self) -> datetime:
        """The governor's clock, so the executor and the rules share an instant.

        A precession computed from the system clock while the governor validated
        against an injected one would make a test that freezes time meaningless,
        and would make a decision irreproducible from its log line for exactly
        the reason SPEC section 9 gives the governor a clock at all.
        """
        return self._governor.now()

    async def _resolve(self, name: str) -> ResolvedTarget:
        """``name`` as a place on the sky, or an error naming what was not found.

        Raises:
            UnknownTargetError: if KStars' catalogue has no such object. A name
                that resolves to nothing is not a safety refusal — there is
                nothing unsafe about it — so it is not one.
        """
        resolved = await self._mount().resolve_target(name)
        if resolved is None:
            raise UnknownTargetError(
                f"KStars' catalogue does not contain {name!r}. Check the spelling, or "
                "give the coordinates directly with slew()."
            )
        return resolved

    async def _execute_property(
        self, command: Command, *, timeout: float | None
    ) -> Property | None:
        """Perform a command whose result is the driver's echo of a vector.

        The narrowing is a runtime check rather than a cast because the thing it
        would be asserting is that :data:`COMMAND_RULES` and :meth:`_perform`
        agree about what kind of command this is. If they ever stop agreeing,
        saying so is worth more than a silent reinterpretation.
        """
        result = await self._execute(command, timeout=timeout)
        if isinstance(result, bool):
            raise SafetyViolation(
                f"{command.describe()} was performed as a mount command, which "
                "acknowledges rather than returning a property vector. The command "
                "registry and Executor._perform disagree about what this command is."
            )
        return result

    async def _execute_mount(self, command: Command, *, timeout: float | None) -> bool:
        """Perform a mount command and report whether Ekos says it was sent."""
        result = await self._execute(command, timeout=timeout)
        if not isinstance(result, bool):
            raise SafetyViolation(
                f"{command.describe()} was performed as a property write rather than "
                "through the Ekos mount module. The command registry and "
                "Executor._perform disagree about what this command is."
            )
        return result

    async def _execute(self, command: Command, *, timeout: float | None) -> CommandResult:
        """Validate ``command`` and perform it.

        Raises:
            SafetyViolation: if the governor rejects it. Nothing is sent.
        """
        approval = self._governor.approve(command)
        return await self._perform(approval, timeout=timeout)

    async def _perform(self, approval: Approval, *, timeout: float | None) -> CommandResult:
        """Carry out an approved command. The only method that moves anything.

        Raises:
            SafetyViolation: if handed anything but a governor-issued approval.
        """
        if not isinstance(approval, Approval):
            raise SafetyViolation(
                "the executor performs Approval objects only; a raw command has not "
                "been through the safety governor (SPEC section 9)"
            )
        command = approval.command
        logger.info(
            "executing approved command",
            extra={"command_type": type(command).__name__, "command": command.describe()},
        )
        match command:
            case ConnectDevice(device=device):
                await self._client.connect_device(device, timeout=timeout)
                return self._client.get(device, "CONNECTION")
            case DisconnectDevice(device=device):
                await self._client.disconnect_device(device, timeout=timeout)
                return self._client.get(device, "CONNECTION")
            case SetProperty(device=device, property=name, values=values):
                return await self._client.write(device, name, values, timeout=timeout)
            case SlewToCoordinates(ra_hours=ra_hours, dec_deg=dec_deg):
                return await self._mount().mount_slew(ra_hours, dec_deg)
            case ParkMount():
                return await self._mount().mount_park()
            case UnparkMount():
                return await self._mount().mount_unpark()
            case AbortMotion():
                return await self._mount().mount_abort()
            case SyncToCoordinates(ra_hours=ra_hours, dec_deg=dec_deg):
                return await self._mount().mount_sync(ra_hours, dec_deg)
            case SlewToTarget(target=target):
                return await self._mount().mount_goto_target(target.name)
            case SyncToTarget(target=target):
                return await self._mount().mount_sync_target(target.name)
            case ResetMountModel():
                return await self._mount().mount_reset_model()
            case SetMeridianFlipOffset(enabled=enabled, offset_deg=offset_deg):
                return await self._mount().mount_set_meridian_flip(enabled, offset_deg)
            case SolveHere(binning_index=binning_index):
                return await self._solve(binning_index)
            case SetAlignTarget(ra_hours=ra_hours, dec_deg=dec_deg):
                return await self._set_align_target(ra_hours, dec_deg)
            case AbortAlign():
                await self._mount().align_abort()
                return True
            case _:
                # The governor rejects unregistered command types, so reaching
                # here means the registry and this dispatch disagree.
                raise SafetyViolation(
                    f"the executor does not know how to perform {type(command).__name__}; "
                    "it is registered with the governor but not implemented here"
                )
