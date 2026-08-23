"""Where the measured meridian calibration lives — ADR 0021, SPEC sections 4 and 9.1.

The limits of SPEC section 9.1 are measured on the rig, not chosen. Until now
the only place to put a measurement was ``config/safety.yaml``, typed in by
hand, and nothing in Nocturne may write configuration (CLAUDE.md invariant 2).
That rule does not bend: what changes is that a *measurement with provenance* is
not configuration. It is a record of something that happened at a place and a
time, and it belongs in a table.

Three properties this module exists to guarantee:

**Append-only.** There is no UPDATE and no DELETE, anywhere. Measuring again
records a new profile; the old one stays as evidence of what was enforced last
week. ``test_store_calibration.py`` asserts that against the source text, with a
planted offender to prove the check can fail.

**Bound to where it was measured.** The mount's polar axis is tilted to the site
latitude, so the arc the tube sweeps past the tripod legs is a function of it. A
profile measured at one latitude is not "close enough" at another — it does not
apply, and :meth:`CalibrationStore.latest_for` returns nothing. SPEC section
9.1.7 applies the same reasoning to the counterweight; the optical train is
here for the same reason, because the camera corner is usually what reaches a
leg first.

**All of the evidence, two of the numbers.** Only the derived pair is enforced.
Every mark is kept with the declination, the altitude and the pier side it was
taken at, because the shape of this limit is not finally settled (ADR 0020): if
the measurements turn out to depend strongly on declination, modelling that
properly must not require going back out to the terrace.

This module writes to disk. It is the only module in the package that does, it
is allowlisted for it in ``test_safety_boundaries.py``, and it writes nothing
but its own database.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, final

#: Where the database lives when nobody says otherwise. Not a configuration
#: value: it is machine state, not a tuning knob, and SPEC section 5 lists three
#: configuration files rather than four. ``NOCTURNE_DATA_DIR`` overrides it, and
#: every test passes an explicit path so that none of them can reach this.
_DATA_DIR_VARIABLE: Final = "NOCTURNE_DATA_DIR"
_XDG_VARIABLE: Final = "XDG_DATA_HOME"
_DATABASE_NAME: Final = "nocturne.db"

#: Which side of the meridian a mark was taken on.
SIDES: Final = ("east", "west")

#: Bumped when the tables below change shape. There is no migration path yet and
#: there is nothing to migrate: a store from an older version is refused rather
#: than half-read.
SCHEMA_VERSION: Final = 1

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meridian_profile (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at          TEXT    NOT NULL,
    latitude_deg         REAL    NOT NULL,
    longitude_deg        REAL    NOT NULL,
    counterweight_fitted INTEGER NOT NULL,
    optical_train_id     TEXT    NOT NULL,
    hour_angle_east_deg  REAL    NOT NULL,
    hour_angle_west_deg  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS meridian_mark (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      INTEGER NOT NULL REFERENCES meridian_profile(id),
    marked_at       TEXT    NOT NULL,
    side            TEXT    NOT NULL CHECK (side IN ('east', 'west')),
    hour_angle_deg  REAL    NOT NULL,
    declination_deg REAL    NOT NULL,
    altitude_deg    REAL    NOT NULL,
    pier_side       TEXT
);

CREATE INDEX IF NOT EXISTS meridian_profile_recorded_at
    ON meridian_profile (recorded_at DESC);
"""


def default_database_path() -> Path:
    """Where the store lives on this machine.

    ``NOCTURNE_DATA_DIR`` wins; otherwise the XDG data directory. This is the
    state database of SPEC section 4, not the image archive — FITS goes on the
    NVMe drive of SPEC section 2.2 and gets its own decision when M3 needs it.
    """
    override = os.environ.get(_DATA_DIR_VARIABLE)
    if override:
        return Path(override) / _DATABASE_NAME
    xdg = os.environ.get(_XDG_VARIABLE)
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "nocturne" / _DATABASE_NAME


@final
@dataclass(frozen=True, slots=True)
class MeridianMark:
    """One point where the operator judged the clearance to have run out.

    ``hour_angle_deg`` is negative east of the meridian and positive west of it
    (SPEC section 7.2). The declination, altitude and pier side are provenance:
    nothing enforces them, and they are what a later model would be built from.
    """

    side: str
    hour_angle_deg: float
    declination_deg: float
    altitude_deg: float
    pier_side: str | None
    marked_at: datetime


@final
@dataclass(frozen=True, slots=True)
class RecordedCalibration:
    """A calibration as it comes off the tool, before it is stored."""

    recorded_at: datetime
    latitude_deg: float
    longitude_deg: float
    counterweight_fitted: bool
    optical_train_id: str
    marks: tuple[MeridianMark, ...]


@final
@dataclass(frozen=True, slots=True)
class MeridianProfile:
    """A stored calibration: its provenance, its marks, and the derived band.

    ``hour_angle_east_deg`` and ``hour_angle_west_deg`` bracket the band the tube
    may **not** enter (ADR 0020). They are derived from the marks and are not
    separately editable — there is no setter, and no UPDATE.
    """

    id: int
    recorded_at: datetime
    latitude_deg: float
    longitude_deg: float
    counterweight_fitted: bool
    optical_train_id: str
    hour_angle_east_deg: float
    hour_angle_west_deg: float
    marks: tuple[MeridianMark, ...]


def derive_band_deg(marks: Iterable[MeridianMark]) -> tuple[float, float]:
    """The enforced band from the marks: the most conservative of each side.

    The marks say where clearance ran out at several declinations. One pair is
    enforced at every declination (``docs/meridian-calibration.md``), so the
    widest band wins — the earliest the tube ever ran out of room on the way in,
    and the latest it ever regained it on the way out.

    Raises:
        ValueError: if either side has no mark, if a mark is on the wrong side
            of the meridian for the side it claims, or if a side is not one of
            :data:`SIDES`. Half a band is not half a limit: the unmarked side
            would be unbounded, and that is the direction that breaks glass.
    """
    collected: dict[str, list[float]] = {side: [] for side in SIDES}
    for one in marks:
        if one.side not in collected:
            raise ValueError(f"unknown side {one.side!r}; expected one of {SIDES}")
        if one.side == "east" and one.hour_angle_deg >= 0.0:
            raise ValueError(
                f"an east mark must be at a negative hour angle, got {one.hour_angle_deg}"
            )
        if one.side == "west" and one.hour_angle_deg <= 0.0:
            raise ValueError(
                f"a west mark must be at a positive hour angle, got {one.hour_angle_deg}"
            )
        collected[one.side].append(one.hour_angle_deg)

    for side in SIDES:
        if not collected[side]:
            raise ValueError(
                f"no mark on the {side} side of the meridian. Both sides are measured "
                "before a calibration means anything (docs/meridian-calibration.md)"
            )
    return min(collected["east"]), max(collected["west"])


@final
class CalibrationStore:
    """The measured calibrations, on disk. Append-only."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            stored = connection.execute("SELECT version FROM schema_version").fetchone()
            if stored is None:
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif int(stored[0]) != SCHEMA_VERSION:
                raise ValueError(
                    f"{path} was written by schema version {stored[0]}, and this is "
                    f"version {SCHEMA_VERSION}. There is no migration; move the file "
                    "aside and recalibrate rather than half-reading it"
                )

    @property
    def path(self) -> Path:
        """Where the database lives."""
        return self._path

    def record(self, calibration: RecordedCalibration) -> int:
        """Store a calibration and return its id.

        The band is derived here rather than taken from the caller, so that
        nothing can store a pair that its own marks do not support.
        """
        east, west = derive_band_deg(calibration.marks)
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO meridian_profile ("
                " recorded_at, latitude_deg, longitude_deg, counterweight_fitted,"
                " optical_train_id, hour_angle_east_deg, hour_angle_west_deg"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _to_text(calibration.recorded_at),
                    calibration.latitude_deg,
                    calibration.longitude_deg,
                    int(calibration.counterweight_fitted),
                    calibration.optical_train_id,
                    east,
                    west,
                ),
            )
            profile_id = int(cursor.lastrowid or 0)
            connection.executemany(
                "INSERT INTO meridian_mark ("
                " profile_id, marked_at, side, hour_angle_deg, declination_deg,"
                " altitude_deg, pier_side"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        profile_id,
                        _to_text(one.marked_at),
                        one.side,
                        one.hour_angle_deg,
                        one.declination_deg,
                        one.altitude_deg,
                        one.pier_side,
                    )
                    for one in calibration.marks
                ],
            )
        return profile_id

    def profiles(self) -> tuple[MeridianProfile, ...]:
        """Every stored calibration, newest first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, recorded_at, latitude_deg, longitude_deg,"
                " counterweight_fitted, optical_train_id, hour_angle_east_deg,"
                " hour_angle_west_deg FROM meridian_profile"
                " ORDER BY recorded_at DESC, id DESC"
            ).fetchall()
            return tuple(self._profile(connection, row) for row in rows)

    def latest_for(
        self,
        *,
        latitude_deg: float,
        latitude_tolerance_deg: float,
        counterweight_fitted: bool,
        optical_train_id: str,
    ) -> MeridianProfile | None:
        """The newest calibration that applies to the rig as it stands now.

        ``None`` means there is none — not that the nearest one is close enough.
        A caller that treats a missing profile as permission has misunderstood
        this method; the governor treats it as uncalibrated (ADR 0019).
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, recorded_at, latitude_deg, longitude_deg,"
                " counterweight_fitted, optical_train_id, hour_angle_east_deg,"
                " hour_angle_west_deg FROM meridian_profile"
                " WHERE ABS(latitude_deg - ?) <= ?"
                "   AND counterweight_fitted = ?"
                "   AND optical_train_id = ?"
                " ORDER BY recorded_at DESC, id DESC LIMIT 1",
                (
                    latitude_deg,
                    latitude_tolerance_deg,
                    int(counterweight_fitted),
                    optical_train_id,
                ),
            ).fetchone()
            if row is None:
                return None
            return self._profile(connection, row)

    def _profile(self, connection: sqlite3.Connection, row: sqlite3.Row) -> MeridianProfile:
        profile_id = int(row["id"])
        marks = connection.execute(
            "SELECT side, hour_angle_deg, declination_deg, altitude_deg, pier_side,"
            " marked_at FROM meridian_mark WHERE profile_id = ? ORDER BY id",
            (profile_id,),
        ).fetchall()
        return MeridianProfile(
            id=profile_id,
            recorded_at=_from_text(str(row["recorded_at"])),
            latitude_deg=float(row["latitude_deg"]),
            longitude_deg=float(row["longitude_deg"]),
            counterweight_fitted=bool(row["counterweight_fitted"]),
            optical_train_id=str(row["optical_train_id"]),
            hour_angle_east_deg=float(row["hour_angle_east_deg"]),
            hour_angle_west_deg=float(row["hour_angle_west_deg"]),
            marks=tuple(
                MeridianMark(
                    side=str(mark_row["side"]),
                    hour_angle_deg=float(mark_row["hour_angle_deg"]),
                    declination_deg=float(mark_row["declination_deg"]),
                    altitude_deg=float(mark_row["altitude_deg"]),
                    pier_side=(
                        None if mark_row["pier_side"] is None else str(mark_row["pier_side"])
                    ),
                    marked_at=_from_text(str(mark_row["marked_at"])),
                )
                for mark_row in marks
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        # Columns by name. The positional form reads fine until a column is
        # inserted in the middle of a SELECT and every field below shifts by one
        # without anything failing.
        connection.row_factory = sqlite3.Row
        # WAL per SPEC section 4: a reader must never block on the writer, and a
        # session reading its own calibration while the tool records another one
        # is exactly that case.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _to_text(when: datetime) -> str:
    """Timestamps are stored as ISO-8601 in UTC, which sorts lexicographically."""
    if when.tzinfo is None:
        raise ValueError("a stored timestamp must be timezone-aware")
    return when.astimezone(UTC).isoformat()


def _from_text(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


__all__ = [
    "SCHEMA_VERSION",
    "SIDES",
    "CalibrationStore",
    "MeridianMark",
    "MeridianProfile",
    "RecordedCalibration",
    "default_database_path",
    "derive_band_deg",
]
