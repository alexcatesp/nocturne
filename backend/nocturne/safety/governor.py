"""The safety governor — SPEC section 9.

Layer 2. Not bypassable. Every command passes through
``validate(command) -> Ok | Rejected(reason)``.

What is enforced here:

* An :class:`Approval` is the only thing the executor accepts, and only this
  module can mint one. There is no second door (CLAUDE.md invariant 1).
* A command type that is not in the registry is **rejected**, not passed
  through. Adding a command without registering it fails closed.
* ``supervised`` and ``autonomous`` are refused while the meridian limits are
  uncalibrated — a hard failure with an explicit message, never a warning and
  never a default-to-permissive (SPEC section 9.1.2, CLAUDE.md invariant 3).
* Every command that points the instrument is checked against the altitude
  floor and ceiling, the Sun, and the meridian hour angle
  (:mod:`nocturne.safety.rules`, SPEC sections 9.1 and 9.2). While the meridian
  limits are uncalibrated, pointing is refused outright — at every autonomy
  level, not only the unattended ones (ADR 0019).

**Two things the governor is given rather than takes.** The observing site,
because altitude and hour angle are meaningless without it; and the clock,
because a decision that cannot be replayed cannot be audited. The clock is read
**once per decision**, so every rule in one validation sees the same instant.

The governor never mutates its configuration and exposes no way to do so. The
limits of SPEC section 9.2 that are not about pointing — TEC ramp rate, disk
floor, the communication watchdog — are properties of running state rather than
of a command, and belong to the session layer that will hold that state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass
from datetime import UTC, datetime
from typing import Final, final

from nocturne.safety.commands import (
    AbortAlign,
    AbortMotion,
    Command,
    ConnectDevice,
    DisconnectDevice,
    ParkMount,
    ResetMountModel,
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
from nocturne.safety.decisions import Decision as Decision
from nocturne.safety.decisions import Ok as Ok
from nocturne.safety.decisions import Rejected as Rejected
from nocturne.safety.rules import (
    FLIP_RULES,
    MODEL_RULES,
    POINTING_RULES,
    RESOLVED_POINTING_RULES,
    RESOLVED_SYNC_RULES,
    SYNC_RULES,
    MeasuredBand,
    Rule,
    RuleContext,
)
from nocturne.schemas import AUTONOMY_LEVELS, AutonomyLevel, SafetyConfig, Site

logger = logging.getLogger("nocturne.safety")

#: Autonomy levels that run without a human watching, and are therefore gated on
#: the meridian calibration (SPEC section 8.4 and section 9.1.2).
UNATTENDED_LEVELS: Final[frozenset[str]] = frozenset({"supervised", "autonomous"})

#: The highest level permitted while the meridian limits are uncalibrated.
MAX_UNCALIBRATED_LEVEL: Final[AutonomyLevel] = "advisory"


class SafetyViolation(Exception):
    """A safety rule was broken, or an unsafe operation was attempted.

    Raised rather than returned when the caller has no meaningful way to
    continue: forging an approval, or requesting an autonomy level the rig is
    not calibrated for. Never caught to proceed anyway.
    """


#: Module-private sentinel. Possession of this object is what distinguishes an
#: approval minted by the governor from one a caller tried to build itself.
_APPROVAL_TOKEN: Final = object()


@final
@dataclass(frozen=True)
class Approval:
    """Proof that a command passed :meth:`SafetyGovernor.validate`.

    The executor's mutating API accepts nothing else. Only
    :meth:`SafetyGovernor.approve` can construct one; attempting to build it
    directly raises :class:`SafetyViolation`.

    ``token`` is an :class:`~dataclasses.InitVar`: it is checked at construction
    and never stored, so holding an approval does not hand the caller the means
    to mint another one.
    """

    command: Command
    token: InitVar[object]

    def __post_init__(self, token: object) -> None:
        if token is not _APPROVAL_TOKEN:
            raise SafetyViolation(
                "An Approval can only be issued by the safety governor. Call "
                "SafetyGovernor.approve(command); do not construct Approval directly "
                "(SPEC section 9, CLAUDE.md invariant 1)."
            )


#: A clock returns the current instant, timezone-aware. Injected so that every
#: decision is reproducible from its log line, and so that M3's disciplined time
#: (docs/FIELD-NOTES-TIMING.md) can replace the system one without touching a
#: rule.
Clock = Callable[[], datetime]


def _system_clock() -> datetime:
    """The default clock. The only place in this package that reads the wall clock."""
    return datetime.now(UTC)


#: Every command type the governor knows about, with the rules that constrain it.
#: A command type absent from this mapping is rejected — adding a command in a
#: later milestone without deciding which limits apply to it fails closed.
#:
#: ``ConnectDevice`` and ``DisconnectDevice`` carry no rules and that is a
#: decision, not an omission: connecting a driver commands no motion, and what a
#: driver does on connect is bounded at bring-up by
#: :class:`~nocturne.executor.mount.MountLink`. ``ParkMount``, ``UnparkMount``
#: and ``AbortMotion`` carry none either, and that decision is ADR 0022, kept by
#: ADR 0023 when every other call on the mount interface gained one: two of them
#: *are* the safe state, and none of the three names a place on the sky.
#: ``SetProperty`` and ``SlewToCoordinates`` carry the pointing rules — the
#: former applies them only to the writes that point
#: (:mod:`nocturne.safety.properties`), the latter to every one of them.
#:
#: The rest of the mount's surface carries rules of its own rather than the
#: pointing ones, because none of it names a destination and each is dangerous
#: in a different way (ADR 0023): a sync edits the frame the limits are measured
#: in, a model reset discards it, a named target hands over a string in place of
#: a coordinate, and Ekos' own flip is a second thing deciding when to cross the
#: meridian. **Every one of them is bounded. None is waved through.**
COMMAND_RULES: Final[Mapping[type[Command], tuple[Rule, ...]]] = {
    ConnectDevice: (),
    DisconnectDevice: (),
    SetProperty: POINTING_RULES,
    SlewToCoordinates: POINTING_RULES,
    SlewToTarget: RESOLVED_POINTING_RULES,
    SyncToCoordinates: SYNC_RULES,
    SyncToTarget: RESOLVED_SYNC_RULES,
    ResetMountModel: MODEL_RULES,
    SetMeridianFlipOffset: FLIP_RULES,
    ParkMount: (),
    UnparkMount: (),
    AbortMotion: (),
    # The align module. Solving is a read — of the sky, through the camera —
    # and it is one because SolveHere sets the goto mode that makes it one
    # (ADR 0024). SetAlignTarget names a place, so it is checked like any other
    # place even though nothing goes there.
    SolveHere: (),
    SetAlignTarget: POINTING_RULES,
    AbortAlign: (),
}


@final
class SafetyGovernor:
    """Validates every command before it reaches the executor."""

    def __init__(
        self,
        config: SafetyConfig,
        *,
        site: Site | None = None,
        clock: Clock = _system_clock,
        measured: MeasuredBand | None = None,
    ) -> None:
        """Build a governor.

        Args:
            config: the validated ``safety.yaml`` bundle. Never mutated.
            site: where the instrument stands, from ``equipment.yaml``. Without
                it — or with the shipped placeholder — **every pointing command
                is refused**, because altitude and hour angle computed for the
                wrong place do not fail, they simply point somewhere else. A
                governor built without a site still validates everything that
                moves nothing, which is what the M1 bring-up path needs.
            clock: returns the current instant, timezone-aware. Read once per
                decision.
            measured: the meridian calibration measured on this rig, if the
                store holds one that applies to this site (ADR 0021). Resolved
                by the caller **once, at startup**, and never re-read per
                command: a governor is a function of what it was built with, and
                a limit that can change under a running session is not a limit.
        """
        self._config = config
        self._site = site
        self._clock = clock
        self._measured = measured

    @property
    def config(self) -> SafetyConfig:
        """The safety configuration. Frozen; there is no setter, by design."""
        return self._config

    @property
    def site(self) -> Site | None:
        """Where the instrument stands, or ``None``. Read-only, like the config."""
        return self._site

    @property
    def measured_band(self) -> MeasuredBand | None:
        """The measured meridian calibration in force, if any. Read-only."""
        return self._measured

    @property
    def max_autonomy_level(self) -> AutonomyLevel:
        """Highest autonomy level this configuration permits (SPEC section 9.1.2)."""
        if self._config.limits.meridian.calibrated:
            return AUTONOMY_LEVELS[-1]
        return MAX_UNCALIBRATED_LEVEL

    def now(self) -> datetime:
        """The instant this governor's clock reads. A read, not a command.

        Exposed so that the executor can share it. Anything that converts
        between frames — a J2000 target into the equinox of date, say — is a
        function of time, and a conversion computed from the system clock while
        the rules were validated against an injected one would make a frozen
        clock meaningless and a decision irreproducible from its log line.
        """
        return self._clock()

    def validate(self, command: Command) -> Decision:
        """Validate ``command`` — SPEC section 9.

        Returns :class:`Ok` or :class:`Rejected`. Never raises for an unsafe
        command; rejection is a value so that callers must handle it.
        """
        rules = COMMAND_RULES.get(type(command))
        # One clock read per decision: two rules that each asked would disagree
        # about where the sky is, by however long the first one took.
        context = RuleContext(
            config=self._config,
            site=self._site,
            when=self._clock(),
            measured=self._measured,
        )
        if rules is None:
            decision: Decision = Rejected(
                reason=(
                    f"{type(command).__name__} is not registered with the safety "
                    "governor, so no limits are known to apply to it. Register it in "
                    "COMMAND_RULES with the rules that constrain it."
                ),
                rule="command_registry",
            )
        else:
            decision = Ok(command=command)
            for rule in rules:
                rejection = rule(command, context)
                if rejection is not None:
                    decision = rejection
                    break

        self._log(command, decision)
        return decision

    def approve(self, command: Command) -> Approval:
        """Validate ``command`` and mint an :class:`Approval` for the executor.

        Raises:
            SafetyViolation: if the command is rejected.
        """
        decision = self.validate(command)
        if isinstance(decision, Rejected):
            raise SafetyViolation(
                f"Refused by the safety governor [{decision.rule}]: {decision.reason} "
                f"(command: {command.describe()})"
            )
        return Approval(command=command, token=_APPROVAL_TOKEN)

    def require_autonomy_level(self, level: str) -> None:
        """Assert that ``level`` is permitted, or raise.

        SPEC section 9.1.2: the system must refuse ``supervised`` and
        ``autonomous`` while ``meridian.calibrated`` is false. Hard failure with
        an explicit message — not a warning, and not a silent demotion.

        Raises:
            SafetyViolation: if the level is unknown or not permitted.
        """
        if level not in AUTONOMY_LEVELS:
            raise SafetyViolation(
                f"Unknown autonomy level {level!r}. Valid levels: "
                f"{', '.join(AUTONOMY_LEVELS)} (SPEC section 8.4)."
            )
        if level in UNATTENDED_LEVELS and not self._config.limits.meridian.calibrated:
            raise SafetyViolation(
                f"Autonomy level {level!r} is refused: the meridian limits for this "
                "mount and tripod have not been calibrated, so the governor cannot "
                "know at which hour angle the OTA strikes the tripod legs. Complete "
                "the procedure in docs/meridian-calibration.md and record the measured "
                "limits in config/safety.yaml. The highest level available until then "
                f"is {MAX_UNCALIBRATED_LEVEL!r} (SPEC section 9.1)."
            )
        logger.info(
            "autonomy level %s permitted",
            level,
            extra={
                "autonomy_level": level,
                "meridian_calibrated": self._config.limits.meridian.calibrated,
            },
        )

    def _log(self, command: Command, decision: Decision) -> None:
        if isinstance(decision, Rejected):
            logger.warning(
                "safety governor REJECTED %s: %s",
                command.describe(),
                decision.reason,
                extra={
                    "command_type": type(command).__name__,
                    "command": command.describe(),
                    "decision": "rejected",
                    "rule": decision.rule,
                    "reason": decision.reason,
                },
            )
        else:
            logger.info(
                "safety governor approved %s",
                command.describe(),
                extra={
                    "command_type": type(command).__name__,
                    "command": command.describe(),
                    "decision": "ok",
                },
            )
