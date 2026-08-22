"""The ephemeris arithmetic the pointing rules stand on — SPEC sections 9.1 and 9.2.

Two kinds of test, and the second is the one that matters.

**Self-consistency.** Ranges, normalisation, identities that must hold whatever
the inputs: the hour angle lands in a half-open interval, the altitude on the
meridian is what spherical trigonometry says it is, azimuth and altitude are a
direction rather than a pair of numbers.

**Agreement with an independent implementation.** Everything here is written by
hand, in this repository, and hand-written spherical trigonometry that is only
checked against itself is checked against nothing. So every angle is compared
against ``astropy`` — a package with two decades of use behind it — over swept
dates, latitudes, declinations and right ascensions. astropy is a *test*
dependency and never a runtime one (ADR 0018); a test below asserts that, so
the day someone imports it in the package the suite says so.

The frame is deliberate. ``EQUATORIAL_EOD_COORD`` is equinox-of-date apparent
place, which is astropy's ``TETE``, not ``ICRS``. Comparing against ICRS would
have shown a slowly growing disagreement — about 0.3 degrees at this epoch,
entirely precession — and the obvious reading of that would have been "our
arithmetic is wrong", which it would not have been.
"""

from __future__ import annotations

import math
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nocturne.safety.sky import (
    SkyPosition,
    altitude_azimuth_deg,
    angular_separation_deg,
    greenwich_mean_sidereal_time_deg,
    hour_angle_deg,
    julian_date,
    local_sidereal_time_deg,
    sun_position,
)

# --------------------------------------------------------------------- oracle

#: How far our arithmetic may sit from astropy's, in degrees.
#:
#: Measured, not chosen: over 200 random points across 2000-2024, all latitudes
#: and the whole sky, the worst altitude disagreement is 0.0051 degrees (18
#: arcseconds), and it is dominated by UT1 minus UTC. Our
#: sidereal time formula wants UT1 and is fed UTC, which differ by up to 0.9
#: seconds of time — 13.5 arcseconds of Earth rotation. Chasing that would mean
#: a leap-second table, a network dependency and a file that goes stale; it is
#: three orders of magnitude below the smallest limit in safety.yaml (the
#: 5-degree meridian margin), so it is a rounding error in this application and
#: an operational hazard to fix.
ORACLE_TOLERANCE_DEG = 0.01

#: The Sun's apparent place from a truncated series, against astropy's full one.
#: The series is quoted at 0.01 degrees and the measured worst case over the same
#: sweep is 0.011 (40 arcseconds), so the quoted figure is honest and this bound
#: sits just above it. The Sun avoidance circle is 30 degrees: 1500 times wider.
SUN_TOLERANCE_DEG = 0.02

#: Dates are swept inside astropy's bundled IERS-B table. Outside it astropy
#: warns, and the suite runs with ``filterwarnings = error``. Restricting the
#: sweep is honest: it is the range over which the oracle is itself exact.
SWEEP_START = datetime(2000, 1, 1, tzinfo=UTC)
SWEEP_DAYS = 9000


def _astropy_altaz(
    ra_hours: float, dec_deg: float, when: datetime, lat: float, lon: float
) -> tuple[float, float]:
    """Altitude and azimuth from astropy, in the same frame and units as ours.

    ``TETE`` — true equator, true equinox of date — is what an INDI mount means
    by ``EQUATORIAL_EOD_COORD``. No refraction: the safety limits are geometric
    (where the tube is, where the tripod is), and refraction moves the image,
    not the tube.
    """
    import astropy.units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    from astropy.utils import iers

    # ADR 0018: astropy is allowed to reach the network in tests as little as
    # the package is allowed to at runtime, which is not at all.
    iers.conf.auto_download = False

    location = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=0 * u.m)
    time = Time(when)
    target = SkyCoord(
        ra=ra_hours * 15.0 * u.deg, dec=dec_deg * u.deg, frame="tete", obstime=time
    )
    with warnings.catch_warnings():
        # Polar motion and dUT1 outside the tabulated range are exactly the
        # residuals ORACLE_TOLERANCE_DEG accounts for.
        warnings.simplefilter("ignore")
        altaz = target.transform_to(AltAz(obstime=time, location=location))
        return float(altaz.alt.deg), float(altaz.az.deg)


def _astropy_sun(when: datetime) -> tuple[float, float]:
    """The Sun's apparent right ascension (hours) and declination (degrees)."""
    from astropy.coordinates import TETE, get_sun
    from astropy.time import Time
    from astropy.utils import iers

    iers.conf.auto_download = False
    time = Time(when)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sun = get_sun(time).transform_to(TETE(obstime=time))
        return float(sun.ra.hour), float(sun.dec.deg)


# ------------------------------------------------------------------ strategies

instants = st.builds(
    lambda days, seconds: SWEEP_START + timedelta(days=days, seconds=seconds),
    st.integers(min_value=0, max_value=SWEEP_DAYS),
    st.integers(min_value=0, max_value=86399),
)
latitudes = st.floats(min_value=-89.5, max_value=89.5, allow_nan=False)
longitudes = st.floats(min_value=-180.0, max_value=180.0, allow_nan=False)
declinations = st.floats(min_value=-89.5, max_value=89.5, allow_nan=False)
right_ascensions = st.floats(min_value=0.0, max_value=23.999, allow_nan=False)

SWEEP = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


class TestJulianDate:
    def test_the_j2000_epoch_is_where_it_should_be(self) -> None:
        # JD 2451545.0 is 2000-01-01 12:00 TT; in UTC that is 11:58:55.816,
        # and the 64.184-second difference is what this function does not model
        # (ADR 0018). Asserting the UTC noon value keeps the convention visible.
        assert julian_date(datetime(2000, 1, 1, 12, tzinfo=UTC)) == pytest.approx(
            2451545.0, abs=1e-9
        )

    def test_a_day_later_is_a_day_later(self) -> None:
        first = julian_date(datetime(2026, 3, 1, 4, 5, 6, tzinfo=UTC))
        second = julian_date(datetime(2026, 3, 2, 4, 5, 6, tzinfo=UTC))
        assert second - first == pytest.approx(1.0, abs=1e-9)

    def test_a_naive_datetime_is_refused(self) -> None:
        """A timestamp without a zone is a guess about which second it is."""
        with pytest.raises(ValueError, match="timezone-aware"):
            julian_date(datetime(2026, 3, 1, 4, 5, 6))

    @given(when=instants)
    @SWEEP
    def test_it_agrees_with_astropy(self, when: datetime) -> None:
        from astropy.time import Time

        assert julian_date(when) == pytest.approx(Time(when).jd, abs=1e-6)


class TestSiderealTime:
    @given(when=instants)
    @SWEEP
    def test_greenwich_sidereal_time_is_an_angle(self, when: datetime) -> None:
        assert 0.0 <= greenwich_mean_sidereal_time_deg(when) < 360.0

    @given(when=instants, longitude=longitudes)
    @SWEEP
    def test_local_sidereal_time_is_greenwich_plus_the_longitude(
        self, when: datetime, longitude: float
    ) -> None:
        expected = (greenwich_mean_sidereal_time_deg(when) + longitude) % 360.0
        assert local_sidereal_time_deg(when, longitude) == pytest.approx(expected, abs=1e-9)

    @given(when=instants)
    @SWEEP
    def test_it_agrees_with_astropy(self, when: datetime) -> None:
        from astropy.time import Time
        from astropy.utils import iers

        iers.conf.auto_download = False
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            reference = float(Time(when).sidereal_time("mean", "greenwich").deg)
        ours = greenwich_mean_sidereal_time_deg(when)
        assert _angular_gap(ours, reference) < ORACLE_TOLERANCE_DEG


class TestHourAngle:
    @given(ra=right_ascensions, when=instants, longitude=longitudes)
    @SWEEP
    def test_it_is_always_in_the_half_open_interval(
        self, ra: float, when: datetime, longitude: float
    ) -> None:
        """SPEC section 7.2: negative east of the meridian, positive west."""
        angle = hour_angle_deg(ra, when, longitude)
        assert -180.0 <= angle < 180.0

    @given(when=instants, longitude=longitudes)
    @SWEEP
    def test_a_target_on_the_meridian_has_hour_angle_zero(
        self, when: datetime, longitude: float
    ) -> None:
        on_the_meridian = local_sidereal_time_deg(when, longitude) / 15.0
        assert abs(hour_angle_deg(on_the_meridian, when, longitude)) < 1e-6

    def test_east_is_negative_and_west_is_positive(self) -> None:
        when = datetime(2026, 8, 22, 22, 0, tzinfo=UTC)
        longitude = -3.7038
        meridian_ra = local_sidereal_time_deg(when, longitude) / 15.0
        # A target one hour of right ascension *greater* than the meridian has
        # not yet crossed it: it is east, and rising.
        assert hour_angle_deg((meridian_ra + 1.0) % 24.0, when, longitude) < 0.0
        assert hour_angle_deg((meridian_ra - 1.0) % 24.0, when, longitude) > 0.0


class TestAltitudeAndAzimuth:
    def test_on_the_meridian_the_altitude_is_ninety_minus_the_zenith_distance(self) -> None:
        for latitude in (-45.0, 0.0, 41.6, 78.0):
            for dec in (-30.0, 0.0, 20.0, 60.0):
                altitude, _ = altitude_azimuth_deg(0.0, dec, latitude)
                assert altitude == pytest.approx(90.0 - abs(latitude - dec), abs=1e-9)

    def test_the_pole_sits_at_the_latitude(self) -> None:
        altitude, azimuth = altitude_azimuth_deg(0.0, 90.0, 41.6)
        assert altitude == pytest.approx(41.6, abs=1e-9)
        assert azimuth == pytest.approx(0.0, abs=1e-9)

    @given(hour_angle=st.floats(-180.0, 180.0), dec=declinations, latitude=latitudes)
    @SWEEP
    def test_the_result_is_always_a_direction(
        self, hour_angle: float, dec: float, latitude: float
    ) -> None:
        altitude, azimuth = altitude_azimuth_deg(hour_angle, dec, latitude)
        assert -90.0 <= altitude <= 90.0
        assert 0.0 <= azimuth < 360.0

    @given(
        ra=right_ascensions,
        dec=declinations,
        when=instants,
        latitude=latitudes,
        longitude=longitudes,
    )
    @SWEEP
    def test_it_agrees_with_astropy(
        self, ra: float, dec: float, when: datetime, latitude: float, longitude: float
    ) -> None:
        ours = altitude_azimuth_deg(hour_angle_deg(ra, when, longitude), dec, latitude)
        reference = _astropy_altaz(ra, dec, when, latitude, longitude)
        assert abs(ours[0] - reference[0]) < ORACLE_TOLERANCE_DEG
        # Azimuth is ill-conditioned near the zenith, where a tiny altitude
        # difference swings it a long way. The altitude comparison above is
        # what the limits are made of; azimuth is checked where it means
        # something.
        if ours[0] < 85.0:
            assert _angular_gap(ours[1], reference[1]) < ORACLE_TOLERANCE_DEG / max(
                math.cos(math.radians(ours[0])), 1e-3
            )


class TestTheSun:
    def test_it_is_where_the_almanac_says_at_the_equinox(self) -> None:
        """March 2026 equinox: the Sun crosses the celestial equator."""
        position = sun_position(datetime(2026, 3, 20, 14, 46, tzinfo=UTC))
        assert abs(position.dec_deg) < 0.02
        assert position.ra_hours == pytest.approx(0.0, abs=0.02) or position.ra_hours == (
            pytest.approx(24.0, abs=0.02)
        )

    @given(when=instants)
    @SWEEP
    def test_it_agrees_with_astropy(self, when: datetime) -> None:
        ours = sun_position(when)
        reference_ra, reference_dec = _astropy_sun(when)
        separation = angular_separation_deg(
            SkyPosition(ra_hours=ours.ra_hours, dec_deg=ours.dec_deg),
            SkyPosition(ra_hours=reference_ra, dec_deg=reference_dec),
        )
        assert separation < SUN_TOLERANCE_DEG


class TestAngularSeparation:
    def test_a_position_is_zero_from_itself(self) -> None:
        position = SkyPosition(ra_hours=5.5, dec_deg=-12.0)
        assert angular_separation_deg(position, position) == pytest.approx(0.0, abs=1e-12)

    def test_the_poles_are_one_hundred_and_eighty_degrees_apart(self) -> None:
        north = SkyPosition(ra_hours=0.0, dec_deg=90.0)
        south = SkyPosition(ra_hours=12.0, dec_deg=-90.0)
        assert angular_separation_deg(north, south) == pytest.approx(180.0, abs=1e-9)

    def test_one_hour_of_right_ascension_on_the_equator_is_fifteen_degrees(self) -> None:
        first = SkyPosition(ra_hours=3.0, dec_deg=0.0)
        second = SkyPosition(ra_hours=4.0, dec_deg=0.0)
        assert angular_separation_deg(first, second) == pytest.approx(15.0, abs=1e-9)

    def test_it_stays_accurate_at_small_separations(self) -> None:
        """The cosine formula loses its digits here; the Vincenty form does not."""
        first = SkyPosition(ra_hours=6.0, dec_deg=30.0)
        second = SkyPosition(ra_hours=6.0, dec_deg=30.0 + 1.0 / 3600.0)
        assert angular_separation_deg(first, second) == pytest.approx(1.0 / 3600.0, rel=1e-6)

    @given(
        ra_a=right_ascensions,
        dec_a=declinations,
        ra_b=right_ascensions,
        dec_b=declinations,
    )
    @SWEEP
    def test_it_agrees_with_astropy(
        self, ra_a: float, dec_a: float, ra_b: float, dec_b: float
    ) -> None:
        import astropy.units as u
        from astropy.coordinates import SkyCoord

        ours = angular_separation_deg(
            SkyPosition(ra_hours=ra_a, dec_deg=dec_a),
            SkyPosition(ra_hours=ra_b, dec_deg=dec_b),
        )
        reference = float(
            SkyCoord(ra=ra_a * 15.0 * u.deg, dec=dec_a * u.deg)
            .separation(SkyCoord(ra=ra_b * 15.0 * u.deg, dec=dec_b * u.deg))
            .deg
        )
        assert ours == pytest.approx(reference, abs=1e-9)


class TestTheOracleIsRealAndStaysThat:
    """A comparison against a package nobody installed compares nothing."""

    def test_astropy_is_a_development_dependency(self, repo_root: Path) -> None:
        pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        development = pyproject.split("dev = [", 1)[1].split("]", 1)[0]
        assert "astropy" in development, (
            "astropy is the oracle these tests are built on. If it leaves the dev "
            "extras, every comparison in this module stops running."
        )

    def test_astropy_is_not_a_runtime_dependency(self, repo_root: Path) -> None:
        """ADR 0018. The safety path does its own arithmetic, on purpose."""
        pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        runtime = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
        assert "astropy" not in runtime

    def test_the_oracle_disagrees_when_it_should(self) -> None:
        """The positive control: point the comparison at a wrong answer.

        Every agreement test above passes by finding no difference. This one
        proves the comparison can find one — a target displaced by a degree
        must fail the same tolerance the others pass.
        """
        when = datetime(2026, 8, 22, 22, 0, tzinfo=UTC)
        latitude, longitude = 41.6, -3.7
        honest, _ = altitude_azimuth_deg(hour_angle_deg(5.0, when, longitude), 20.0, latitude)
        reference, _ = _astropy_altaz(5.0, 21.0, when, latitude, longitude)
        assert abs(honest - reference) > ORACLE_TOLERANCE_DEG


def _angular_gap(first: float, second: float) -> float:
    """The shortest distance between two angles in degrees, across the wrap."""
    return abs((first - second + 180.0) % 360.0 - 180.0)
