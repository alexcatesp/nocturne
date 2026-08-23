"""What the governor answers — SPEC section 9.

``Ok`` and ``Rejected`` live here rather than beside the governor because the
rules produce them and the governor consumes them, and a rule that had to
import the governor to say "no" would be a cycle. They are re-exported from
:mod:`nocturne.safety.governor` and from the package, which is where callers
should import them from.

Nothing here carries authority. An :class:`~nocturne.safety.governor.Approval`
— the thing the executor accepts — stays in the governor with the token that
mints it, because that is the one object whose provenance is the invariant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from nocturne.safety.commands import Command


class Decision:
    """Outcome of :meth:`SafetyGovernor.validate`. Either :class:`Ok` or :class:`Rejected`."""

    __slots__ = ()

    def __bool__(self) -> bool:
        raise NotImplementedError


@final
@dataclass(frozen=True)
class Ok(Decision):
    """The command is permitted."""

    command: Command

    def __bool__(self) -> bool:
        return True


@final
@dataclass(frozen=True)
class Rejected(Decision):
    """The command is refused, with the reason and the rule that refused it.

    ``reason`` is read by an operator on a phone, at night, on a terrace
    (CLAUDE.md section 9). It says what was refused, against which number, and
    where that number is set.
    """

    reason: str
    rule: str

    def __bool__(self) -> bool:
        return False


__all__ = ["Decision", "Ok", "Rejected"]
