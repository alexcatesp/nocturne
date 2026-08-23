"""The mount commands, at the gate — SPEC sections 9.1 and 9.2, ADR 0022.

Until M2 the only way to move the mount was a raw property write, and the gate
that landed with the pointing rules was built around one command type. Driving
the Ekos mount module adds four more, and the question every one of them has to
answer is the same: *is this door narrower than, or exactly as narrow as, the
one already there?*

**The central test in this file is an equivalence.** For any position, any
instant and any configuration, a ``SlewToCoordinates`` must be decided exactly
as the ``SetProperty`` write that commands the same slew on a real EQMod mount —
same verdict, same rule. A new command type that reached the instrument under
conditions the old one could not is a second door, and CLAUDE.md invariant 1
says there is no second door. Asserting the two agree is stronger than
re-testing each limit against the new type, because it cannot go stale: a limit
added to ``POINTING_RULES`` later is covered by this the day it lands.

The three commands that name no place on the sky — park, unpark, abort — are
tested from the other direction: they must be permitted in the conditions that
refuse every slew, because two of them *are* the safe state (ADR 0022).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nocturne.safety import (
    AbortMotion,
    Command,
    Ok,
    ParkMount,
    Rejected,
    SafetyGovernor,
    SafetyViolation,
    SetProperty,
    SlewToCoordinates,
    UnparkMount,
)
from nocturne.safety.properties import PointingIntent, UnreadablePointing, pointing_intent
from nocturne.safety.sky import HOURS_PER_DAY, SkyPosition
from nocturne.schemas import SafetyConfig, Site, load_config_bundle
from tests.fixtures.observing import (
    DAY,
    NIGHT,
    TEST_SITE,
    calibrated_safety_config,
    target_at,
)

MOUNT = "EQMod Mount"
COORDINATES = "EQUATORIAL_EOD_COORD"

#: An hour angle well east of the band the fixture's limits forbid (-30 to +25),
#: at a declination inside the altitude window from the test site.
CLEAR_OF_THE_BAND = -60.0
USABLE_DECLINATION = 20.0


def property_slew(position: SkyPosition) -> SetProperty:
    """The raw property write that commands a slew on a real EQMod mount."""
    return SetProperty(
        device=MOUNT,
        property=COORDINATES,
        values={"RA": position.ra_hours, "DEC": position.dec_deg},
    )


def module_slew(position: SkyPosition) -> SlewToCoordinates:
    """The same slew, as the Ekos mount module is asked for it."""
    return SlewToCoordinates(ra_hours=position.ra_hours, dec_deg=position.dec_deg)


def governor_for(
    config: SafetyConfig, *, site: Site | None = TEST_SITE, when: datetime = NIGHT
) -> SafetyGovernor:
    return SafetyGovernor(config, site=site, clock=lambda: when)


def verdict(governor: SafetyGovernor, command: Command) -> tuple[bool, str]:
    """``(permitted, rule)`` — the whole of a decision, comparably."""
    decision = governor.validate(command)
    if isinstance(decision, Rejected):
        return False, decision.rule
    assert isinstance(decision, Ok), decision
    return True, ""


@pytest.fixture
def uncalibrated(config_dir: Path) -> SafetyConfig:
    return load_config_bundle(config_dir).safety


@pytest.fixture
def calibrated(config_dir: Path) -> SafetyConfig:
    return calibrated_safety_config(config_dir)


class TestASlewIsDecidedExactlyAsTheEquivalentPropertyWrite:
    """The new command type opens no door the old one did not (invariant 1)."""

    @settings(max_examples=250, deadline=None)
    @given(
        ra_hours=st.floats(min_value=0.0, max_value=23.999_999, allow_nan=False),
        dec_deg=st.floats(min_value=-90.0, max_value=90.0, allow_nan=False),
    )
    def test_anywhere_on_the_sky_the_two_agree(
        self, config_dir: Path, ra_hours: float, dec_deg: float
    ) -> None:
        position = SkyPosition(ra_hours=ra_hours, dec_deg=dec_deg)
        governor = governor_for(calibrated_safety_config(config_dir))
        assert verdict(governor, module_slew(position)) == verdict(
            governor, property_slew(position)
        )

    @pytest.mark.parametrize("when", [NIGHT, DAY])
    @pytest.mark.parametrize("hour_angle", [-60.0, -20.0, 0.0, 20.0, 60.0])
    @pytest.mark.parametrize("dec", [-80.0, 0.0, 20.0, 89.0])
    def test_they_agree_across_the_grid_the_limits_are_stated_on(
        self, calibrated: SafetyConfig, when: datetime, hour_angle: float, dec: float
    ) -> None:
        """The same claim in the quantities the rules are written in.

        The property test above samples right ascension, which is only a
        position relative to the meridian once an instant is fixed. This one
        walks hour angle and declination directly, in daylight and at night, so
        a divergence at a boundary shows up as a named case rather than as a
        shrunk float.
        """
        position = target_at(hour_angle, dec, when)
        governor = governor_for(calibrated, when=when)
        assert verdict(governor, module_slew(position)) == verdict(
            governor, property_slew(position)
        )

    def test_the_grid_contains_both_verdicts(self, calibrated: SafetyConfig) -> None:
        """The positive control for the agreement above.

        Two decisions agree trivially if everything is refused, or everything
        permitted. This asserts the grid the tests walk produces both, so the
        agreement is being demonstrated over a boundary and not over a constant.
        """
        governor = governor_for(calibrated)
        permitted = verdict(governor, module_slew(target_at(CLEAR_OF_THE_BAND, 20.0, NIGHT)))
        refused = verdict(governor, module_slew(target_at(0.0, 20.0, NIGHT)))
        assert permitted == (True, "")
        assert refused == (False, "meridian")

    def test_a_divergence_would_be_detected(self, calibrated: SafetyConfig) -> None:
        """The positive control for the comparison itself.

        A ``SlewToCoordinates`` that the extractor did not understand would
        classify as nothing to check, and sail through every limit while the
        property write beside it was refused. That is precisely the failure the
        equivalence exists to catch, so the comparison has to reject it.
        """
        inside_the_band = target_at(0.0, USABLE_DECLINATION, NIGHT)
        governor = governor_for(calibrated)

        real = verdict(governor, module_slew(inside_the_band))
        ungated = (True, "")

        assert real == verdict(governor, property_slew(inside_the_band))
        assert real != ungated


class TestNothingSlewsWhileTheLimitsAreUncalibrated:
    """ADR 0019, through the new door as well as the old one."""

    def test_a_module_slew_is_refused(self, uncalibrated: SafetyConfig) -> None:
        decision = governor_for(uncalibrated).validate(
            module_slew(target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT))
        )
        assert isinstance(decision, Rejected)
        assert decision.rule == "meridian"
        assert "meridian-calibration.md" in decision.reason

    def test_the_same_slew_is_permitted_once_calibrated(self, calibrated: SafetyConfig) -> None:
        """Without this the test above passes on a governor that refuses all."""
        decision = governor_for(calibrated).validate(
            module_slew(target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT))
        )
        assert isinstance(decision, Ok)

    def test_approve_raises_rather_than_returning(self, uncalibrated: SafetyConfig) -> None:
        with pytest.raises(SafetyViolation, match="meridian"):
            governor_for(uncalibrated).approve(
                module_slew(target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT))
            )


class TestCoordinatesThatAreNotAPositionAreRefusedNotRaised:
    """A nonsense slew must appear in the decision log, not in a traceback.

    ``SlewToCoordinates`` accepts any float, deliberately. Range-checking it in
    the model would raise before the governor saw the command, and a command
    that never reaches the gate never reaches the log either — which is the one
    place an operator can find out what was attempted.
    """

    @pytest.mark.parametrize(
        ("ra_hours", "dec_deg"),
        [
            (24.0, 0.0),
            (-0.1, 0.0),
            (99.0, 0.0),
            (0.0, 90.1),
            (0.0, -90.1),
            (float("inf"), 0.0),
            (float("nan"), 0.0),
            (0.0, float("nan")),
        ],
    )
    def test_it_is_rejected_with_a_reason(
        self, calibrated: SafetyConfig, ra_hours: float, dec_deg: float
    ) -> None:
        command = SlewToCoordinates(ra_hours=ra_hours, dec_deg=dec_deg)
        decision = governor_for(calibrated).validate(command)
        assert isinstance(decision, Rejected)
        assert decision.rule == "pointing_unreadable"

    def test_the_classifier_says_why(self) -> None:
        classification = pointing_intent(SlewToCoordinates(ra_hours=99.0, dec_deg=0.0))
        assert isinstance(classification, UnreadablePointing)
        assert "not a position on the sky" in classification.detail

    def test_a_position_on_the_sky_is_read_as_one(self) -> None:
        """The positive control: the same classifier, over a legal coordinate."""
        classification = pointing_intent(SlewToCoordinates(ra_hours=5.5, dec_deg=-12.25))
        assert isinstance(classification, PointingIntent)
        assert classification.position == SkyPosition(ra_hours=5.5, dec_deg=-12.25)

    @settings(max_examples=200, deadline=None)
    @given(
        ra_hours=st.floats(allow_nan=True, allow_infinity=True),
        dec_deg=st.floats(allow_nan=True, allow_infinity=True),
    )
    def test_no_float_pair_ever_raises(
        self, config_dir: Path, ra_hours: float, dec_deg: float
    ) -> None:
        """Whatever arrives, the governor answers. It does not blow up."""
        decision = governor_for(calibrated_safety_config(config_dir)).validate(
            SlewToCoordinates(ra_hours=ra_hours, dec_deg=dec_deg)
        )
        assert isinstance(decision, Ok | Rejected)

    @settings(max_examples=200, deadline=None)
    @given(
        ra_hours=st.floats(allow_nan=True, allow_infinity=True),
        dec_deg=st.floats(allow_nan=True, allow_infinity=True),
    )
    def test_anything_outside_the_sky_is_refused(
        self, config_dir: Path, ra_hours: float, dec_deg: float
    ) -> None:
        """Permission is only ever granted for a real position.

        Stated as an implication rather than as a list of bad inputs: if the
        governor said yes, the coordinates were inside the ranges
        ``SkyPosition`` accepts. There is no pair outside them that gets through.
        """
        decision = governor_for(calibrated_safety_config(config_dir)).validate(
            SlewToCoordinates(ra_hours=ra_hours, dec_deg=dec_deg)
        )
        if isinstance(decision, Ok):
            assert 0.0 <= ra_hours < HOURS_PER_DAY
            assert -90.0 <= dec_deg <= 90.0


#: The three commands that name no place on the sky. ADR 0022 explains each.
UNCONDITIONAL_COMMANDS: tuple[Command, ...] = (ParkMount(), UnparkMount(), AbortMotion())


class TestTheSafeStateIsAlwaysReachable:
    """Park, unpark and abort are permitted where every slew is refused.

    This is the property ADR 0022 turns on. SPEC section 8.5 degrades to
    parking, so parking has to work in exactly the conditions that are going
    badly: no site, no calibration, the Sun up, anything.
    """

    @pytest.mark.parametrize("command", UNCONDITIONAL_COMMANDS, ids=lambda c: c.describe())
    def test_permitted_while_uncalibrated(
        self, uncalibrated: SafetyConfig, command: Command
    ) -> None:
        assert isinstance(governor_for(uncalibrated).validate(command), Ok)

    @pytest.mark.parametrize("command", UNCONDITIONAL_COMMANDS, ids=lambda c: c.describe())
    def test_permitted_with_no_site_at_all(
        self, uncalibrated: SafetyConfig, command: Command
    ) -> None:
        assert isinstance(governor_for(uncalibrated, site=None).validate(command), Ok)

    @pytest.mark.parametrize("command", UNCONDITIONAL_COMMANDS, ids=lambda c: c.describe())
    def test_permitted_in_broad_daylight(
        self, calibrated: SafetyConfig, command: Command
    ) -> None:
        assert isinstance(governor_for(calibrated, when=DAY).validate(command), Ok)

    @pytest.mark.parametrize("command", UNCONDITIONAL_COMMANDS, ids=lambda c: c.describe())
    def test_each_still_passes_through_the_governor(
        self, calibrated: SafetyConfig, command: Command
    ) -> None:
        """Permitted is not the same as ungated. Every one mints an Approval."""
        approval = governor_for(calibrated).approve(command)
        assert approval.command is command

    def test_those_same_conditions_refuse_a_slew(
        self, uncalibrated: SafetyConfig, calibrated: SafetyConfig
    ) -> None:
        """The positive control: the conditions above are hostile ones.

        Without this, "park is permitted with no site" would read the same
        whether the governor was discriminating or simply permissive.
        """
        target = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        assert isinstance(governor_for(uncalibrated).validate(module_slew(target)), Rejected)
        assert isinstance(
            governor_for(uncalibrated, site=None).validate(module_slew(target)), Rejected
        )
        assert isinstance(
            governor_for(calibrated, when=DAY).validate(
                module_slew(target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, DAY))
            ),
            Rejected,
        )

    def test_none_of_them_is_classified_as_pointing(self) -> None:
        for command in UNCONDITIONAL_COMMANDS:
            assert pointing_intent(command) is None


class TestEveryMountCommandDescribesItselfUsefully:
    """A rejection is read on a phone, at night (CLAUDE.md section 9)."""

    def test_a_slew_names_the_coordinates(self) -> None:
        described = SlewToCoordinates(ra_hours=5.5, dec_deg=-12.25).describe()
        assert "5.5" in described
        assert "-12.25" in described

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            (ParkMount(), "park"),
            (UnparkMount(), "unpark"),
            (AbortMotion(), "abort"),
        ],
    )
    def test_the_others_name_what_they_do(self, command: Command, expected: str) -> None:
        assert expected in command.describe()

    def test_the_description_reaches_the_refusal(self, uncalibrated: SafetyConfig) -> None:
        with pytest.raises(SafetyViolation) as raised:
            governor_for(uncalibrated).approve(
                module_slew(target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT))
            )
        assert "slew to RA" in str(raised.value)
