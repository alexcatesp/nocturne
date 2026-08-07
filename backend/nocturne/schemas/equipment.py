"""Schema for ``config/equipment.yaml`` — SPEC section 5.1.

Nothing in this module is specific to the reference rig of SPEC section 2.1.
Focal lengths, pixel sizes, plate scales and guide scales are all read from the
file; the migration from guide scope to OAG, and the addition of the coma
corrector, are changes to the YAML only (SPEC section 2.1, migration note).
"""

from __future__ import annotations

import re
import zoneinfo
from typing import Annotated, Final, Literal, Self

from pydantic import Field, field_validator, model_validator

from .common import ARCSEC_PER_ARCMIN, ARCSEC_PER_RADIAN, StrictModel

#: Filter categories the specification defines (SPEC section 5.1). The
#: vocabulary is closed so that a typo fails loudly; adding narrowband filters
#: is a schema change. See docs/decisions/0004-closed-vocabularies.md.
FilterType = Literal["dark", "luminance", "red", "green", "blue", "empty"]

#: SPEC section 5.1: guide scale differs by an order of magnitude between these.
GuidingMode = Literal["guidescope", "oag"]

#: SPEC section 5.1: USB serial is preferred. The WiFi fallback was the
#: documented alternative had the M1 HITL test failed; it passed on 2026-08
#: over direct USB serial, so WiFi is retained only as a contingency
#: (docs/decisions/0011-m1-mount-link-verified.md).
MountConnection = Literal["serial", "wifi"]

#: Serial device paths the schema accepts.
#:
#: The Wave 150i presents an STM32 virtual COM port and the kernel binds
#: ``cdc_acm``, so it enumerates as /dev/ttyACM0 — there is no FTDI, CH340 or
#: CP210x bridge chip, and anything looking only for ttyUSB* will miss it
#: entirely. Measured on the reference rig; see docs/FIELD-NOTES-M1.md section
#: 2.1 and backend/tests/fixtures/hardware/wave150i-properties.txt.
_STABLE_PORT_PREFIXES: Final = ("/dev/serial/by-id/", "/dev/serial/by-path/")
_BARE_PORT_PATTERN: Final = re.compile(r"^/dev/tty(ACM|USB)\d+$")

#: The site shipped in ``config/equipment.yaml``. Deliberately generic.
#:
#: A residential observing site's coordinates to four decimal places are
#: somebody's home address to within metres. Published in a repository next to
#: an inventory of expensive equipment left outside overnight and a schedule of
#: when nobody is watching it, that is not a detail — so the reference
#: configuration ships a round-numbered mid-latitude point that belongs to
#: nobody, and the operator replaces it before the first session.
#:
#: 45.0 N, 0.0 E is in open country in south-west France. It is a real place
#: only in the sense that every point is; nothing observes from it.
PLACEHOLDER_LATITUDE: Final = 45.0
PLACEHOLDER_LONGITUDE: Final = 0.0
PLACEHOLDER_ELEVATION_M: Final = 100.0

#: How close counts as "still the placeholder", in degrees.
#:
#: This asks "has the file been edited?", not "is this site near 45N 0E?", so
#: the tolerance is tiny: 1e-6 degrees is about 10 cm. Any coordinate a person
#: typed clears it. It is not exact equality only so that 45, 45.0 and 45.000
#: cannot be told apart by a rounding artefact.
PLACEHOLDER_TOLERANCE_DEG: Final = 1e-6

PositiveFloat = Annotated[float, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class IndiDevice(StrictModel):
    """A device Nocturne addresses over INDI.

    ``indi_device_name`` is the name the *driver* announces itself under, which
    is equipment-dependent and therefore configuration rather than a constant
    (CLAUDE.md section 6). All five were read off the running drivers on
    2026-08-07; see docs/FIELD-NOTES-M1.md sections 1 and 9.
    """

    indi_driver: str = Field(min_length=1)
    indi_device_name: str = Field(min_length=1)

    @field_validator("indi_device_name")
    @classmethod
    def _device_name_is_not_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class Site(StrictModel):
    """Observing site — SPEC section 2.3."""

    name: str = Field(min_length=1)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    elevation_m: float = Field(ge=-500.0, le=9000.0)
    timezone: str = Field(min_length=1)

    @property
    def is_placeholder(self) -> bool:
        """Whether the coordinates are still the ones the repository ships.

        Coordinates only, not the name: a wrong label is cosmetic, wrong
        coordinates silently produce the wrong ephemeris, the wrong altitude
        windows and the wrong meridian timing all night.
        """
        return (
            abs(self.latitude - PLACEHOLDER_LATITUDE) < PLACEHOLDER_TOLERANCE_DEG
            and abs(self.longitude - PLACEHOLDER_LONGITUDE) < PLACEHOLDER_TOLERANCE_DEG
        )

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


class ImagingCamera(IndiDevice):
    """Main imaging camera — SPEC section 5.1."""

    name: str = Field(min_length=1)
    pixel_size_um: PositiveFloat
    width_px: PositiveInt
    height_px: PositiveInt
    bit_depth: int = Field(gt=0, le=32)
    mono: bool
    cooled: bool
    cooling: CoolingConfig
    #: Written to CCD_CONTROLS.Offset on connect. The driver ships its own
    #: default — 1 on the 533MM — which is not our configuration
    #: (docs/FIELD-NOTES-M1.md section 11).
    offset: NonNegativeInt
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


class FilterWheel(IndiDevice):
    """Filter wheel — SPEC section 5.1.

    This is the source of truth for what is physically in the wheel. The
    driver's own FILTER_NAME values are ZWO factory defaults that match nothing
    in it, and Nocturne overwrites them on connect rather than reading them
    (docs/FIELD-NOTES-M1.md section 10.2).
    """

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


class Focuser(IndiDevice):
    """Focuser — SPEC section 5.1."""

    #: Unset until measured. The EAF ships FOCUS_BACKLASH_STEPS=180, which is a
    #: ZWO default; the 0 that used to be configured here was worse, because it
    #: looked like a measurement (docs/FIELD-NOTES-M1.md section 12).
    backlash_steps: NonNegativeInt | None = None
    step_size_um: PositiveFloat | None = None
    #: Unset means "whatever the driver reports", which is authoritative because
    #: it comes from the hardware — FOCUS_MAX.FOCUS_MAX_VALUE, 100000 on the EAF.
    #: Set it only to impose a TIGHTER travel limit than the driver's; bring-up
    #: refuses a value above the driver's rather than clamping silently.
    max_position: PositiveInt | None = None
    temperature_compensation: TemperatureCompensation
    refocus_triggers: RefocusTriggers


class GuideCamera(IndiDevice):
    """Guide camera — SPEC section 5.1."""

    name: str = Field(min_length=1)
    pixel_size_um: PositiveFloat
    width_px: PositiveInt
    height_px: PositiveInt
    #: Written to CCD_CONTROLS on connect (docs/FIELD-NOTES-M1.md section 11).
    gain: NonNegativeInt
    offset: NonNegativeInt


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


class Mount(IndiDevice):
    """Mount — SPEC section 5.1. The M1 HITL subject."""

    #: What the operator calls it. Display only, and translatable; never used
    #: to address the device.
    device_label: str = Field(min_length=1)
    connection: MountConnection
    port: str | None = None
    baud: PositiveInt
    slew_rate_max_deg_s: PositiveFloat
    counterweight_fitted: bool

    @field_validator("device_label")
    @classmethod
    def _label_is_not_whitespace(cls, value: str) -> str:
        """``min_length`` counts spaces; a label of spaces names nothing."""
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("port")
    @classmethod
    def _port_is_a_serial_device(cls, value: str | None) -> str | None:
        """Accept a by-id path or a bare ACM/USB node, and nothing else."""
        if value is None:
            return None
        if any(value.startswith(prefix) for prefix in _STABLE_PORT_PREFIXES):
            bare = [prefix.rstrip("/") for prefix in _STABLE_PORT_PREFIXES]
            if ".." in value or value.rstrip("/") in bare:
                raise ValueError(f"{value!r} is not a complete /dev/serial path")
            return value
        if _BARE_PORT_PATTERN.match(value):
            return value
        raise ValueError(
            f"{value!r} is not a serial device path. Expected a stable "
            "/dev/serial/by-id/... path (preferred, and what the driver reports), "
            "or /dev/ttyACM<n> or /dev/ttyUSB<n>. The Wave 150i is CDC-ACM and "
            "appears as ttyACM, not ttyUSB."
        )

    @property
    def uses_stable_port_path(self) -> bool:
        """Whether the port survives a reboot or another USB device appearing.

        ``/dev/ttyACM0`` is assigned in enumeration order. Plug in a second
        USB-serial device and the mount can become ttyACM1, at which point the
        configured path points at something else entirely.
        """
        return self.port is not None and any(
            self.port.startswith(prefix) for prefix in _STABLE_PORT_PREFIXES
        )

    # There is deliberately no rule making ``port`` mandatory for a serial
    # connection. indi_eqmod fills DEVICE_PORT.PORT with the correct by-id path
    # unprompted, so a configured port is an override rather than the primary
    # source (docs/FIELD-NOTES-M1.md section 2.3). Omitting the key means "use
    # the port the driver reports"; the executor refuses to connect when
    # neither the configuration nor the driver names one. A blank string is not
    # that statement and is rejected by the field validator above.


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
