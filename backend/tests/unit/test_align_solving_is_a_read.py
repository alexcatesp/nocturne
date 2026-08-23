"""Solving cannot move the mount, and the reason is structural — ADR 0024.

The align module is the first thing Nocturne drives that could move the
instrument *without being asked to*. ``captureAndSolve`` exposes a frame and
solves it; what happens next is decided by a mode set on an earlier call, and
``Align::executeGOTO()`` acts on that mode without anything of Nocturne's in the
path. Set the mode wrong once and every solve afterwards is a slew the safety
governor never saw.

So the argument that solving is a read has three parts, and this file holds all
three:

1. The mode that makes it true is set **by the solve itself**, every time, not
   once at startup — because Ekos changes it behind you.
2. The two modes that would make it false are never passed. Asserted over the
   source, not over behaviour, so that a call added later is caught.
3. ``loadAndSlew`` is not called at all, because it slews regardless of the mode
   and leaves the mode on "slew" afterwards.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from pathlib import Path

import pytest

from nocturne.executor import ekos
from nocturne.executor.executor import Executor
from nocturne.safety import (
    AbortAlign,
    Ok,
    Rejected,
    SafetyGovernor,
    SetAlignTarget,
    SolveHere,
)
from nocturne.safety.sky import (
    SkyPosition,
    angular_separation_deg,
    precess_date_to_j2000,
    precess_j2000_to_date,
)
from nocturne.schemas import SafetyConfig, Site, load_config_bundle
from tests.fixtures.observing import (
    NIGHT,
    TEST_SITE,
    calibrated_safety_config,
    target_at,
)

CLEAR_OF_THE_BAND = -60.0
USABLE_DECLINATION = 20.0


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


class TestTheGotoModeIsSetByEverySolve:
    """Part one: the mode is not set once and trusted."""

    def test_the_executor_sets_it_before_starting_a_solve(self) -> None:
        source = inspect.getsource(Executor._solve)
        assert "align_set_solver_action" in source
        assert "GOTO_MODE_NOTHING" in source

    def test_it_sets_it_before_it_captures(self) -> None:
        """Order is the whole property. Setting the mode after starting the
        solve would be a race with the solver finishing."""
        source = inspect.getsource(Executor._solve)
        assert source.index("align_set_solver_action") < source.index("align_capture_and_solve")

    def test_that_ordering_check_can_fail(self) -> None:
        """The positive control: the same comparison over the reverse order."""
        reversed_source = (
            "await bridge.align_capture_and_solve()\n"
            "await bridge.align_set_solver_action(GOTO_MODE_NOTHING)\n"
        )
        assert not (
            reversed_source.index("align_set_solver_action")
            < reversed_source.index("align_capture_and_solve")
        )

    def test_nothing_else_starts_a_solve(self) -> None:
        """One call site, so there is one place the ordering has to hold."""
        source = inspect.getsource(Executor)
        assert source.count("align_capture_and_solve") == 1


class TestSolvingCannotMoveTheMount:
    """Part two: the two modes that would let it are never passed."""

    def test_the_mode_constants_are_the_recorded_ones(self) -> None:
        """From ``Ekos::Align::GotoMode`` — sync, slew, nothing, in that order."""
        assert (ekos.GOTO_MODE_SYNC, ekos.GOTO_MODE_SLEW, ekos.GOTO_MODE_NOTHING) == (
            0,
            1,
            2,
        )

    def test_the_only_mode_the_executor_names_is_the_one_that_moves_nothing(
        self,
    ) -> None:
        source = inspect.getsource(Executor)
        assert "GOTO_MODE_NOTHING" in source
        assert "GOTO_MODE_SYNC" not in source
        assert "GOTO_MODE_SLEW" not in source

    def test_that_check_would_catch_a_slewing_mode(self) -> None:
        """The positive control, over a line that would be the whole defect."""
        offender = "await bridge.align_set_solver_action(GOTO_MODE_SLEW)"
        assert "GOTO_MODE_SLEW" in offender

    def test_no_module_outside_the_bridge_names_a_moving_mode(self) -> None:
        """Including the tests' own fixtures. A stub that offered the constant
        would make a call using it look ordinary."""
        package = Path(inspect.getfile(Executor)).resolve().parents[1]
        offenders = [
            path.relative_to(package).as_posix()
            for path in package.rglob("*.py")
            if path.name != "ekos.py" and ("GOTO_MODE_SLEW" in path.read_text(encoding="utf-8"))
        ]
        assert offenders == []

    def test_the_scan_saw_the_package(self) -> None:
        """Passing over an empty tree is what a broken scan looks like."""
        package = Path(inspect.getfile(Executor)).resolve().parents[1]
        assert len(list(package.rglob("*.py"))) >= 20


class TestLoadAndSlewIsNotCalled:
    """Part three, and it is the one the source settles rather than judgement."""

    def test_it_is_in_the_forbidden_list_with_a_reason(self) -> None:
        assert "loadAndSlew" in ekos.FORBIDDEN_ALIGN_METHODS
        assert len(ekos.FORBIDDEN_ALIGN_METHODS["loadAndSlew"]) > 40

    def test_it_is_not_in_the_called_list(self) -> None:
        assert "loadAndSlew" not in ekos.REQUIRED_ALIGN_METHODS

    def test_the_bridge_never_calls_it(self) -> None:
        source = inspect.getsource(ekos)
        assert '"loadAndSlew"' not in source.replace('"loadAndSlew": ', "")

    def test_that_source_check_can_fail(self) -> None:
        offender = 'await self._call(self._align_interface(), "loadAndSlew", url)'
        assert '"loadAndSlew"' in offender.replace('"loadAndSlew": ', "")

    def test_the_two_lists_cover_the_recorded_interface(self) -> None:
        """No third category: a method in neither is one nobody decided about."""
        from tests.fixtures.ekos_interfaces import declared_interfaces

        declared = declared_interfaces()[ekos.EKOS_ALIGN_INTERFACE].method_names()
        undecided = sorted(
            declared - ekos.REQUIRED_ALIGN_METHODS - set(ekos.FORBIDDEN_ALIGN_METHODS)
        )
        assert undecided == [], (
            f"the align module offers {undecided}, which Nocturne neither calls nor "
            "refuses. Decide: add it to REQUIRED_ALIGN_METHODS with the command that "
            "uses it, or to FORBIDDEN_ALIGN_METHODS with the reason (ADR 0024)"
        )

    def test_the_partition_covers_something(self) -> None:
        from tests.fixtures.ekos_interfaces import declared_interfaces

        declared = declared_interfaces()[ekos.EKOS_ALIGN_INTERFACE].method_names()
        assert len(declared) == 13
        assert len(ekos.REQUIRED_ALIGN_METHODS) == 12


class TestTheSolutionIsReadInTheRightOrder:
    """KStars' documentation is wrong about which element is which."""

    def test_the_indices_put_orientation_first(self) -> None:
        """``align_goto.cpp`` returns ``sOrientation << sRA << sDEC``.

        The doxygen comment on the declaration says the first item is the right
        ascension. Following it would read the field orientation as a right
        ascension and the right ascension as a declination — a position that is
        not anywhere, syncable into the mount's model, with nothing in the
        ``ad`` signature to object.
        """
        assert ekos.SOLUTION_ORIENTATION_INDEX == 0
        assert ekos.SOLUTION_RA_INDEX == 1
        assert ekos.SOLUTION_DEC_INDEX == 2

    def test_they_are_distinct(self) -> None:
        indices = {
            ekos.SOLUTION_ORIENTATION_INDEX,
            ekos.SOLUTION_RA_INDEX,
            ekos.SOLUTION_DEC_INDEX,
        }
        assert len(indices) == 3

    def test_the_bridge_reads_them_by_name(self) -> None:
        """Not by literal. A literal 0 and 1 is how the documentation's order
        gets written in by someone reading the comment."""
        source = inspect.getsource(ekos.EkosBridge.align_solution)
        assert "SOLUTION_RA_INDEX" in source
        assert "SOLUTION_DEC_INDEX" in source

    def test_the_solution_is_labelled_with_its_frame(self) -> None:
        """J2000, and named so, because the mount is not."""
        assert "position_j2000" in ekos.AlignSolution.__dataclass_fields__


class TestTheAlignTargetIsCheckedLikeAnyOtherPlace:
    def test_a_target_clear_of_the_limits_is_permitted(self, calibrated: SafetyConfig) -> None:
        position = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        decision = governor_for(calibrated).validate(
            SetAlignTarget(ra_hours=position.ra_hours, dec_deg=position.dec_deg)
        )
        assert isinstance(decision, Ok)

    def test_a_target_inside_the_meridian_band_is_refused(
        self, calibrated: SafetyConfig
    ) -> None:
        """Nothing goes there, and it is still refused. ADR 0024 argues that a
        second lock costing one comparison is worth having on the one command
        that would matter if the goto-mode rule ever regressed."""
        position = target_at(0.0, USABLE_DECLINATION, NIGHT)
        decision = governor_for(calibrated).validate(
            SetAlignTarget(ra_hours=position.ra_hours, dec_deg=position.dec_deg)
        )
        assert isinstance(decision, Rejected)
        assert decision.rule == "meridian"

    def test_a_target_below_the_floor_is_refused(self, calibrated: SafetyConfig) -> None:
        position = target_at(CLEAR_OF_THE_BAND, -70.0, NIGHT)
        decision = governor_for(calibrated).validate(
            SetAlignTarget(ra_hours=position.ra_hours, dec_deg=position.dec_deg)
        )
        assert isinstance(decision, Rejected)
        assert decision.rule == "altitude"


class TestSolvingAndAbortingArePermittedWhereNothingElseIs:
    """They move nothing, so nothing about the sky can refuse them.

    This matters more than it looks: a rig that cannot solve until it is
    calibrated cannot *be* calibrated, because the meridian procedure and the
    polar alignment both want to know where the telescope is pointing.
    """

    @pytest.mark.parametrize("command", [SolveHere(), AbortAlign()])
    def test_permitted_while_uncalibrated(
        self, uncalibrated: SafetyConfig, command: SolveHere | AbortAlign
    ) -> None:
        assert isinstance(governor_for(uncalibrated).validate(command), Ok)

    @pytest.mark.parametrize("command", [SolveHere(), AbortAlign()])
    def test_permitted_with_no_site(
        self, uncalibrated: SafetyConfig, command: SolveHere | AbortAlign
    ) -> None:
        assert isinstance(governor_for(uncalibrated, site=None).validate(command), Ok)

    def test_the_same_conditions_refuse_an_align_target(
        self, uncalibrated: SafetyConfig
    ) -> None:
        """The positive control: those conditions are hostile ones."""
        position = target_at(CLEAR_OF_THE_BAND, USABLE_DECLINATION, NIGHT)
        decision = governor_for(uncalibrated).validate(
            SetAlignTarget(ra_hours=position.ra_hours, dec_deg=position.dec_deg)
        )
        assert isinstance(decision, Rejected)

    @pytest.mark.parametrize("command", [SolveHere(), AbortAlign()])
    def test_each_still_mints_an_approval(
        self, calibrated: SafetyConfig, command: SolveHere | AbortAlign
    ) -> None:
        assert governor_for(calibrated).approve(command).command is command


class TestTheFrameConversionRoundTrips:
    """The align module speaks J2000; the mount and the limits speak of date.

    The executor converts on the way in and back on the way out, so a target set
    through Nocturne and read back from Ekos has to be the same position. If the
    two directions disagreed the error would be silent and would grow with the
    epoch.
    """

    @pytest.mark.parametrize(
        ("ra_hours", "dec_deg"),
        [(0.0, 0.0), (5.5, -12.25), (18.3, 41.27), (2.0, 89.5), (23.9, -89.5)],
    )
    def test_a_position_survives_both_directions(self, ra_hours: float, dec_deg: float) -> None:
        position = SkyPosition(ra_hours=ra_hours, dec_deg=dec_deg)
        of_date = precess_j2000_to_date(position, NIGHT)
        back = precess_date_to_j2000(of_date, NIGHT)
        assert angular_separation_deg(position, back) == pytest.approx(0.0, abs=1e-9)

    def test_the_round_trip_is_not_the_identity(self) -> None:
        """The positive control. Two functions that both returned their input
        would round-trip perfectly and convert nothing."""
        position = SkyPosition(ra_hours=5.5, dec_deg=-12.25)
        of_date = precess_j2000_to_date(position, NIGHT)
        assert angular_separation_deg(position, of_date) > 0.25


class TestTheAlignEvidenceIsRecordedNotRemembered:
    """Every claim this file rests on, checked against the checked-in excerpt.

    The comments in the bridge say what KStars does. This asserts the source
    they were read from is in the repository and still says it, so that the next
    person to doubt any of it has something to look at rather than a comment
    written by whoever is being doubted.
    """

    def text(self) -> str:
        from tests.fixtures.ekos_interfaces import mount_units_documentation

        return mount_units_documentation()

    def test_the_solution_layout_is_recorded(self) -> None:
        assert "return QList<double>() << sOrientation << sRA << sDEC;" in self.text()

    def test_the_wrong_comment_is_recorded_beside_it(self) -> None:
        """Both halves, because the defect is the disagreement between them."""
        assert "First item is RA in degrees" in self.text()

    def test_the_solutions_frame_and_units_are_recorded(self) -> None:
        assert "m_AlignCoord.setRA0(ra / 15.0);" in self.text()

    def test_the_target_setters_frame_and_units_are_recorded(self) -> None:
        assert "@param ra0 J2000 Right Ascension in hours." in self.text()

    def test_the_goto_mode_enum_is_recorded(self) -> None:
        assert "typedef enum { GOTO_SYNC, GOTO_SLEW, GOTO_NOTHING } GotoMode;" in self.text()

    def test_the_switch_that_acts_on_it_is_recorded(self) -> None:
        assert "void Align::executeGOTO()" in self.text()

    def test_the_reason_load_and_slew_is_refused_is_recorded(self) -> None:
        """Not "it looks dangerous": the two lines that make it so."""
        text = self.text()
        assert "m_SolveFromFile = true;" in text
        assert "m_CurrentGotoMode = GOTO_SLEW;" in text

    def test_the_align_states_are_recorded(self) -> None:
        assert "ALIGN_PROGRESS, ALIGN_SUCCESSFUL, ALIGN_SYNCING, ALIGN_SLEWING," in self.text()

    def test_the_state_constants_match_the_recording(self) -> None:
        assert ekos.ALIGN_STATE_SUCCESSFUL == 5
        assert {6, 7} == ekos.ALIGN_STATES_THAT_MOVE_THE_MOUNT
        assert ekos.ALIGN_STATE_NAMES[6] == "syncing"
        assert ekos.ALIGN_STATE_NAMES[7] == "slewing"

    def test_the_detector_would_notice_a_missing_excerpt(self) -> None:
        """The positive control for the eight checks above."""
        assert "void Align::warpDrive()" not in self.text()
