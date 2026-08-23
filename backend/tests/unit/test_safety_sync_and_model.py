"""The five calls ADR 0022 refused, and the rules that let them back — ADR 0023.

A sync, a model reset, a target named rather than pointed at, and Ekos' own
meridian flip. None of them names a destination, so none is covered by the
pointing rules, and ADR 0022 refused all five rather than approve something it
could not measure. The operator's decision was to implement them, which made the
question "what *can* be measured about each" — and the answers are the four rule
tuples this file exercises.

Each one is tested from both directions. A rule that only ever refuses is
indistinguishable from a rule that refuses everything, and a rule that only ever
permits is indistinguishable from no rule at all, so every class here asserts a
refusal *and* the nearest permitted case beside it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nocturne.safety import (
    MOVING_OBJECT_TYPES,
    Command,
    MountReport,
    Ok,
    Rejected,
    ResetMountModel,
    ResolvedTarget,
    SafetyGovernor,
    SetMeridianFlipOffset,
    SlewToCoordinates,
    SlewToTarget,
    SyncToCoordinates,
    SyncToTarget,
)
from nocturne.safety.sky import SkyPosition, angular_separation_deg
from nocturne.schemas import SafetyConfig, Site, load_config_bundle
from tests.fixtures.observing import (
    EAST_LIMIT_DEG,
    NIGHT,
    TEST_SITE,
    calibrated_safety_config,
    target_at,
)

#: Clear of the forbidden band and inside the altitude window at NIGHT.
CLEAR_OF_THE_BAND = -60.0
USABLE_DECLINATION = 20.0

#: The shipped tolerance, so a test can say "just inside" and "just outside"
#: without either becoming a number nobody can trace back to the file.
SHIPPED_SYNC_TOLERANCE_DEG = 5.0


def governor_for(
    config: SafetyConfig, *, site: Site | None = TEST_SITE, when: datetime = NIGHT
) -> SafetyGovernor:
    return SafetyGovernor(config, site=site, clock=lambda: when)


@pytest.fixture
def calibrated(config_dir: Path) -> SafetyConfig:
    return calibrated_safety_config(config_dir)


@pytest.fixture
def uncalibrated(config_dir: Path) -> SafetyConfig:
    return load_config_bundle(config_dir).safety


def at(position: SkyPosition, *, parked: bool = False) -> MountReport:
    """A mount reporting itself at ``position``."""
    return MountReport(ra_hours=position.ra_hours, dec_deg=position.dec_deg, parked=parked)


def offset_by(position: SkyPosition, *, degrees: float) -> SkyPosition:
    """``position`` moved ``degrees`` north, or south if that would leave the sky.

    Moving in declination is deliberate: a degree of declination is a degree of
    arc wherever you are, whereas a degree of right ascension is a degree only
    on the equator. A test written in right ascension would be measuring a
    different separation at every declination, which is exactly the mistake the
    tolerance rule exists to be robust against.
    """
    moved = position.dec_deg + degrees
    if moved > 90.0:
        moved = position.dec_deg - degrees
    return SkyPosition(ra_hours=position.ra_hours, dec_deg=moved)


def resolved(
    position: SkyPosition,
    *,
    name: str = "M 31",
    object_type: str = "Galaxy",
    when: datetime = NIGHT,
) -> ResolvedTarget:
    return ResolvedTarget(
        name=name,
        ra_hours=position.ra_hours,
        dec_deg=position.dec_deg,
        object_type=object_type,
        resolved_at=when,
    )


class TestASyncMayCorrectTheModelAndNotReplaceIt:
    """SPEC section 9.1 is stated in a frame. A sync edits the frame."""

    def test_a_small_sync_is_permitted(self, calibrated: SafetyConfig) -> None:
        where_it_is = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        solved = offset_by(where_it_is, degrees=1.0)
        decision = governor_for(calibrated).validate(
            SyncToCoordinates(
                ra_hours=solved.ra_hours, dec_deg=solved.dec_deg, reported=at(where_it_is)
            )
        )
        assert isinstance(decision, Ok)

    def test_a_large_sync_is_refused(self, calibrated: SafetyConfig) -> None:
        where_it_is = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        solved = offset_by(where_it_is, degrees=SHIPPED_SYNC_TOLERANCE_DEG + 1.0)
        decision = governor_for(calibrated).validate(
            SyncToCoordinates(
                ra_hours=solved.ra_hours, dec_deg=solved.dec_deg, reported=at(where_it_is)
            )
        )
        assert isinstance(decision, Rejected)
        assert decision.rule == "sync_tolerance"

    def test_the_two_cases_differ_only_in_how_far(self, calibrated: SafetyConfig) -> None:
        """The precondition the pair above rests on.

        If the permitted and refused cases differed in anything else — a
        declination that crossed the altitude floor, say — the pair would prove
        nothing about the tolerance.
        """
        where_it_is = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        near = offset_by(where_it_is, degrees=1.0)
        far = offset_by(where_it_is, degrees=SHIPPED_SYNC_TOLERANCE_DEG + 1.0)
        assert angular_separation_deg(where_it_is, near) < SHIPPED_SYNC_TOLERANCE_DEG
        assert angular_separation_deg(where_it_is, far) > SHIPPED_SYNC_TOLERANCE_DEG

    def test_the_refusal_says_how_far_and_against_what(self, calibrated: SafetyConfig) -> None:
        where_it_is = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        solved = offset_by(where_it_is, degrees=30.0)
        decision = governor_for(calibrated).validate(
            SyncToCoordinates(
                ra_hours=solved.ra_hours, dec_deg=solved.dec_deg, reported=at(where_it_is)
            )
        )
        assert isinstance(decision, Rejected)
        assert "30" in decision.reason
        assert "safety.yaml" in decision.reason

    def test_the_tolerance_is_measured_as_an_angle_not_a_coordinate_difference(
        self, calibrated: SafetyConfig
    ) -> None:
        """Near the pole a large difference in right ascension is a small angle.

        A rule written on the raw numbers would refuse this, which would make
        syncing anywhere near Polaris impossible for no reason. The separation
        is the thing that matters and the separation is what is measured.
        """
        near_the_pole = SkyPosition(ra_hours=2.0, dec_deg=89.5)
        far_in_ra = SkyPosition(ra_hours=14.0, dec_deg=89.5)
        assert angular_separation_deg(near_the_pole, far_in_ra) < SHIPPED_SYNC_TOLERANCE_DEG
        decision = governor_for(calibrated).validate(
            SyncToCoordinates(
                ra_hours=far_in_ra.ra_hours,
                dec_deg=far_in_ra.dec_deg,
                reported=at(near_the_pole),
            )
        )
        assert isinstance(decision, Ok)

    def test_a_sync_is_not_refused_for_where_it_points(self, calibrated: SafetyConfig) -> None:
        """A sync reports where the instrument *is*; ADR 0023 argues at length
        that refusing to hear it leaves the governor with a frame it already
        knows to be wrong. So a small sync inside the meridian band passes,
        where the identical slew would be refused.
        """
        inside_the_band = target_at(0.0, USABLE_DECLINATION, NIGHT)
        solved = offset_by(inside_the_band, degrees=0.5)
        governor = governor_for(calibrated)

        sync = governor.validate(
            SyncToCoordinates(
                ra_hours=solved.ra_hours,
                dec_deg=solved.dec_deg,
                reported=at(inside_the_band),
            )
        )
        slew = governor.validate(
            SlewToCoordinates(ra_hours=solved.ra_hours, dec_deg=solved.dec_deg)
        )
        assert isinstance(sync, Ok)
        assert isinstance(slew, Rejected)
        assert slew.rule == "meridian"

    def test_a_sync_to_nowhere_is_refused(self, calibrated: SafetyConfig) -> None:
        decision = governor_for(calibrated).validate(
            SyncToCoordinates(
                ra_hours=99.0,
                dec_deg=0.0,
                reported=at(target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)),
            )
        )
        assert isinstance(decision, Rejected)
        assert decision.rule == "pointing_unreadable"

    def test_a_mount_reporting_nonsense_is_refused(self, calibrated: SafetyConfig) -> None:
        """Not defaulted, not ignored. If the governor cannot tell how far the
        sync moves the model, it does not permit the sync."""
        solved = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        decision = governor_for(calibrated).validate(
            SyncToCoordinates(
                ra_hours=solved.ra_hours,
                dec_deg=solved.dec_deg,
                reported=MountReport(ra_hours=float("nan"), dec_deg=0.0, parked=False),
            )
        )
        assert isinstance(decision, Rejected)
        assert decision.rule == "pointing_unreadable"

    @settings(max_examples=200, deadline=None)
    @given(
        ra_hours=st.floats(min_value=0.0, max_value=23.999, allow_nan=False),
        dec_deg=st.floats(min_value=-89.0, max_value=89.0, allow_nan=False),
        moved=st.floats(min_value=0.0, max_value=90.0, allow_nan=False),
    )
    def test_nothing_approved_ever_moves_the_model_past_the_tolerance(
        self, config_dir: Path, ra_hours: float, dec_deg: float, moved: float
    ) -> None:
        """The property, stated as an implication: approval implies inside.

        Whatever the rule does, and whatever it does later, an ``Ok`` on a sync
        means the model moved by no more than the configured tolerance.
        """
        config = calibrated_safety_config(config_dir)
        where_it_is = SkyPosition(ra_hours=ra_hours, dec_deg=dec_deg)
        solved = offset_by(where_it_is, degrees=moved)
        decision = governor_for(config).validate(
            SyncToCoordinates(
                ra_hours=solved.ra_hours, dec_deg=solved.dec_deg, reported=at(where_it_is)
            )
        )
        if isinstance(decision, Ok):
            separation = angular_separation_deg(where_it_is, solved)
            assert separation <= config.limits.targeting.sync_tolerance_deg


class TestNothingDiscardsTheAlignmentModelWhileTheMountIsLive:
    def test_a_reset_is_refused_while_tracking(self, calibrated: SafetyConfig) -> None:
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        decision = governor_for(calibrated).validate(
            ResetMountModel(reported=at(here, parked=False))
        )
        assert isinstance(decision, Rejected)
        assert decision.rule == "requires_park"

    def test_a_reset_is_permitted_when_parked(self, calibrated: SafetyConfig) -> None:
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        decision = governor_for(calibrated).validate(
            ResetMountModel(reported=at(here, parked=True))
        )
        assert isinstance(decision, Ok)

    def test_it_is_permitted_parked_even_uncalibrated(self, uncalibrated: SafetyConfig) -> None:
        """A reset names no place on the sky, so the meridian calibration has
        nothing to say about it. Requiring one would make a rig impossible to
        put right before it has been measured."""
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        decision = governor_for(uncalibrated).validate(
            ResetMountModel(reported=at(here, parked=True))
        )
        assert isinstance(decision, Ok)

    def test_the_refusal_says_to_park_first(self, calibrated: SafetyConfig) -> None:
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        decision = governor_for(calibrated).validate(ResetMountModel(reported=at(here)))
        assert isinstance(decision, Rejected)
        assert "Park first" in decision.reason

    def test_a_sync_is_not_caught_by_the_park_rule(self, calibrated: SafetyConfig) -> None:
        """The two share a ``reported`` field and must not share a rule.

        Requiring a parked mount for a sync would make plate-solve alignment
        impossible, which is the whole of M2. This is the case that would break
        if the extractor stopped telling them apart.
        """
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        solved = offset_by(here, degrees=1.0)
        decision = governor_for(calibrated).validate(
            SyncToCoordinates(
                ra_hours=solved.ra_hours,
                dec_deg=solved.dec_deg,
                reported=at(here, parked=False),
            )
        )
        assert isinstance(decision, Ok)


class TestANamedTargetIsResolvedBeforeItIsJudged:
    def test_a_resolved_target_is_decided_as_its_coordinates_are(
        self, calibrated: SafetyConfig
    ) -> None:
        """The equivalence, again: a name is not a way round the limits."""
        governor = governor_for(calibrated)
        for hour_angle, expected_ok in ((CLEAR_OF_THE_BAND, True), (0.0, False)):
            position = target_at(hour_angle, USABLE_DECLINATION, NIGHT)
            by_name = governor.validate(SlewToTarget(target=resolved(position)))
            by_coordinates = governor.validate(
                SlewToCoordinates(ra_hours=position.ra_hours, dec_deg=position.dec_deg)
            )
            assert isinstance(by_name, Ok) is expected_ok
            assert isinstance(by_name, Ok) == isinstance(by_coordinates, Ok)

    @pytest.mark.parametrize("object_type", sorted(MOVING_OBJECT_TYPES))
    def test_a_target_that_moves_is_refused(
        self, calibrated: SafetyConfig, object_type: str
    ) -> None:
        position = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        decision = governor_for(calibrated).validate(
            SlewToTarget(target=resolved(position, name="Jupiter", object_type=object_type))
        )
        assert isinstance(decision, Rejected)
        assert decision.rule == "moving_target"

    def test_the_same_position_is_permitted_for_a_galaxy(
        self, calibrated: SafetyConfig
    ) -> None:
        """Positive control: it is the type that refuses it, not the place."""
        position = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        decision = governor_for(calibrated).validate(
            SlewToTarget(target=resolved(position, object_type="Galaxy"))
        )
        assert isinstance(decision, Ok)

    def test_the_moving_list_is_not_empty(self) -> None:
        """A rule that checks membership of an empty set refuses nothing."""
        assert len(MOVING_OBJECT_TYPES) >= 5
        assert "Planet" in MOVING_OBJECT_TYPES

    def test_a_stale_resolution_is_refused(self, calibrated: SafetyConfig) -> None:
        """KStars' clock is simulated. This is the only place that shows."""
        position = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        stale = resolved(position, when=NIGHT - timedelta(hours=3))
        decision = governor_for(calibrated).validate(SlewToTarget(target=stale))
        assert isinstance(decision, Rejected)
        assert decision.rule == "resolver_clock"

    def test_a_resolution_from_the_future_is_refused_too(
        self, calibrated: SafetyConfig
    ) -> None:
        """The skew is an absolute value. A clock set forward is as wrong."""
        position = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        ahead = resolved(position, when=NIGHT + timedelta(hours=3))
        decision = governor_for(calibrated).validate(SlewToTarget(target=ahead))
        assert isinstance(decision, Rejected)
        assert decision.rule == "resolver_clock"

    def test_a_resolution_inside_the_skew_is_permitted(self, calibrated: SafetyConfig) -> None:
        position = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        limit = calibrated.limits.targeting.resolver_max_clock_skew_s
        fresh = resolved(position, when=NIGHT + timedelta(seconds=limit / 2))
        assert isinstance(governor_for(calibrated).validate(SlewToTarget(target=fresh)), Ok)

    def test_a_resolution_with_no_timezone_is_refused(self, calibrated: SafetyConfig) -> None:
        """A naive timestamp does not say which instant it is, and the skew
        computed from one is a subtraction of two different things."""
        position = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        naive = resolved(position, when=NIGHT.replace(tzinfo=None))
        decision = governor_for(calibrated).validate(SlewToTarget(target=naive))
        assert isinstance(decision, Rejected)
        assert decision.rule == "resolver_clock"

    def test_the_resolution_checks_come_before_the_pointing_ones(
        self, calibrated: SafetyConfig
    ) -> None:
        """Order matters, and the reason is legibility rather than safety.

        A stale resolution for a target below the horizon is refused for being
        stale: the coordinates are meaningless, so measuring them against the
        altitude floor and reporting *that* would send the operator to fix the
        wrong thing.
        """
        below = target_at(CLEAR_OF_THE_BAND, -70.0, NIGHT)
        stale = resolved(below, when=NIGHT - timedelta(hours=3))
        decision = governor_for(calibrated).validate(SlewToTarget(target=stale))
        assert isinstance(decision, Rejected)
        assert decision.rule == "resolver_clock"

    def test_a_named_sync_carries_both_sets_of_rules(self, calibrated: SafetyConfig) -> None:
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        far = offset_by(here, degrees=40.0)
        governor = governor_for(calibrated)

        too_far = governor.validate(SyncToTarget(target=resolved(far), reported=at(here)))
        assert isinstance(too_far, Rejected)
        assert too_far.rule == "sync_tolerance"

        moving = governor.validate(
            SyncToTarget(
                target=resolved(offset_by(here, degrees=1.0), object_type="Planet"),
                reported=at(here),
            )
        )
        assert isinstance(moving, Rejected)
        assert moving.rule == "moving_target"

    def test_a_named_sync_that_breaks_neither_is_permitted(
        self, calibrated: SafetyConfig
    ) -> None:
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        decision = governor_for(calibrated).validate(
            SyncToTarget(target=resolved(offset_by(here, degrees=1.0)), reported=at(here))
        )
        assert isinstance(decision, Ok)


class TestEkosMayFlipEarlierThanTheGovernorNeverLater:
    """The rule that makes handing the flip to Ekos safe at all.

    Under ADR 0020 the keep-out band brackets the meridian, so its east edge is
    a *negative* hour angle: the tube enters the forbidden region before
    culmination. Ekos' offset must therefore be negative too — earlier than the
    meridian, not later — which is the opposite of what its default suggests.
    """

    def test_a_flip_at_the_band_edge_is_permitted(self, calibrated: SafetyConfig) -> None:
        margin = calibrated.limits.meridian.safety_margin_deg
        east_edge = EAST_LIMIT_DEG - margin
        decision = governor_for(calibrated).validate(
            SetMeridianFlipOffset(enabled=True, offset_deg=east_edge)
        )
        assert isinstance(decision, Ok)

    def test_a_flip_earlier_than_the_band_is_permitted(self, calibrated: SafetyConfig) -> None:
        margin = calibrated.limits.meridian.safety_margin_deg
        decision = governor_for(calibrated).validate(
            SetMeridianFlipOffset(enabled=True, offset_deg=EAST_LIMIT_DEG - margin - 10.0)
        )
        assert isinstance(decision, Ok)

    def test_a_flip_after_the_band_edge_is_refused(self, calibrated: SafetyConfig) -> None:
        margin = calibrated.limits.meridian.safety_margin_deg
        decision = governor_for(calibrated).validate(
            SetMeridianFlipOffset(enabled=True, offset_deg=EAST_LIMIT_DEG - margin + 0.5)
        )
        assert isinstance(decision, Rejected)
        assert decision.rule == "flip_offset"

    def test_ekos_own_default_shape_is_refused(self, calibrated: SafetyConfig) -> None:
        """The case this rule exists for, named rather than left to a boundary.

        Ekos is configured in "degrees past the meridian", and every example of
        it in the wild is a small positive number. On this rig that is
        catastrophically late: the tube is inside the band from -30 degrees.
        """
        decision = governor_for(calibrated).validate(
            SetMeridianFlipOffset(enabled=True, offset_deg=1.0)
        )
        assert isinstance(decision, Rejected)
        assert decision.rule == "flip_offset"
        assert "-30" in decision.reason

    def test_disabling_it_is_always_permitted(self, calibrated: SafetyConfig) -> None:
        """Ekos doing nothing is the state Nocturne has always been in."""
        decision = governor_for(calibrated).validate(
            SetMeridianFlipOffset(enabled=False, offset_deg=0.0)
        )
        assert isinstance(decision, Ok)

    def test_disabling_it_is_permitted_while_uncalibrated_too(
        self, uncalibrated: SafetyConfig
    ) -> None:
        decision = governor_for(uncalibrated).validate(
            SetMeridianFlipOffset(enabled=False, offset_deg=0.0)
        )
        assert isinstance(decision, Ok)

    def test_enabling_it_while_uncalibrated_is_refused(
        self, uncalibrated: SafetyConfig
    ) -> None:
        """There is no hour angle at which a flip is known to be needed."""
        decision = governor_for(uncalibrated).validate(
            SetMeridianFlipOffset(enabled=True, offset_deg=-45.0)
        )
        assert isinstance(decision, Rejected)
        assert decision.rule == "meridian"

    def test_the_refusal_names_both_numbers(self, calibrated: SafetyConfig) -> None:
        decision = governor_for(calibrated).validate(
            SetMeridianFlipOffset(enabled=True, offset_deg=2.5)
        )
        assert isinstance(decision, Rejected)
        assert "+2.5" in decision.reason
        assert "-30" in decision.reason

    @settings(max_examples=200, deadline=None)
    @given(offset_deg=st.floats(min_value=-180.0, max_value=180.0, allow_nan=False))
    def test_nothing_approved_would_flip_inside_the_band(
        self, config_dir: Path, offset_deg: float
    ) -> None:
        """Approval implies Ekos acts before the tube reaches the band."""
        config = calibrated_safety_config(config_dir)
        decision = governor_for(config).validate(
            SetMeridianFlipOffset(enabled=True, offset_deg=offset_deg)
        )
        if isinstance(decision, Ok):
            margin = config.limits.meridian.safety_margin_deg
            assert offset_deg <= EAST_LIMIT_DEG - margin


class TestEveryOneOfThemStillPassesThroughTheGovernor:
    """Bounded is not the same as ungated, and neither is permitted."""

    def commands(self) -> tuple[Command, ...]:
        here = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        near = offset_by(here, degrees=1.0)
        return (
            SyncToCoordinates(ra_hours=near.ra_hours, dec_deg=near.dec_deg, reported=at(here)),
            SlewToTarget(target=resolved(here)),
            SyncToTarget(target=resolved(near), reported=at(here)),
            ResetMountModel(reported=at(here, parked=True)),
            SetMeridianFlipOffset(enabled=False, offset_deg=0.0),
        )

    def test_each_mints_an_approval(self, calibrated: SafetyConfig) -> None:
        governor = governor_for(calibrated)
        for command in self.commands():
            assert governor.approve(command).command is command

    def test_each_is_registered_with_rules_that_are_not_empty(self) -> None:
        """The registry's empty-tuple allowlist must not have grown to cover
        these. Every one of the five carries a rule; that is the decision."""
        from nocturne.safety import COMMAND_RULES

        for command in self.commands():
            rules = COMMAND_RULES[type(command)]
            assert rules, f"{type(command).__name__} is registered with no rules"

    def test_each_describes_itself_in_terms_an_operator_can_act_on(self) -> None:
        for command in self.commands():
            described = command.describe()
            assert described
            assert described != type(command).__name__
