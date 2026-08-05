"""Schema for ``config/equipment.yaml`` — SPEC section 5.1.

Nothing in this module is specific to the reference rig of SPEC section 2.1.
Focal lengths, pixel sizes, plate scales and guide scales are all read from the
file; the migration from guide scope to OAG, and the addition of the coma
corrector, are changes to the YAML only (SPEC section 2.1, migration note).
"""

from __future__ import annotations

import zoneinfo
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from .common import ARCSEC_PER_ARCMIN, ARCSEC_PER_RADIAN, StrictModel

#: Filter categories the specification defines (SPEC section 5.1). The
#: vocabulary is closed so that a typo fails loudly; adding narrowband filters
#: is a schema change. See docs/decisions/0004-closed-vocabularies.md.
FilterType = Literal["dark", "luminance", "red", "green", "blue", "empty"]

#: SPEC section 5.1: guide scale differs by an order of magnitude between these.
GuidingMode = Literal["guidescope", "oag"]

#: SPEC section 5.1: USB serial is preferred; WiFi is the documented fallback
#: should the M1 HITL test of the Wave 150i fail (SPEC section 14, M1).
MountConnection = Literal["serial", "wifi"]

PositiveFloat = Annotated[float, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class Site(StrictModel):
    """Observing site — SPEC section 2.3."""

    name: str = Field(min_length=1)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    elevation_m: float = Field(ge=-500.0, le=9000.0)
    timezone: str = Field(min_length=1)

    @field_validator("timezone")
    @classmethod
    def _timezone_must_exist(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value


class OpticalTrain(StrictModel):
    """One optical configuration. Exactly one is active at a time."""

    id: str = Field(min_length=1)
    active: bool
    focal_length_mm: PositiveFloat
    aperture_mm: PositiveFloat
    corrector: str | None = None
    backfocus_mm: PositiveFloat | None = None

    @property
    def focal_ratio(self) -> float:
        """Focal ratio of this train."""
        return self.focal_length_mm / self.aperture_mm


class CoolingConfig(StrictModel):
    """TEC set point and ramp policy — enforced by the governor in M2."""

    target_c: float = Field(ge=-100.0, le=50.0)
    ramp_c_per_min: PositiveFloat
    settle_tolerance_c: PositiveFloat
    settle_timeout_s: PositiveInt


class GainProfile(StrictModel):
    """A measured sensor operating point — SPEC section 15.

    These are measurements, not datasheet figures; see
    docs/sensor-characterisation.md.
    """

    name: str = Field(min_length=1)
    gain: NonNegativeInt
    read_noise_e: PositiveFloat
    full_well_e: PositiveFloat
    e_per_adu: PositiveFloat


class ImagingCamera(StrictModel):
    """Main imaging camera — SPEC section 5.1."""

    indi_driver: str = Field(min_length=1)
    name: str = Field(min_length=1)
    pixel_size_um: PositiveFloat
    width_px: PositiveInt
    height_px: PositiveInt
    bit_depth: int = Field(gt=0, le=32)
    mono: bool
    cooled: bool
    cooling: CoolingConfig
    gain_profiles: tuple[GainProfile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _gain_profile_names_are_unique(self) -> Self:
        names = [profile.name for profile in self.gain_profiles]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate gain profile name(s): {', '.join(duplicates)}")
        return self

    def gain_profile(self, name: str) -> GainProfile:
        """Return the named gain profile, or raise ``KeyError``."""
        for profile in self.gain_profiles:
            if profile.name == name:
                return profile
        raise KeyError(f"no gain profile named {name!r}")


class FilterSlot(StrictModel):
    """One filter wheel position."""

    name: str | None = None
    type: FilterType
    offset_steps: int = 0

    @model_validator(mode="after")
    def _name_matches_type(self) -> Self:
        if self.type == "empty" and self.name is not None:
            raise ValueError("a slot of type 'empty' must not carry a name")
        if self.type != "empty" and self.name is None:
            raise ValueError(f"a slot of type {self.type!r} requires a name")
        return self


class FilterWheel(StrictModel):
    """Filter wheel — SPEC section 5.1."""

    indi_driver: str = Field(min_length=1)
    slots: dict[int, FilterSlot] = Field(min_length=1)

    @model_validator(mode="after")
    def _slots_are_contiguous_from_one(self) -> Self:
        positions = sorted(self.slots)
        if positions != list(range(1, len(positions) + 1)):
            raise ValueError(
                f"filter wheel slots must be numbered contiguously from 1, got {positions}"
            )
        return self

    @model_validator(mode="after")
    def _a_dark_slot_exists(self) -> Self:
        """SPEC section 10.2 captures darks and dark-flats with the dark slot."""
        if not any(slot.type == "dark" for slot in self.slots.values()):
            raise ValueError(
                "one slot must be of type 'dark'; it is required for the dark and "
                "dark-flat calibration frames of SPEC section 10.2"
            )
        return self

    @property
    def populated_slots(self) -> dict[int, FilterSlot]:
        """Slots that carry a filter, keyed by position."""
        return {
            position: slot
            for position, slot in sorted(self.slots.items())
            if slot.type != "empty"
        }


class TemperatureCompensation(StrictModel):
    """Focuser temperature compensation — disabled until measured (SPEC section 15)."""

    enabled: bool
    coefficient_steps_per_c: float | None = None

    @model_validator(mode="after")
    def _enabled_requires_a_measured_coefficient(self) -> Self:
        if self.enabled and self.coefficient_steps_per_c is None:
            raise ValueError(
                "coefficient_steps_per_c must be measured before temperature "
                "compensation can be enabled"
            )
        return self


class RefocusTriggers(StrictModel):
    """Deterministic refocus triggers — SPEC sections 5.1 and 8.1."""

    delta_temperature_c: PositiveFloat
    hfd_drift_percent: float = Field(gt=0.0, le=100.0)
    elapsed_minutes: PositiveInt
    on_filter_change: bool


class Focuser(StrictModel):
    """Focuser — SPEC section 5.1."""

    indi_driver: str = Field(min_length=1)
    backlash_steps: NonNegativeInt
    step_size_um: PositiveFloat | None = None
    max_position: PositiveInt
    temperature_compensation: TemperatureCompensation
    refocus_triggers: RefocusTriggers


class GuideCamera(StrictModel):
    """Guide camera — SPEC section 5.1."""

    indi_driver: str = Field(min_length=1)
    name: str = Field(min_length=1)
    pixel_size_um: PositiveFloat
    width_px: PositiveInt
    height_px: PositiveInt


class DitherConfig(StrictModel):
    """Dither policy — SPEC section 5.1."""

    enabled: bool
    pixels: PositiveFloat
    settle_tolerance_px: PositiveFloat
    settle_timeout_s: PositiveInt
    every_n_frames: PositiveInt


class GuideThresholds(StrictModel):
    """Guiding quality thresholds — SPEC section 5.1."""

    rms_warn_arcsec: PositiveFloat
    rms_abort_arcsec: PositiveFloat
    max_consecutive_lost_frames: PositiveInt

    @model_validator(mode="after")
    def _warn_precedes_abort(self) -> Self:
        if self.rms_warn_arcsec >= self.rms_abort_arcsec:
            raise ValueError(
                "rms_warn_arcsec must be below rms_abort_arcsec, otherwise the "
                "session aborts before it ever warns"
            )
        return self


class Guiding(StrictModel):
    """Guiding configuration — SPEC section 5.1.

    Everything here changes on the OAG migration. No value in this block may be
    duplicated as a constant anywhere in the code.
    """

    mode: GuidingMode
    camera: GuideCamera
    focal_length_mm: PositiveFloat
    exposure_s: PositiveFloat
    calibration_step_ms: PositiveInt
    dither: DitherConfig
    thresholds: GuideThresholds


class Mount(StrictModel):
    """Mount — SPEC section 5.1. The M1 HITL subject."""

    indi_driver: str = Field(min_length=1)
    device_label: str = Field(min_length=1)
    connection: MountConnection
    port: str | None = None
    baud: PositiveInt
    slew_rate_max_deg_s: PositiveFloat
    counterweight_fitted: bool

    @model_validator(mode="after")
    def _serial_connection_requires_a_port(self) -> Self:
        if self.connection == "serial" and not self.port:
            raise ValueError("port is required when connection is 'serial'")
        return self


class EquipmentConfig(StrictModel):
    """Root model for ``config/equipment.yaml``."""

    site: Site
    optical_trains: tuple[OpticalTrain, ...] = Field(min_length=1)
    imaging_camera: ImagingCamera
    filter_wheel: FilterWheel
    focuser: Focuser
    guiding: Guiding
    mount: Mount

    @model_validator(mode="after")
    def _optical_train_ids_are_unique(self) -> Self:
        ids = [train.id for train in self.optical_trains]
        duplicates = sorted({train_id for train_id in ids if ids.count(train_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate optical train id(s): {', '.join(duplicates)}")
        return self

    @model_validator(mode="after")
    def _exactly_one_optical_train_is_active(self) -> Self:
        active = [train.id for train in self.optical_trains if train.active]
        if len(active) != 1:
            raise ValueError(
                "there must be exactly one active optical train, "
                f"found {len(active)}: {active or 'none'}"
            )
        return self

    @property
    def active_optical_train(self) -> OpticalTrain:
        """The optical train currently in use."""
        for train in self.optical_trains:
            if train.active:
                return train
        # Unreachable: _exactly_one_optical_train_is_active guarantees one exists.
        raise AssertionError("no active optical train")

    def imaging_plate_scale_arcsec_per_px(self) -> float:
        """Nominal imaging plate scale, derived from the active train.

        SPEC section 2.1 marks this as nominal: it must be superseded by
        plate-solve-derived values once solving is available (M2).
        """
        return (
            self.imaging_camera.pixel_size_um
            / self.active_optical_train.focal_length_mm
            * ARCSEC_PER_RADIAN
            / 1000.0
        )

    def imaging_fov_arcmin(self) -> tuple[float, float]:
        """Nominal imaging field of view as ``(width, height)`` in arcminutes."""
        scale = self.imaging_plate_scale_arcsec_per_px()
        return (
            self.imaging_camera.width_px * scale / ARCSEC_PER_ARCMIN,
            self.imaging_camera.height_px * scale / ARCSEC_PER_ARCMIN,
        )

    def guide_plate_scale_arcsec_per_px(self) -> float:
        """Guide plate scale, derived from ``guiding.focal_length_mm``.

        On the OAG migration this follows the configuration file with no code
        change (SPEC section 2.1, migration note).
        """
        return (
            self.guiding.camera.pixel_size_um
            / self.guiding.focal_length_mm
            * ARCSEC_PER_RADIAN
            / 1000.0
        )
