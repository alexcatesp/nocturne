"""The pointing gate — SPEC sections 9.1 and 9.2, issue #1, ADR 0007 and 0019.

Until this existed, a raw write to the mount's coordinate vector reached the
instrument with nothing consulting the altitude floor, the Sun or the hour
angle. On a 200PDS with no tripod extension that is the collision
``docs/meridian-calibration.md`` exists to prevent.

Tests are written in the quantities the limits are stated in — "thirty degrees
east of the meridian", "below the altitude floor" — against a frozen clock and
a site with real coordinates. Each one asserts its own preconditions: that the
target really is above the floor when the point of the test is the Sun, that
the Sun really is up when the point is the twilight gate. A test that rejects
for the wrong reason is a test that proves nothing, and without the
preconditions it reads exactly like one that works.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nocturne.safety import (
    ConnectDevice,
    DisconnectDevice,
    Ok,
    Rejected,
    SafetyGovernor,
    SetProperty,
)
from nocturne.safety.properties import (
    COORDINATE_PROPERTIES,
    DRIVER_LIMIT_PROPERTIES,
    UNVALIDATABLE_MOTION_PROPERTIES,
    pointing_intent,
)
from nocturne.safety.sky import (
    SkyPosition,
    altitude_azimuth_deg,
    angular_separation_deg,
    hour_angle_deg,
    sun_position,
)
from nocturne.schemas import SafetyConfig, Site, load_config_bundle
from tests.fixtures.observing import (
    DAY,
    EAST_LIMIT_DEG,
    NIGHT,
    TEST_SITE,
    WEST_LIMIT_DEG,
    calibrated_safety_config,
    target_at,
)

MOUNT = "EQMod Mount"
COORDINATES = "EQUATORIAL_EOD_COORD"


def slew(position: SkyPosition) -> SetProperty:
    """The raw property write that commands a slew on a real EQMod mount."""
    return SetProperty(
        device=MOUNT,
        property=COORDINATES,
        values={"RA": position.ra_hours, "DEC": position.dec_deg},
    )


def governor_for(
    config: SafetyConfig, *, site: Site | None = TEST_SITE, when: datetime = NIGHT
) -> SafetyGovernor:
    return SafetyGovernor(config, site=site, clock=lambda: when)


@pytest.fixture
def uncalibrated(config_dir: Path) -> SafetyConfig:
    return load_config_bundle(config_dir).safety


@pytest.fixture
def calibrated(config_dir: Path) -> SafetyConfig:
    return calibrated_safety_config(config_dir)


def altitude_of(position: SkyPosition, when: datetime) -> float:
    return altitude_azimuth_deg(
        hour_angle_deg(position.ra_hours, when, TEST_SITE.longitude),
        position.dec_deg,
        TEST_SITE.latitude,
    )[0]


class TestASiteIsRequiredBeforeAnythingCanBeChecked:
    """Altitude and hour angle do not exist without coordinates (SPEC section 9.2)."""

    def test_a_governor_with_no_site_refuses_to_point(self, calibrated: SafetyConfig) -> None:
        decision = governor_for(calibrated, site=None).validate(slew(target_at(0, 20, NIGHT)))
        assert isinstance(decision, Rejected)
        assert decision.rule == "site"

    def test_the_shipped_placeholder_is_refused_by_name(
        self, calibrated: SafetyConfig, config_dir: Path
    ) -> None:
        shipped = load_config_bundle(config_dir).equipment.site
        assert shipped.is_placeholder, "the repository must keep shipping a placeholder"
        decision = governor_for(calibrated, site=shipped).validate(
            slew(target_at(0, 20, NIGHT))
        )
        assert isinstance(decision, Rejected)
        assert "equipment.local.yaml" in decision.reason

    def test_a_non_pointing_write_still_works_without_a_site(
        self, calibrated: SafetyConfig
    ) -> None:
        """The site gate covers pointing, not the whole instrument."""
        command = SetProperty(
            device="ZWO CCD ASI533MM Pro",
            property="CCD_TEMPERATURE",
            values={"CCD_TEMP": -10.0},
        )
        assert isinstance(governor_for(calibrated, site=None).validate(command), Ok)


class TestAltitudeLimits:
    """SPEC section 9.2: altitude floor and ceiling."""

    def test_a_target_below_the_floor_is_refused(self, calibrated: SafetyConfig) -> None:
        position = target_at(0.0, -60.0, NIGHT)
        assert altitude_of(position, NIGHT) < calibrated.limits.altitude_min_deg
        decision = governor_for(calibrated).validate(slew(position))
        assert isinstance(decision, Rejected)
        assert decision.rule == "altitude"
        assert "altitude" in decision.reason

    def test_a_target_above_the_ceiling_is_refused(self, calibrated: SafetyConfig) -> None:
        """Zenith avoidance: an equatorial mount tracks badly through the zenith."""
        position = target_at(0.0, TEST_SITE.latitude, NIGHT)
        assert altitude_of(position, NIGHT) > calibrated.limits.altitude_max_deg
        decision = governor_for(calibrated).validate(slew(position))
        assert isinstance(decision, Rejected)
        assert decision.rule == "altitude"

    def test_a_target_inside_the_window_passes_this_rule(
        self, calibrated: SafetyConfig
    ) -> None:
        position = target_at(0.0, 20.0, NIGHT)
        assert (
            calibrated.limits.altitude_min_deg
            < altitude_of(position, NIGHT)
            < calibrated.limits.altitude_max_deg
        )
        assert isinstance(governor_for(calibrated).validate(slew(position)), Ok)


class TestTheSun:
    """SPEC section 9.2: sun avoidance, and the sun-altitude gate."""

    def test_pointing_at_the_sun_is_refused(self, calibrated: SafetyConfig) -> None:
        sun = sun_position(DAY)
        assert altitude_of(sun, DAY) > calibrated.limits.altitude_min_deg, (
            "the point of this test is the Sun, so it must not be refused for altitude"
        )
        decision = governor_for(calibrated, when=DAY).validate(slew(sun))
        assert isinstance(decision, Rejected)
        assert decision.rule == "sun_avoidance"

    def test_a_target_inside_the_avoidance_circle_is_refused(
        self, calibrated: SafetyConfig
    ) -> None:
        sun = sun_position(DAY)
        near = SkyPosition(ra_hours=sun.ra_hours, dec_deg=sun.dec_deg + 20.0)
        assert angular_separation_deg(near, sun) < calibrated.limits.sun_avoidance_deg
        decision = governor_for(calibrated, when=DAY).validate(slew(near))
        assert isinstance(decision, Rejected)
        assert decision.rule == "sun_avoidance"

    def test_pointing_is_refused_while_the_sun_is_up(self, calibrated: SafetyConfig) -> None:
        """A slew sweeps a path, and the gate is what makes checking only its end sound."""
        position = target_at(-60.0, 20.0, DAY)
        assert angular_separation_deg(position, sun_position(DAY)) > (
            calibrated.limits.sun_avoidance_deg
        ), "this target must be clear of the Sun, or it proves the other rule"
        assert altitude_of(position, DAY) > calibrated.limits.altitude_min_deg
        decision = governor_for(calibrated, when=DAY).validate(slew(position))
        assert isinstance(decision, Rejected)
        assert decision.rule == "sun_altitude"
        assert "Sun" in decision.reason

    def test_the_same_target_is_permitted_at_night(self, calibrated: SafetyConfig) -> None:
        """The positive control for both Sun rules: darkness lifts them and nothing else."""
        permitted = governor_for(calibrated).validate(slew(target_at(-10, 20, NIGHT)))
        assert isinstance(permitted, Ok)


class TestMeridianLimits:
    """SPEC section 9.1 — the highest-severity limit in the project."""

    def test_nothing_is_pointed_while_the_limits_are_uncalibrated(
        self, uncalibrated: SafetyConfig
    ) -> None:
        """ADR 0019. Not a warning, not a default, and not only unattended."""
        position = target_at(0.0, 20.0, NIGHT)
        assert altitude_of(position, NIGHT) > uncalibrated.limits.altitude_min_deg
        decision = governor_for(uncalibrated).validate(slew(position))
        assert isinstance(decision, Rejected)
        assert decision.rule == "meridian"
        assert "meridian-calibration" in decision.reason

    def test_past_the_east_limit_is_refused(self, calibrated: SafetyConfig) -> None:
        east, _ = calibrated.limits.meridian.effective_hour_angle_limits_deg()
        decision = governor_for(calibrated).validate(slew(target_at(east - 5.0, 20.0, NIGHT)))
        assert isinstance(decision, Rejected)
        assert decision.rule == "meridian"

    def test_past_the_west_limit_is_refused(self, calibrated: SafetyConfig) -> None:
        _, west = calibrated.limits.meridian.effective_hour_angle_limits_deg()
        decision = governor_for(calibrated).validate(slew(target_at(west + 5.0, 20.0, NIGHT)))
        assert isinstance(decision, Rejected)
        assert decision.rule == "meridian"

    def test_the_safety_margin_is_what_is_enforced_not_the_measurement(
        self, calibrated: SafetyConfig
    ) -> None:
        """Between the measured limit and the enforced one is where the margin lives."""
        margin = calibrated.limits.meridian.safety_margin_deg
        assert margin > 0.0
        inside_the_measurement = WEST_LIMIT_DEG - margin / 2.0
        governor = governor_for(calibrated)
        decision = governor.validate(slew(target_at(inside_the_measurement, 20.0, NIGHT)))
        assert isinstance(decision, Rejected), (
            "an hour angle inside the measured limit but inside the margin must be "
            "refused; enforcing the measurement itself would spend the margin"
        )
        assert isinstance(
            governor.validate(slew(target_at(EAST_LIMIT_DEG + margin + 1.0, 20.0, NIGHT))), Ok
        )

    def test_between_the_limits_is_permitted(self, calibrated: SafetyConfig) -> None:
        assert isinstance(governor_for(calibrated).validate(slew(target_at(0, 20, NIGHT))), Ok)


class TestWhatCountsAsPointing:
    """The classification, and the evidence it is complete."""

    def test_a_coordinate_write_missing_an_element_is_refused(
        self, calibrated: SafetyConfig
    ) -> None:
        """Half a coordinate is not half safe: the mount fills the rest in itself."""
        command = SetProperty(device=MOUNT, property=COORDINATES, values={"RA": 5.0})
        decision = governor_for(calibrated).validate(command)
        assert isinstance(decision, Rejected)
        assert decision.rule == "pointing_unreadable"

    def test_a_coordinate_write_with_a_non_numeric_value_is_refused(
        self, calibrated: SafetyConfig
    ) -> None:
        command = SetProperty(
            device=MOUNT, property=COORDINATES, values={"RA": "five", "DEC": 20.0}
        )
        decision = governor_for(calibrated).validate(command)
        assert isinstance(decision, Rejected)
        assert decision.rule == "pointing_unreadable"

    def test_an_out_of_range_coordinate_is_refused(self, calibrated: SafetyConfig) -> None:
        command = SetProperty(
            device=MOUNT, property=COORDINATES, values={"RA": 25.0, "DEC": 20.0}
        )
        decision = governor_for(calibrated).validate(command)
        assert isinstance(decision, Rejected)
        assert decision.rule == "pointing_unreadable"

    @pytest.mark.parametrize("name", sorted(UNVALIDATABLE_MOTION_PROPERTIES))
    def test_motion_with_no_target_is_refused(
        self, name: str, calibrated: SafetyConfig
    ) -> None:
        """Open-loop motion has no coordinate to check, so it has no safe form here."""
        command = SetProperty(device=MOUNT, property=name, values={"MOTION_NORTH": True})
        decision = governor_for(calibrated).validate(command)
        assert isinstance(decision, Rejected)
        assert decision.rule == "unvalidatable_motion"

    @pytest.mark.parametrize("name", sorted(DRIVER_LIMIT_PROPERTIES))
    def test_the_drivers_own_limits_are_not_writable_yet(
        self, name: str, calibrated: SafetyConfig
    ) -> None:
        """ADR 0010 defines that path and is not implemented."""
        command = SetProperty(device=MOUNT, property=name, values={"whatever": True})
        decision = governor_for(calibrated).validate(command)
        assert isinstance(decision, Rejected)
        assert "0010" in decision.reason

    def test_parking_is_permitted(self, calibrated: SafetyConfig) -> None:
        """Park is the safe state and the abort action (SPEC section 9.4)."""
        command = SetProperty(device=MOUNT, property="TELESCOPE_PARK", values={"PARK": True})
        assert isinstance(governor_for(calibrated).validate(command), Ok)

    def test_the_uncalibrated_refusal_does_not_block_parking(
        self, uncalibrated: SafetyConfig
    ) -> None:
        """A rig that cannot park is a rig that cannot be made safe."""
        command = SetProperty(device=MOUNT, property="TELESCOPE_PARK", values={"PARK": True})
        assert isinstance(governor_for(uncalibrated, site=None).validate(command), Ok)

    @pytest.mark.parametrize(
        "name",
        [
            "CCD_TEMPERATURE",
            "CCD_EXPOSURE",
            "FILTER_SLOT",
            "ABS_FOCUS_POSITION",
            "SLEWSPEEDS",
            "TELESCOPE_TRACK_STATE",
            "CONNECTION",
        ],
    )
    def test_the_rest_of_the_instrument_is_untouched(
        self, name: str, uncalibrated: SafetyConfig
    ) -> None:
        """M1 exists to read and write these. The gate is about pointing."""
        command = SetProperty(device="anything", property=name, values={"value": 1.0})
        assert isinstance(governor_for(uncalibrated, site=None).validate(command), Ok)

    def test_every_moving_property_in_the_recorded_dump_is_classified(
        self, repo_root: Path
    ) -> None:
        """The classification is measured against the real mount, not remembered.

        ``wave150i-properties.txt`` is what ``indi_eqmod`` exported on the
        reference rig. Anything in it that commands motion and is not in one of
        the three sets would fall through to "not pointing" and be permitted.
        """
        dump = (
            repo_root
            / "backend"
            / "tests"
            / "fixtures"
            / "hardware"
            / "wave150i-properties.txt"
        ).read_text(encoding="utf-8")
        vectors = {
            line.split("=")[0].strip().split(".")[1]
            for line in dump.splitlines()
            if "." in line.split("=")[0] and line.strip() and not line.startswith("#")
        }
        assert "EQUATORIAL_EOD_COORD" in vectors, "the dump was not parsed at all"
        assert len(vectors) > 50, f"only {len(vectors)} vectors parsed out of the dump"

        suspicious = {
            name
            for name in vectors
            if any(
                token in name
                for token in ("COORD", "MOTION", "PARK", "GUIDE_NS", "GUIDE_WE", "HOME")
            )
        }
        assert suspicious, "the motion-name filter matched nothing"
        classified = (
            COORDINATE_PROPERTIES
            | UNVALIDATABLE_MOTION_PROPERTIES
            | DRIVER_LIMIT_PROPERTIES
            | PERMITTED_MOTION_PROPERTIES
        )
        assert not (suspicious - classified), (
            f"unclassified motion vectors on the real mount: {sorted(suspicious - classified)}"
        )


#: Vectors that name motion but are deliberately permitted, each for a stated
#: reason. Listed here rather than in the package so the test above cannot be
#: satisfied by quietly adding a name to the code it checks.
PERMITTED_MOTION_PROPERTIES = frozenset(
    {
        # Park is the safe state and the abort action (SPEC section 9.4).
        "TELESCOPE_PARK",
        # Stopping is always permitted. A governor that can refuse "stop" has
        # the sign of the whole design backwards.
        "TELESCOPE_ABORT_MOTION",
        # Where the site is, not where the tube points.
        "GEOGRAPHIC_COORD",
        # Read-back of the alignment subsystem's idea of the current position.
        "ALIGNTELESCOPECOORDS",
        # Park behaviour and the stored park position are configuration of the
        # safe state, written by the operator through the driver, and nothing in
        # Nocturne writes them.
        "TELESCOPE_PARK_OPTION",
        "TELESCOPE_PARK_POSITION",
        # Guide *rates*, not guide pulses. A rate moves nothing on its own.
        "GUIDE_RATE",
        "ST4_GUIDE_RATE_NS",
        "ST4_GUIDE_RATE_WE",
        # Whether the driver reverses a direction; MountLink writes neither.
        "TELESCOPE_REVERSE_MOTION",
        "REVERSEDEC",
    }
)


class TestTheClockIsInjectedAndReadOnce:
    def test_every_rule_sees_the_same_instant(self, calibrated: SafetyConfig) -> None:
        """One decision, one time. Rules that each read the clock cannot be replayed."""
        ticks = iter([NIGHT + timedelta(hours=hours) for hours in range(24)])
        governor = SafetyGovernor(calibrated, site=TEST_SITE, clock=lambda: next(ticks))
        governor.validate(slew(target_at(0, 20, NIGHT)))
        # One decision must consume exactly one tick, whatever the rules do.
        assert next(ticks) == NIGHT + timedelta(hours=1)

    def test_no_rule_reads_the_system_clock(self) -> None:
        """Structural, with the positive control below."""
        offenders = _system_clock_calls_in(_safety_sources())
        assert offenders == {"_system_clock"}, (
            "only the governor's default clock may read the system time; found "
            f"{sorted(offenders)}"
        )

    def test_the_clock_detector_finds_a_planted_offender(self) -> None:
        source = (
            "from datetime import UTC, datetime\n"
            "def sneaky() -> None:\n"
            "    datetime.now(UTC)\n"
            "def also() -> None:\n"
            "    time.time()\n"
        )
        assert _clock_calls_in_source(source) == {"sneaky", "also"}

    def test_the_source_scan_is_not_empty(self) -> None:
        sources = _safety_sources()
        assert len(sources) >= 4, sorted(sources)
        assert any("governor.py" in name for name in sources)


def _safety_sources() -> dict[str, str]:
    from nocturne import safety

    package = Path(inspect.getfile(safety)).parent
    found = {path.name: path.read_text(encoding="utf-8") for path in package.glob("*.py")}
    if not found:
        raise AssertionError(f"no safety modules found under {package}")
    return found


def _system_clock_calls_in(sources: dict[str, str]) -> set[str]:
    offenders: set[str] = set()
    for source in sources.values():
        offenders |= _clock_calls_in_source(source)
    return offenders


def _clock_calls_in_source(source: str) -> set[str]:
    """Functions that read the system clock, by name."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def enclosing(node: ast.AST) -> str:
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
                return current.name
            current = parents.get(current)
        return "<module>"

    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        reads_a_date = node.func.attr in {"now", "utcnow", "today"} and node.func.value.id in {
            "datetime",
            "date",
        }
        reads_a_counter = (
            node.func.attr in {"time", "monotonic"} and node.func.value.id == "time"
        )
        if reads_a_date or reads_a_counter:
            found.add(enclosing(node))
    return found


class TestNothingApprovedEverViolatesALimit:
    """Property: whatever the rules do, an approval implies every limit holds."""

    @settings(max_examples=200, deadline=None)
    @given(
        hour_angle=st.floats(-180.0, 179.9),
        dec=st.floats(-89.0, 89.0),
        hours=st.floats(0.0, 24.0),
    )
    def test_an_approved_pointing_satisfies_every_limit(
        self, hour_angle: float, dec: float, hours: float, config_dir: Path
    ) -> None:
        config = calibrated_safety_config(config_dir)
        when = NIGHT + timedelta(hours=hours)
        position = target_at(hour_angle, dec, when)
        decision = governor_for(config, when=when).validate(slew(position))
        if not isinstance(decision, Ok):
            return
        limits = config.limits
        east, west = limits.meridian.effective_hour_angle_limits_deg()
        altitude = altitude_azimuth_deg(hour_angle, dec, TEST_SITE.latitude)[0]
        sun = sun_position(when)
        sun_altitude = altitude_azimuth_deg(
            hour_angle_deg(sun.ra_hours, when, TEST_SITE.longitude),
            sun.dec_deg,
            TEST_SITE.latitude,
        )[0]
        assert limits.altitude_min_deg <= altitude <= limits.altitude_max_deg
        assert east <= hour_angle <= west
        assert angular_separation_deg(position, sun) >= limits.sun_avoidance_deg
        assert sun_altitude <= limits.sun_altitude_max_deg

    @settings(max_examples=100, deadline=None)
    @given(
        hour_angle=st.floats(-180.0, 179.9),
        dec=st.floats(-89.0, 89.0),
        hours=st.floats(0.0, 24.0),
    )
    def test_an_uncalibrated_configuration_approves_no_pointing_at_all(
        self, hour_angle: float, dec: float, hours: float, config_dir: Path
    ) -> None:
        config = load_config_bundle(config_dir).safety
        assert not config.limits.meridian.calibrated
        when = NIGHT + timedelta(hours=hours)
        position = target_at(hour_angle, dec, when)
        decision = governor_for(config, when=when).validate(slew(position))
        assert isinstance(decision, Rejected)


class TestTheExtractorIsTotal:
    """A command type the extractor does not know about must not slip through."""

    def test_every_registered_command_is_handled(self) -> None:
        from nocturne.safety import COMMAND_RULES

        assert COMMAND_RULES, "the registry is empty; this test would prove nothing"
        for command_type in COMMAND_RULES:
            assert command_type in _EXTRACTOR_KNOWS, (
                f"{command_type.__name__} is registered with the governor but "
                "pointing_intent() has never been taught what it means. Teach it, "
                "and add it here."
            )

    def test_a_connect_is_not_a_pointing_command(self) -> None:
        assert pointing_intent(ConnectDevice(device=MOUNT)) is None


#: The command types ``pointing_intent`` has been reasoned about. Kept in the
#: test rather than derived from the code, so that adding a command type to the
#: package cannot quietly satisfy the check above.
_EXTRACTOR_KNOWS = {ConnectDevice, DisconnectDevice, SetProperty}


class TestRejectionsSayEnoughToActOn:
    def test_the_reason_names_the_number_and_where_it_came_from(
        self, calibrated: SafetyConfig
    ) -> None:
        decision = governor_for(calibrated).validate(slew(target_at(0.0, -60.0, NIGHT)))
        assert isinstance(decision, Rejected)
        assert "25" in decision.reason, decision.reason
        assert "safety.yaml" in decision.reason
