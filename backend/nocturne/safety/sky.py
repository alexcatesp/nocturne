"""Ephemeris arithmetic for the safety limits — SPEC sections 9.1 and 9.2.

Altitude, hour angle and angular distance from the Sun are what the limits in
``safety.yaml`` are stated in, and none of them exists without this arithmetic.
It lives here, in the safety package, rather than in a dependency: the
reasoning is ADR 0018, and the short form is that a control path on a mount at
three in the morning must not import a package that may decide to fetch an
Earth-orientation table over the network.

**Everything in this module is a pure function of its arguments.** No clock, no
configuration, no I/O, no state. The current time arrives as a parameter — the
governor holds the clock (SPEC section 9) — which is what makes every decision
the rules take reproducible from its log line alone.

**On the constants below and CLAUDE.md section 6.** "No magic numbers" is about
thresholds, tolerances and limits: the things an operator tunes, which belong
in validated YAML. The numbers here are none of those. They are the length of
the sidereal day and the terms of a solar series — properties of the solar
system, not of this rig, and moving them into a configuration file would invite
an edit that can only make them wrong. Every one is named, and every one cites
where it comes from.

**Precision, and why this much is enough.** Sidereal time is computed from UTC
where the formula wants UT1. They differ by at most 0.9 seconds, which is 13.5
arcseconds of Earth rotation. The Sun's place comes from a truncated series
good to about 0.01 degrees. The smallest quantity any of this is compared
against is the 5-degree meridian safety margin, and the largest error above is
about a thousandth of it. ``test_safety_sky.py`` measures all of it against
astropy rather than asserting it here.

**What this module deliberately does not model.** Refraction: the limits are
geometric — where the tube is, and where the tripod leg is — and refraction
moves the image, not the tube. Parallax and light-time for solar-system bodies:
the Sun's geocentric place is within 9 arcseconds of its topocentric one, four
orders below the avoidance circle. Proper motion, nutation in the target's own
place, and polar motion: all far below the same threshold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, final

#: Julian date of the Unix epoch, 1970-01-01T00:00:00Z. Exact by definition.
JULIAN_DATE_AT_UNIX_EPOCH: Final = 2440587.5

#: Julian date of J2000.0 (2000-01-01T12:00:00 TT), the epoch every series below
#: is expanded about.
JULIAN_DATE_J2000: Final = 2451545.0

SECONDS_PER_DAY: Final = 86400.0
DAYS_PER_JULIAN_CENTURY: Final = 36525.0
DEGREES_PER_HOUR: Final = 15.0
HOURS_PER_DAY: Final = 24.0

# Greenwich mean sidereal time, Meeus, *Astronomical Algorithms*, 2nd edition,
# equation 12.4: a cubic in the Julian centuries since J2000 plus a linear term
# in the days since J2000. The linear coefficient is the length of the mean
# sidereal day expressed in degrees of rotation per day of UT.
GMST_CONSTANT_DEG: Final = 280.46061837
GMST_DEGREES_PER_DAY: Final = 360.98564736629
GMST_QUADRATIC_DEG: Final = 0.000387933
GMST_CUBIC_DIVISOR: Final = 38710000.0

# The Sun's apparent geocentric place, from the Astronomical Almanac's
# low-precision formulae ("Approximate Solar Coordinates", section C), quoted at
# 0.01 degrees for 1950 through 2050. Mean longitude and mean anomaly are linear
# in days since J2000; the equation of the centre is the two leading terms.
SUN_MEAN_LONGITUDE_DEG: Final = 280.460
SUN_MEAN_LONGITUDE_DEG_PER_DAY: Final = 0.9856474
SUN_MEAN_ANOMALY_DEG: Final = 357.528
SUN_MEAN_ANOMALY_DEG_PER_DAY: Final = 0.9856003
SUN_CENTRE_FIRST_DEG: Final = 1.915
SUN_CENTRE_SECOND_DEG: Final = 0.020

#: Obliquity of the ecliptic at J2000 and its secular change, same source.
OBLIQUITY_DEG: Final = 23.439
OBLIQUITY_DEG_PER_DAY: Final = 0.0000004

# Precession of the equinoxes, Meeus equation 21.2 (the IAU 1976 series): the
# three Euler angles that carry a mean place from J2000 to the mean equinox of
# date, in arcseconds, as cubics in the Julian centuries since J2000.
#
# **Why this is here at all.** Ekos' align module reports a plate solve in
# J2000, and the mount works in the equinox of date. That difference is about
# 0.3 degrees at this epoch — the one frame distinction :class:`SkyPosition`'s
# docstring says actually matters at the precision these limits need — and it
# would otherwise be written into the mount's model on every alignment.
PRECESSION_ZETA_ARCSEC: Final = (2306.2181, 0.30188, 0.017998)
PRECESSION_Z_ARCSEC: Final = (2306.2181, 1.09468, 0.018203)
PRECESSION_THETA_ARCSEC: Final = (2004.3109, -0.42665, -0.041833)

ARCSEC_PER_DEGREE: Final = 3600.0


@final
@dataclass(frozen=True, slots=True)
class SkyPosition:
    """An equatorial position: right ascension in hours, declination in degrees.

    Hours for right ascension because that is what INDI's
    ``EQUATORIAL_EOD_COORD`` carries, and converting at the boundary rather than
    everywhere inside is one fewer place to drop a factor of fifteen.

    The frame is equinox of date (apparent place), which is what the mount
    means. Nothing here converts between frames; at the precision these limits
    need, the distinction that matters is the one that is 0.3 degrees at this
    epoch — J2000 versus date — and both the mount and the Sun series speak the
    latter.
    """

    ra_hours: float
    dec_deg: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.ra_hours < HOURS_PER_DAY:
            raise ValueError(f"right ascension must be in [0, 24) hours, got {self.ra_hours}")
        if not -90.0 <= self.dec_deg <= 90.0:
            raise ValueError(f"declination must be in [-90, 90] degrees, got {self.dec_deg}")


def _fold(value: float, period: float) -> float:
    """``value`` modulo ``period``, guaranteed to stay below ``period``.

    Python's ``%`` does not guarantee that. For a tiny negative input the exact
    result is just under the period, and the nearest representable double *is*
    the period: ``-1e-206 % 360.0 == 360.0``. Every range assertion in this
    module is half-open, and hypothesis found this within a hundred examples.
    """
    folded = value % period
    return 0.0 if folded >= period else folded


def julian_date(when: datetime) -> float:
    """The Julian date of ``when``, which must be timezone-aware.

    Raises:
        ValueError: if ``when`` carries no timezone. A naive timestamp is a
            guess about which second it is, and the whole point of the clock
            being injected (SPEC section 9) is that the guess is never taken.
    """
    if when.tzinfo is None or when.tzinfo.utcoffset(when) is None:
        raise ValueError(
            "the instant must be timezone-aware; a naive datetime does not say "
            "which second it is, and every angle below is a function of that"
        )
    return JULIAN_DATE_AT_UNIX_EPOCH + when.timestamp() / SECONDS_PER_DAY


def datetime_from_julian_date(julian: float) -> datetime:
    """The UTC instant of a Julian date. The inverse of :func:`julian_date`.

    KStars reports its own clock as a Julian date and nothing else, so reading
    it back requires this. ``test_safety_sky.py`` round-trips the pair and
    checks both directions against astropy.

    Raises:
        ValueError: if the value is not a finite Julian date this side of a
            plausible epoch. A NaN or a wild number arriving from a parsed
            document must not become a timestamp that merely looks odd.
    """
    if not math.isfinite(julian):
        raise ValueError(f"a Julian date must be a finite number, got {julian!r}")
    seconds = (julian - JULIAN_DATE_AT_UNIX_EPOCH) * SECONDS_PER_DAY
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"{julian!r} is not a representable Julian date: {exc}") from exc


def _precession_angles_deg(when: datetime) -> tuple[float, float, float]:
    """``(zeta, z, theta)`` in degrees, for the mean equinox of ``when``."""
    centuries = (julian_date(when) - JULIAN_DATE_J2000) / DAYS_PER_JULIAN_CENTURY
    return tuple(  # type: ignore[return-value]  # exactly three, one per series
        (linear * centuries + quadratic * centuries**2 + cubic * centuries**3)
        / ARCSEC_PER_DEGREE
        for linear, quadratic, cubic in (
            PRECESSION_ZETA_ARCSEC,
            PRECESSION_Z_ARCSEC,
            PRECESSION_THETA_ARCSEC,
        )
    )


def precess_j2000_to_date(position: SkyPosition, when: datetime) -> SkyPosition:
    """``position``, given in J2000, as a mean place of the equinox of ``when``.

    Ekos' align module reports a plate solve in J2000; the mount points in the
    equinox of date. Treating one as the other is an error of about 0.3 degrees
    at this epoch, which is six per cent of the shipped meridian safety margin
    and — far worse — would be written into the mount's model by every sync that
    followed a solve.

    **What this does not include.** Nutation, about 17 arcseconds, and annual
    aberration, about 20. Both are three orders below the smallest limit any of
    this is compared against, and two orders below the frame error this function
    exists to remove; adding them would be arithmetic in the safety path with
    nothing to show for it. So this returns a *mean* place of date rather than an
    apparent one, and says so. ``test_safety_sky.py`` measures the difference
    against astropy rather than asserting it here.
    """
    return _rotate_equatorial(position, *_precession_angles_deg(when))


def precess_date_to_j2000(position: SkyPosition, when: datetime) -> SkyPosition:
    """The inverse of :func:`precess_j2000_to_date`.

    Needed because the align module takes a target in J2000 while the safety
    limits are checked in the equinox of date: something has to go back the
    other way, and it should be the same arithmetic run backwards rather than a
    second approximation that disagrees with the first by an arcsecond nobody
    can account for.

    The rotation is inverted by exchanging the two equatorial angles and
    negating all three, which is what transposing the rotation matrix comes to
    (Meeus, chapter 21).
    """
    zeta_deg, z_deg, theta_deg = _precession_angles_deg(when)
    return _rotate_equatorial(position, -z_deg, -zeta_deg, -theta_deg)


def _rotate_equatorial(
    position: SkyPosition, zeta_deg: float, z_deg: float, theta_deg: float
) -> SkyPosition:
    """Meeus equation 21.4, given the three Euler angles in degrees."""
    zeta, z, theta = map(math.radians, (zeta_deg, z_deg, theta_deg))
    ra = math.radians(position.ra_hours * DEGREES_PER_HOUR)
    dec = math.radians(position.dec_deg)

    ra_plus_zeta = ra + zeta
    a = math.cos(dec) * math.sin(ra_plus_zeta)
    b = math.cos(theta) * math.cos(dec) * math.cos(ra_plus_zeta) - math.sin(theta) * math.sin(
        dec
    )
    c = math.sin(theta) * math.cos(dec) * math.cos(ra_plus_zeta) + math.cos(theta) * math.sin(
        dec
    )

    # Near the pole ``asin(c)`` loses precision where ``atan2`` does not: the
    # derivative of the sine is zero there, so the last bits of ``c`` are worth
    # whole arcseconds of declination. The pole is where a polar-alignment solve
    # puts the telescope, so this matters here more than it usually would.
    return SkyPosition(
        ra_hours=_fold(math.degrees(math.atan2(a, b) + z), 360.0) / DEGREES_PER_HOUR,
        dec_deg=math.degrees(math.atan2(c, math.hypot(a, b))),
    )


def greenwich_mean_sidereal_time_deg(when: datetime) -> float:
    """Greenwich mean sidereal time in degrees, in ``[0, 360)``."""
    days = julian_date(when) - JULIAN_DATE_J2000
    centuries = days / DAYS_PER_JULIAN_CENTURY
    degrees = (
        GMST_CONSTANT_DEG
        + GMST_DEGREES_PER_DAY * days
        + GMST_QUADRATIC_DEG * centuries**2
        - centuries**3 / GMST_CUBIC_DIVISOR
    )
    return _fold(degrees, 360.0)


def local_sidereal_time_deg(when: datetime, longitude_deg: float) -> float:
    """Local mean sidereal time in degrees, in ``[0, 360)``.

    ``longitude_deg`` is positive east, matching ``equipment.yaml``.
    """
    return _fold(greenwich_mean_sidereal_time_deg(when) + longitude_deg, 360.0)


def hour_angle_deg(ra_hours: float, when: datetime, longitude_deg: float) -> float:
    """Hour angle of ``ra_hours`` at ``when``, in degrees, in ``[-180, 180)``.

    Sign convention, SPEC section 7.2 and ``docs/meridian-calibration.md``:
    **negative east** of the meridian (rising, not yet crossed), **positive
    west** (setting, already crossed). This is the quantity ``safety.yaml``
    states the meridian limits in, so the convention is not cosmetic.
    """
    local = local_sidereal_time_deg(when, longitude_deg)
    return normalise_angle_deg(local - ra_hours * DEGREES_PER_HOUR)


def normalise_angle_deg(degrees: float) -> float:
    """Fold an angle into ``[-180, 180)``."""
    return _fold(degrees + 180.0, 360.0) - 180.0


def altitude_azimuth_deg(
    hour_angle_degrees: float, dec_deg: float, latitude_deg: float
) -> tuple[float, float]:
    """Altitude and azimuth in degrees for a target at this hour angle.

    Azimuth is measured from north, increasing towards east, in ``[0, 360)``.
    Altitude is geometric: no refraction (see the module docstring).
    """
    hour_angle = math.radians(hour_angle_degrees)
    dec = math.radians(dec_deg)
    latitude = math.radians(latitude_deg)

    sin_altitude = math.sin(latitude) * math.sin(dec) + math.cos(latitude) * math.cos(
        dec
    ) * math.cos(hour_angle)
    altitude = math.asin(max(-1.0, min(1.0, sin_altitude)))

    east = -math.cos(dec) * math.sin(hour_angle)
    north = math.cos(latitude) * math.sin(dec) - math.sin(latitude) * math.cos(dec) * math.cos(
        hour_angle
    )
    azimuth = _fold(math.degrees(math.atan2(east, north)), 360.0)
    return math.degrees(altitude), azimuth


def sun_position(when: datetime) -> SkyPosition:
    """The Sun's apparent geocentric position at ``when``.

    Good to about 0.01 degrees, against a 30-degree avoidance circle.
    """
    days = julian_date(when) - JULIAN_DATE_J2000
    mean_longitude = math.radians(
        (SUN_MEAN_LONGITUDE_DEG + SUN_MEAN_LONGITUDE_DEG_PER_DAY * days) % 360.0
    )
    mean_anomaly = math.radians(
        (SUN_MEAN_ANOMALY_DEG + SUN_MEAN_ANOMALY_DEG_PER_DAY * days) % 360.0
    )
    ecliptic_longitude = (
        mean_longitude
        + math.radians(SUN_CENTRE_FIRST_DEG) * math.sin(mean_anomaly)
        + math.radians(SUN_CENTRE_SECOND_DEG) * math.sin(2.0 * mean_anomaly)
    )
    obliquity = math.radians(OBLIQUITY_DEG - OBLIQUITY_DEG_PER_DAY * days)

    right_ascension = math.atan2(
        math.cos(obliquity) * math.sin(ecliptic_longitude), math.cos(ecliptic_longitude)
    )
    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic_longitude))
    return SkyPosition(
        ra_hours=_fold(math.degrees(right_ascension) / DEGREES_PER_HOUR, HOURS_PER_DAY),
        dec_deg=math.degrees(declination),
    )


def angular_separation_deg(first: SkyPosition, second: SkyPosition) -> float:
    """The angle between two positions, in degrees.

    The Vincenty form, not the cosine rule: the cosine rule loses most of its
    significant digits below a degree or so, which is precisely the regime a
    "is this star still the one we measured" check would work in later.
    """
    ra_first = math.radians(first.ra_hours * DEGREES_PER_HOUR)
    ra_second = math.radians(second.ra_hours * DEGREES_PER_HOUR)
    dec_first = math.radians(first.dec_deg)
    dec_second = math.radians(second.dec_deg)
    delta_ra = ra_second - ra_first

    numerator = math.hypot(
        math.cos(dec_second) * math.sin(delta_ra),
        math.cos(dec_first) * math.sin(dec_second)
        - math.sin(dec_first) * math.cos(dec_second) * math.cos(delta_ra),
    )
    denominator = math.sin(dec_first) * math.sin(dec_second) + math.cos(dec_first) * math.cos(
        dec_second
    ) * math.cos(delta_ra)
    return math.degrees(math.atan2(numerator, denominator))
