"""The measured meridian calibration, and where it is kept — ADR 0021.

The limits stop being something a human types into ``safety.yaml`` and become
something the calibration tool measures. They still may not be written to
configuration — nothing writes configuration, ever (CLAUDE.md invariant 2) — so
they land in a store of their own, and this is that store.

Three properties carry the safety weight, and each has a test that fails when
it stops holding:

* **Append-only.** A profile is never edited. Measuring again writes a new one
  and the old stays as evidence.
* **Bound to where it was measured.** The mount's polar axis is tilted to the
  site latitude, so a calibration is only valid near it. A profile that does not
  match the current site is not "close enough"; it does not apply.
* **The derived band is the widest of what was marked.** The marks say where
  clearance ran out at several declinations; the enforced pair is the most
  conservative of them, computed here rather than in the operator's head.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nocturne.store import (
    CalibrationStore,
    MeridianMark,
    RecordedCalibration,
    derive_band_deg,
)

NOW = datetime(2026, 8, 23, 11, 0, tzinfo=UTC)
TRAIN = "native"


def mark(
    side: str,
    hour_angle_deg: float,
    dec_deg: float = 20.0,
    altitude_deg: float = 60.0,
    pier_side: str | None = "west",
) -> MeridianMark:
    return MeridianMark(
        side=side,
        hour_angle_deg=hour_angle_deg,
        declination_deg=dec_deg,
        altitude_deg=altitude_deg,
        pier_side=pier_side,
        marked_at=NOW,
    )


def calibration(
    *,
    latitude_deg: float = 41.6,
    counterweight_fitted: bool = False,
    optical_train_id: str = TRAIN,
    recorded_at: datetime = NOW,
    marks: tuple[MeridianMark, ...] = (mark("east", -25.0), mark("west", 20.0)),
) -> RecordedCalibration:
    return RecordedCalibration(
        recorded_at=recorded_at,
        latitude_deg=latitude_deg,
        longitude_deg=-3.7,
        counterweight_fitted=counterweight_fitted,
        optical_train_id=optical_train_id,
        marks=marks,
    )


@pytest.fixture
def store(tmp_path: Path) -> CalibrationStore:
    return CalibrationStore(tmp_path / "nocturne.db")


class TestDerivingTheBand:
    """SPEC section 9.1 and ADR 0020: the pair brackets what is forbidden."""

    def test_the_widest_mark_on_each_side_wins(self) -> None:
        """Clearance ran out earliest at one declination. That is the number."""
        east, west = derive_band_deg(
            (
                mark("east", -18.0, dec_deg=60.0),
                mark("east", -25.0, dec_deg=20.0),
                mark("west", 20.0, dec_deg=20.0),
                mark("west", 26.0, dec_deg=60.0),
            )
        )
        assert east == -25.0
        assert west == 26.0

    def test_a_single_mark_per_side_is_enough(self) -> None:
        assert derive_band_deg((mark("east", -22.0), mark("west", 19.0))) == (-22.0, 19.0)

    @pytest.mark.parametrize(
        ("marks", "missing"),
        [
            ((mark("east", -22.0),), "west"),
            ((mark("west", 19.0),), "east"),
            ((), "east"),
        ],
    )
    def test_a_side_with_no_mark_is_refused(
        self, marks: tuple[MeridianMark, ...], missing: str
    ) -> None:
        """Half a band is not half a limit: the unmarked side would be unbounded."""
        with pytest.raises(ValueError, match=missing):
            derive_band_deg(marks)

    def test_an_east_mark_must_be_east(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            derive_band_deg((mark("east", 5.0), mark("west", 19.0)))

    def test_a_west_mark_must_be_west(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            derive_band_deg((mark("west", -5.0), mark("east", -19.0)))

    def test_an_unknown_side_is_refused(self) -> None:
        with pytest.raises(ValueError, match="side"):
            derive_band_deg((mark("up", -5.0), mark("west", 19.0)))


class TestRecordingAndReading:
    def test_a_new_store_holds_nothing(self, store: CalibrationStore) -> None:
        assert store.profiles() == ()

    def test_a_recorded_profile_comes_back_with_its_derived_band(
        self, store: CalibrationStore
    ) -> None:
        store.record(calibration())
        (profile,) = store.profiles()
        assert profile.hour_angle_east_deg == -25.0
        assert profile.hour_angle_west_deg == 20.0
        assert profile.latitude_deg == 41.6
        assert profile.optical_train_id == TRAIN
        assert profile.counterweight_fitted is False

    def test_every_mark_is_kept_with_what_it_was_measured_at(
        self, store: CalibrationStore
    ) -> None:
        """Only two numbers are enforced; all of the evidence is stored (ADR 0020)."""
        store.record(
            calibration(
                marks=(
                    mark("east", -25.0, dec_deg=20.0, altitude_deg=68.4, pier_side="west"),
                    mark("west", 20.0, dec_deg=60.0, altitude_deg=71.2, pier_side="east"),
                )
            )
        )
        (profile,) = store.profiles()
        assert len(profile.marks) == 2
        east_mark = next(one for one in profile.marks if one.side == "east")
        assert east_mark.declination_deg == 20.0
        assert east_mark.altitude_deg == 68.4
        assert east_mark.pier_side == "west"
        assert east_mark.marked_at == NOW

    def test_it_survives_being_reopened(self, tmp_path: Path) -> None:
        """The process boundary is what fails, so the process boundary is the test."""
        path = tmp_path / "nocturne.db"
        CalibrationStore(path).record(calibration())
        assert len(CalibrationStore(path).profiles()) == 1

    def test_profiles_come_back_newest_first(self, store: CalibrationStore) -> None:
        store.record(calibration(recorded_at=NOW - timedelta(days=2)))
        store.record(calibration(recorded_at=NOW))
        first, second = store.profiles()
        assert first.recorded_at > second.recorded_at

    def test_the_database_is_in_wal_mode(self, store: CalibrationStore) -> None:
        """SPEC section 4. A reader must never block on the writer mid-session."""
        with sqlite3.connect(store.path) as connection:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


class TestMatchingTheCurrentSite:
    """A calibration measured somewhere else does not apply here."""

    def test_the_same_latitude_matches(self, store: CalibrationStore) -> None:
        store.record(calibration(latitude_deg=41.6))
        found = store.latest_for(
            latitude_deg=41.6,
            latitude_tolerance_deg=0.5,
            counterweight_fitted=False,
            optical_train_id=TRAIN,
        )
        assert found is not None
        assert found.hour_angle_east_deg == -25.0

    def test_a_latitude_inside_the_tolerance_matches(self, store: CalibrationStore) -> None:
        store.record(calibration(latitude_deg=41.6))
        assert (
            store.latest_for(
                latitude_deg=41.9,
                latitude_tolerance_deg=0.5,
                counterweight_fitted=False,
                optical_train_id=TRAIN,
            )
            is not None
        )

    def test_a_latitude_outside_the_tolerance_does_not(self, store: CalibrationStore) -> None:
        """The polar axis is tilted to the latitude; the arc past the legs moves with it."""
        store.record(calibration(latitude_deg=41.6))
        assert (
            store.latest_for(
                latitude_deg=37.0,
                latitude_tolerance_deg=0.5,
                counterweight_fitted=False,
                optical_train_id=TRAIN,
            )
            is None
        )

    def test_a_different_counterweight_state_does_not(self, store: CalibrationStore) -> None:
        """SPEC section 9.1.7 says so outright."""
        store.record(calibration(counterweight_fitted=False))
        assert (
            store.latest_for(
                latitude_deg=41.6,
                latitude_tolerance_deg=0.5,
                counterweight_fitted=True,
                optical_train_id=TRAIN,
            )
            is None
        )

    def test_a_different_optical_train_does_not(self, store: CalibrationStore) -> None:
        """A coma corrector moves the camera, and the camera corner is what hits."""
        store.record(calibration(optical_train_id="native"))
        assert (
            store.latest_for(
                latitude_deg=41.6,
                latitude_tolerance_deg=0.5,
                counterweight_fitted=False,
                optical_train_id="mpcc",
            )
            is None
        )

    def test_the_newest_matching_profile_wins(self, store: CalibrationStore) -> None:
        store.record(
            calibration(
                recorded_at=NOW - timedelta(days=30),
                marks=(mark("east", -40.0), mark("west", 40.0)),
            )
        )
        store.record(calibration(recorded_at=NOW))
        found = store.latest_for(
            latitude_deg=41.6,
            latitude_tolerance_deg=0.5,
            counterweight_fitted=False,
            optical_train_id=TRAIN,
        )
        assert found is not None
        assert found.hour_angle_east_deg == -25.0, "the older, wider profile was superseded"

    def test_an_empty_store_matches_nothing(self, store: CalibrationStore) -> None:
        assert (
            store.latest_for(
                latitude_deg=41.6,
                latitude_tolerance_deg=0.5,
                counterweight_fitted=False,
                optical_train_id=TRAIN,
            )
            is None
        )


class TestNothingIsEverRewritten:
    """Append-only, asserted against the source rather than against behaviour."""

    def test_measuring_again_adds_rather_than_replaces(self, store: CalibrationStore) -> None:
        store.record(calibration(recorded_at=NOW - timedelta(days=1)))
        store.record(calibration(recorded_at=NOW))
        assert len(store.profiles()) == 2

    def test_the_store_issues_no_statement_that_can_lose_a_row(self) -> None:
        offenders = _destructive_statements_in(_store_sources())
        assert offenders == [], f"the calibration store must be append-only; found {offenders}"

    def test_the_detector_finds_a_planted_offender(self) -> None:
        """Positive control: the check above passes by finding nothing."""
        planted = (
            'def wrong() -> None:\n    cursor.execute("UPDATE meridian_profile SET x=1")\n'
        )
        assert _destructive_statements_in({"planted.py": planted}) == ["UPDATE"]

    def test_the_scan_reads_the_real_package(self) -> None:
        sources = _store_sources()
        assert sources, "no store modules were scanned, so the check above proves nothing"
        assert any("INSERT INTO" in text for text in sources.values()), (
            "the scanned sources contain no SQL at all; the detector is pointed somewhere wrong"
        )


#: Statements that can destroy a recorded measurement.
DESTRUCTIVE_SQL = ("UPDATE", "DELETE", "DROP", "TRUNCATE", "REPLACE INTO")

#: The sqlite3 methods that run SQL. Scanning what is *handed to these* rather
#: than every string in the file is what keeps the check honest: this module's
#: own docstring says the words UPDATE and DELETE, because it explains that it
#: does not use them, and a detector that cannot tell prose from a statement
#: would have to be silenced — which is how a detector stops detecting.
EXECUTING_METHODS = frozenset({"execute", "executemany", "executescript"})


def _store_sources() -> dict[str, str]:
    from nocturne import store

    package = Path(inspect.getfile(store)).parent
    return {path.name: path.read_text(encoding="utf-8") for path in package.glob("*.py")}


def _destructive_statements_in(sources: dict[str, str]) -> list[str]:
    """Destructive SQL handed to anything that executes it."""
    found: list[str] = []
    for text in sources.values():
        for node in ast.walk(ast.parse(text)):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr not in EXECUTING_METHODS
            ):
                continue
            for argument in ast.walk(node):
                if not isinstance(argument, ast.Constant) or not isinstance(
                    argument.value, str
                ):
                    continue
                upper = " ".join(argument.value.upper().split())
                found.extend(statement for statement in DESTRUCTIVE_SQL if statement in upper)
    return sorted(set(found))
