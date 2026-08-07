"""Equipment configuration schema — SPEC section 5.1.

The shipped ``config/equipment.yaml`` must carry the reference hardware of
SPEC section 2.1 exactly, and the schema must reject anything malformed
loudly rather than filling in defaults.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from nocturne.schemas import EquipmentConfig, load_equipment_config
from nocturne.schemas.equipment import (
    PLACEHOLDER_ELEVATION_M,
    PLACEHOLDER_LATITUDE,
    PLACEHOLDER_LONGITUDE,
    Site,
)


@pytest.fixture
def raw(config_dir: Path) -> dict[str, Any]:
    """The shipped equipment.yaml parsed to a plain dict, safe to mutate."""
    import yaml

    with (config_dir / "equipment.yaml").open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return copy.deepcopy(loaded)


class TestShippedFile:
    def test_shipped_equipment_config_validates(self, config_dir: Path) -> None:
        config = load_equipment_config(config_dir / "equipment.yaml")
        assert isinstance(config, EquipmentConfig)

    def test_the_shipped_site_is_the_generic_placeholder(self, config_dir: Path) -> None:
        """SPEC section 2.3 names the town in prose. Coordinates are not shipped.

        A residential site's latitude and longitude to four decimal places is
        somebody's home address to within metres, published next to a
        description of expensive equipment left outside at night and a schedule
        of when nobody is watching it.
        """
        site = load_equipment_config(config_dir / "equipment.yaml").site
        assert site.latitude == pytest.approx(PLACEHOLDER_LATITUDE)
        assert site.longitude == pytest.approx(PLACEHOLDER_LONGITUDE)
        assert site.elevation_m == pytest.approx(PLACEHOLDER_ELEVATION_M)
        assert site.is_placeholder

    #: Fragments of the reference rig's real coordinates. None may reach the file.
    LEAKED_COORDINATES = ("41.58", "-4.58", "4.5814")

    def test_no_real_site_coordinates_are_shipped(self, config_dir: Path) -> None:
        """The regression this class exists for. Checked on the file, not the model."""
        text = (config_dir / "equipment.yaml").read_text(encoding="utf-8")
        assert "site:" in text, "the file was not read; this test proves nothing"
        for leaked in self.LEAKED_COORDINATES:
            assert leaked not in text, f"{leaked} is a real observing site"

    def test_the_leak_check_would_catch_a_leak(self) -> None:
        """Positive control — CLAUDE.md section 2.

        The test above passes by finding nothing, and finding nothing is also
        what a broken check does. This runs the same predicate over a file that
        does contain the coordinates, and requires every fragment to be caught.
        """
        leaked_file = "site:\n  latitude: 41.5806\n  longitude: -4.5814\n"
        caught = [fragment for fragment in self.LEAKED_COORDINATES if fragment in leaked_file]
        assert sorted(caught) == sorted(self.LEAKED_COORDINATES)

    def test_imaging_camera_matches_asi533mm_pro(self, config_dir: Path) -> None:
        camera = load_equipment_config(config_dir / "equipment.yaml").imaging_camera
        assert camera.name == "ZWO CCD ASI533MM Pro"
        assert camera.pixel_size_um == pytest.approx(3.76)
        assert camera.width_px == 3008
        assert camera.height_px == 3008
        assert camera.bit_depth == 14
        assert camera.mono is True
        assert camera.cooled is True
        assert camera.cooling.target_c == pytest.approx(-10.0)
        assert camera.cooling.ramp_c_per_min == pytest.approx(2.0)

    def test_filter_wheel_has_eight_slots_five_populated(self, config_dir: Path) -> None:
        wheel = load_equipment_config(config_dir / "equipment.yaml").filter_wheel
        assert sorted(wheel.slots) == [1, 2, 3, 4, 5, 6, 7, 8]
        # The order is asserted in TestTheFilterWheelMatchesTheWheel, against
        # what is physically in it. Here only the shape.
        assert len(wheel.populated_slots) == 5
        assert all(wheel.slots[i].type == "empty" for i in (6, 7, 8))

    def test_mount_is_configured_for_direct_usb_serial(self, config_dir: Path) -> None:
        """SPEC section 5.1: USB serial preferred over WiFi. This is the M1 HITL subject."""
        mount = load_equipment_config(config_dir / "equipment.yaml").mount
        assert mount.indi_driver == "indi_eqmod_telescope"
        assert mount.device_label == "Wave 150i"
        assert mount.connection == "serial"
        assert mount.indi_device_name == "EQMod Mount"
        # No port: the driver reports its own, and a by-id path is one rig's
        # serial number. See TestMountSerialPort below.
        assert mount.port is None
        assert mount.baud == 115200
        assert mount.counterweight_fitted is False

    def test_guiding_is_guidescope_with_configurable_focal_length(
        self, config_dir: Path
    ) -> None:
        """SPEC section 2.1 migration note: guide scale must never be a constant."""
        guiding = load_equipment_config(config_dir / "equipment.yaml").guiding
        assert guiding.mode == "guidescope"
        assert guiding.focal_length_mm == pytest.approx(120.0)
        assert guiding.camera.pixel_size_um == pytest.approx(3.75)


class TestDerivedPlateScales:
    """SPEC section 2.1 nominal values, derived rather than hardcoded."""

    def test_imaging_plate_scale(self, config_dir: Path) -> None:
        config = load_equipment_config(config_dir / "equipment.yaml")
        assert config.imaging_plate_scale_arcsec_per_px() == pytest.approx(0.776, abs=1e-3)

    def test_imaging_field_of_view(self, config_dir: Path) -> None:
        config = load_equipment_config(config_dir / "equipment.yaml")
        width, height = config.imaging_fov_arcmin()
        assert width == pytest.approx(38.9, abs=0.1)
        assert height == pytest.approx(38.9, abs=0.1)

    def test_guide_plate_scale(self, config_dir: Path) -> None:
        config = load_equipment_config(config_dir / "equipment.yaml")
        assert config.guide_plate_scale_arcsec_per_px() == pytest.approx(6.45, abs=1e-2)

    def test_guide_plate_scale_follows_config_on_oag_migration(
        self, raw: dict[str, Any]
    ) -> None:
        """Migrating to an OAG is a config change, never a code change."""
        raw["guiding"]["mode"] = "oag"
        raw["guiding"]["focal_length_mm"] = 1000
        config = EquipmentConfig.model_validate(raw)
        assert config.guide_plate_scale_arcsec_per_px() == pytest.approx(0.77, abs=1e-2)

    def test_plate_scale_follows_active_optical_train(self, raw: dict[str, Any]) -> None:
        raw["optical_trains"][0]["active"] = False
        raw["optical_trains"][1]["active"] = True
        raw["optical_trains"][1]["focal_length_mm"] = 2000
        config = EquipmentConfig.model_validate(raw)
        assert config.active_optical_train.id == "mpcc"
        assert config.imaging_plate_scale_arcsec_per_px() == pytest.approx(0.388, abs=1e-3)


class TestStrictness:
    def test_unknown_key_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["telescope_colour"] = "black"
        with pytest.raises(ValidationError, match="telescope_colour"):
            EquipmentConfig.model_validate(raw)

    def test_unknown_nested_key_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["mount"]["wifi_ssid"] = "terrace"
        with pytest.raises(ValidationError, match="wifi_ssid"):
            EquipmentConfig.model_validate(raw)

    def test_missing_required_section_is_rejected(self, raw: dict[str, Any]) -> None:
        del raw["mount"]
        with pytest.raises(ValidationError, match="mount"):
            EquipmentConfig.model_validate(raw)

    def test_config_is_immutable(self, config_dir: Path) -> None:
        """CLAUDE.md invariant 2: nothing modifies configuration at runtime."""
        config = load_equipment_config(config_dir / "equipment.yaml")
        with pytest.raises(ValidationError):
            config.mount.counterweight_fitted = True  # type: ignore[misc]

    @pytest.mark.parametrize("latitude", [-90.1, 90.1, 1000.0])
    def test_impossible_latitude_is_rejected(
        self, raw: dict[str, Any], latitude: float
    ) -> None:
        raw["site"]["latitude"] = latitude
        with pytest.raises(ValidationError, match="latitude"):
            EquipmentConfig.model_validate(raw)

    @pytest.mark.parametrize("longitude", [-180.1, 180.1])
    def test_impossible_longitude_is_rejected(
        self, raw: dict[str, Any], longitude: float
    ) -> None:
        raw["site"]["longitude"] = longitude
        with pytest.raises(ValidationError, match="longitude"):
            EquipmentConfig.model_validate(raw)

    def test_unknown_timezone_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["site"]["timezone"] = "Mars/Olympus_Mons"
        with pytest.raises(ValidationError, match="timezone"):
            EquipmentConfig.model_validate(raw)

    @pytest.mark.parametrize("value", [0.0, -1.0])
    def test_non_positive_pixel_size_is_rejected(
        self, raw: dict[str, Any], value: float
    ) -> None:
        raw["imaging_camera"]["pixel_size_um"] = value
        with pytest.raises(ValidationError, match="pixel_size_um"):
            EquipmentConfig.model_validate(raw)

    @pytest.mark.parametrize("value", [0.0, -2.0])
    def test_non_positive_cooling_ramp_is_rejected(
        self, raw: dict[str, Any], value: float
    ) -> None:
        """A zero or negative ramp rate would mean "cool instantly" — TEC damage."""
        raw["imaging_camera"]["cooling"]["ramp_c_per_min"] = value
        with pytest.raises(ValidationError, match="ramp_c_per_min"):
            EquipmentConfig.model_validate(raw)

    def test_non_positive_focal_length_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["optical_trains"][0]["focal_length_mm"] = 0
        with pytest.raises(ValidationError, match="focal_length_mm"):
            EquipmentConfig.model_validate(raw)

    def test_negative_slew_rate_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["mount"]["slew_rate_max_deg_s"] = -1.0
        with pytest.raises(ValidationError, match="slew_rate_max_deg_s"):
            EquipmentConfig.model_validate(raw)

    def test_unknown_guiding_mode_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["guiding"]["mode"] = "off_axis"
        with pytest.raises(ValidationError, match="mode"):
            EquipmentConfig.model_validate(raw)

    def test_unknown_mount_connection_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["mount"]["connection"] = "bluetooth"
        with pytest.raises(ValidationError, match="connection"):
            EquipmentConfig.model_validate(raw)


class TestCrossFieldConsistency:
    def test_exactly_one_optical_train_must_be_active(self, raw: dict[str, Any]) -> None:
        raw["optical_trains"][1]["active"] = True
        with pytest.raises(ValidationError, match="exactly one active optical train"):
            EquipmentConfig.model_validate(raw)

    def test_no_active_optical_train_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["optical_trains"][0]["active"] = False
        with pytest.raises(ValidationError, match="exactly one active optical train"):
            EquipmentConfig.model_validate(raw)

    def test_duplicate_optical_train_id_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["optical_trains"][1]["id"] = "native"
        with pytest.raises(ValidationError, match="duplicate optical train id"):
            EquipmentConfig.model_validate(raw)

    def test_empty_slot_may_not_be_named(self, raw: dict[str, Any]) -> None:
        raw["filter_wheel"]["slots"][6] = {"name": "Ha", "type": "empty"}
        with pytest.raises(ValidationError, match="empty"):
            EquipmentConfig.model_validate(raw)

    def test_populated_slot_must_be_named(self, raw: dict[str, Any]) -> None:
        raw["filter_wheel"]["slots"][2] = {"name": None, "type": "luminance"}
        with pytest.raises(ValidationError, match="name"):
            EquipmentConfig.model_validate(raw)

    def test_slot_numbering_must_be_contiguous_from_one(self, raw: dict[str, Any]) -> None:
        raw["filter_wheel"]["slots"][9] = {"name": None, "type": "empty"}
        del raw["filter_wheel"]["slots"][1]
        with pytest.raises(ValidationError, match="contiguous"):
            EquipmentConfig.model_validate(raw)

    def test_a_dark_slot_is_required_for_calibration_frames(self, raw: dict[str, Any]) -> None:
        """SPEC section 10.2: darks and dark-flats are captured with the Dark slot."""
        slots = raw["filter_wheel"]["slots"]
        # Find it rather than assume where it is. This test used to empty slot 1
        # because slot 1 was Dark; the wheel says otherwise and the test went
        # green while emptying the luminance filter instead.
        dark = [position for position, slot in slots.items() if slot["type"] == "dark"]
        assert len(dark) == 1, dark
        slots[dark[0]] = {"name": None, "type": "empty"}

        with pytest.raises(ValidationError, match="dark"):
            EquipmentConfig.model_validate(raw)

    def test_duplicate_gain_profile_name_is_rejected(self, raw: dict[str, Any]) -> None:
        raw["imaging_camera"]["gain_profiles"].append(
            {
                "name": "hcg",
                "gain": 200,
                "read_noise_e": 1.2,
                "full_well_e": 10000,
                "e_per_adu": 0.16,
            }
        )
        with pytest.raises(ValidationError, match="duplicate gain profile"):
            EquipmentConfig.model_validate(raw)

    def test_at_least_one_gain_profile_is_required(self, raw: dict[str, Any]) -> None:
        raw["imaging_camera"]["gain_profiles"] = []
        with pytest.raises(ValidationError, match="gain_profiles"):
            EquipmentConfig.model_validate(raw)

    def test_temperature_compensation_requires_a_measured_coefficient(
        self, raw: dict[str, Any]
    ) -> None:
        """SPEC section 15: disabled until the coefficient has been measured."""
        raw["focuser"]["temperature_compensation"]["enabled"] = True
        with pytest.raises(ValidationError, match="coefficient_steps_per_c"):
            EquipmentConfig.model_validate(raw)


class TestThePlaceholderSiteIsDetectable:
    """Wrong coordinates fail quietly and expensively.

    Ephemeris, altitude windows and meridian timing are all derived from the
    site. A placeholder left in place does not raise; it just points the
    telescope at the wrong part of the sky all night, and at the wrong times.
    """

    def site(self, **overrides: object) -> Site:
        fields: dict[str, Any] = {
            "name": "Placeholder — REPLACE THIS",
            "latitude": PLACEHOLDER_LATITUDE,
            "longitude": PLACEHOLDER_LONGITUDE,
            "elevation_m": PLACEHOLDER_ELEVATION_M,
            "timezone": "UTC",
        }
        fields.update(overrides)
        return Site.model_validate(fields)

    def test_the_shipped_values_are_recognised(self) -> None:
        assert self.site().is_placeholder

    def test_a_real_site_is_not(self) -> None:
        assert not self.site(latitude=51.4778, longitude=-0.0015).is_placeholder

    def test_changing_only_the_name_does_not_clear_it(self) -> None:
        """Coordinates are what break the ephemeris, not the label."""
        assert self.site(name="My terrace").is_placeholder

    def test_changing_either_coordinate_clears_it(self) -> None:
        """Either one alone. A half-edited file is still not this location."""
        assert not self.site(latitude=41.5).is_placeholder
        assert not self.site(longitude=-4.5).is_placeholder

    def test_any_coordinate_a_person_typed_clears_it(self) -> None:
        """The question is "has this been edited?", not "is this near 45N 0E?".

        So the tolerance is about 10 cm. Someone genuinely observing from
        45.0000 N, 0.0000 E — and the point is in open country, so nobody is —
        moves a metre and is never asked again.
        """
        for delta in (0.0001, 0.001, 0.01, 1.0):
            assert not self.site(latitude=PLACEHOLDER_LATITUDE + delta).is_placeholder
            assert not self.site(longitude=PLACEHOLDER_LONGITUDE - delta).is_placeholder

    def test_the_same_value_written_differently_still_matches(self) -> None:
        """45, 45.0 and 45.000 are the same statement: the file is unedited."""
        assert self.site(latitude=45, longitude=-0.0).is_placeholder

    def test_the_shipped_configuration_reports_itself_as_a_placeholder(
        self, config_dir: Path
    ) -> None:
        assert load_equipment_config(config_dir / "equipment.yaml").site.is_placeholder


class TestTheIndiDeviceNameIsConfiguration:
    """CLAUDE.md section 6: no hardcoded equipment.

    ``indi_eqmod`` announces itself as "EQMod Mount". That is the driver's name
    for the device and it is equipment-dependent — a different mount driver
    announces something else. The operator's name for the same object is
    "Wave 150i". Two fields, because they are two different things: one is a
    technical identifier the executor addresses, the other is a label a person
    reads and a translator translates.
    """

    def test_the_shipped_config_names_the_device_the_driver_announces(
        self, config_dir: Path
    ) -> None:
        mount = load_equipment_config(config_dir / "equipment.yaml").mount
        assert mount.indi_device_name == "EQMod Mount"

    def test_it_matches_what_the_real_driver_announced(self) -> None:
        """Checked against the recorded dump, not against my memory of it."""
        from tests.fixtures.eqmod import EQMOD_DEVICE

        assert EQMOD_DEVICE == "EQMod Mount"

    def test_the_label_and_the_device_name_are_separate_fields(self, config_dir: Path) -> None:
        mount = load_equipment_config(config_dir / "equipment.yaml").mount
        assert mount.device_label == "Wave 150i"
        assert mount.device_label != mount.indi_device_name

    def test_it_is_required(self, raw: dict[str, Any]) -> None:
        del raw["mount"]["indi_device_name"]
        with pytest.raises(ValidationError, match="indi_device_name"):
            EquipmentConfig.model_validate(raw)

    def test_it_may_not_be_blank(self, raw: dict[str, Any]) -> None:
        raw["mount"]["indi_device_name"] = "   "
        with pytest.raises(ValidationError, match="indi_device_name"):
            EquipmentConfig.model_validate(raw)


class TestMountSerialPort:
    """Field notes section 2.1 — the Wave 150i is CDC-ACM, not a USB-serial bridge.

    It presents an STM32 virtual COM port and the kernel binds ``cdc_acm``, so it
    appears at /dev/ttyACM0. There is no FTDI, CH340 or CP210x chip. The shipped
    default pointed at /dev/ttyUSB0, which does not exist on this rig.
    """

    def test_the_shipped_config_names_no_port_at_all(self, config_dir: Path) -> None:
        """A by-id path carries a serial number, and serial numbers are personal.

        The reference rig's path is
        ``usb-STMicroelectronics_STM32_Virtual_ComPort_<serial>-if00``. Shipping
        one operator's serial number as the default means every other user gets
        a path to a device that does not exist. Omitted means "ask the driver",
        which is right for everyone.
        """
        mount = load_equipment_config(config_dir / "equipment.yaml").mount
        assert mount.port is None

    #: The reference mount's own serial number, which is nobody else's.
    LEAKED_SERIAL = "8F8B50B10E31"

    def test_the_shipped_config_carries_nobody_s_serial_number(self, config_dir: Path) -> None:
        """Checked on the file, not the model: a comment would leak it too."""
        text = (config_dir / "equipment.yaml").read_text(encoding="utf-8")
        assert "mount:" in text, "the file was not read; this test proves nothing"
        assert self.LEAKED_SERIAL not in text

    def test_the_serial_number_check_would_catch_a_leak(self) -> None:
        """Positive control — CLAUDE.md section 2."""
        leaked_file = f"  port: /dev/serial/by-id/usb-X_{self.LEAKED_SERIAL}-if00\n"
        assert self.LEAKED_SERIAL in leaked_file

    def test_the_shipped_default_is_not_ttyusb(self, config_dir: Path) -> None:
        """The regression this class exists for."""
        mount = load_equipment_config(config_dir / "equipment.yaml").mount
        assert mount.port != "/dev/ttyUSB0"

    @pytest.mark.parametrize(
        "port",
        [
            "/dev/serial/by-id/usb-STMicroelectronics_STM32_Virtual_ComPort_8F8B-if00",
            "/dev/ttyACM0",
            "/dev/ttyACM11",
            "/dev/ttyUSB0",
        ],
    )
    def test_accepted_serial_paths(self, raw: dict[str, Any], port: str) -> None:
        raw["mount"]["port"] = port
        assert EquipmentConfig.model_validate(raw).mount.port == port

    @pytest.mark.parametrize(
        "port",
        ["ttyACM0", "COM3", "/home/pi/mount", "/dev/../etc/passwd", ""],
    )
    def test_rejected_serial_paths(self, raw: dict[str, Any], port: str) -> None:
        raw["mount"]["port"] = port
        with pytest.raises(ValidationError, match="port"):
            EquipmentConfig.model_validate(raw)

    def test_a_by_id_path_is_reported_as_stable(self, raw: dict[str, Any]) -> None:
        raw["mount"]["port"] = "/dev/serial/by-id/usb-STMicroelectronics_STM32-if00"
        assert EquipmentConfig.model_validate(raw).mount.uses_stable_port_path is True

    def test_a_bare_device_node_is_reported_as_unstable(self, raw: dict[str, Any]) -> None:
        """ttyACM0 moves when other USB serial hardware is attached."""
        raw["mount"]["port"] = "/dev/ttyACM0"
        assert EquipmentConfig.model_validate(raw).mount.uses_stable_port_path is False

    def test_the_reference_baud_is_115200(self, config_dir: Path) -> None:
        """The driver starts at 9600; the executor must set this before CONNECT."""
        assert load_equipment_config(config_dir / "equipment.yaml").mount.baud == 115200


class TestTheFilterWheelMatchesTheWheel:
    """docs/FIELD-NOTES-M1.md §10.1 — verified by opening it and reading the glass.

    The earlier order was wrong in every slot. That is worth a test of its own
    rather than a corrected line, because the failure it produces is silent:
    frames filed against the wrong channel, and nothing in the image looks wrong.
    """

    #: Slot -> filter, as physically fitted on 2026-08-07.
    FITTED: ClassVar[dict[int, str]] = {1: "L", 2: "R", 3: "G", 4: "B", 5: "Dark"}

    def test_the_shipped_order_is_the_order_in_the_wheel(self, config_dir: Path) -> None:
        wheel = load_equipment_config(config_dir / "equipment.yaml").filter_wheel
        named = {position: slot.name for position, slot in wheel.populated_slots.items()}
        assert named == self.FITTED

    def test_the_old_order_is_not_shipped(self, config_dir: Path) -> None:
        """The regression. Slot 1 was Dark and is L; slot 5 was B and is Dark."""
        wheel = load_equipment_config(config_dir / "equipment.yaml").filter_wheel
        assert wheel.slots[1].name != "Dark"
        assert wheel.slots[5].name != "B"

    def test_the_dark_slot_is_where_the_wheel_has_it(self, config_dir: Path) -> None:
        wheel = load_equipment_config(config_dir / "equipment.yaml").filter_wheel
        dark = [pos for pos, slot in wheel.slots.items() if slot.type == "dark"]
        assert dark == [5]

    def test_it_disagrees_with_every_name_the_driver_shipped(self) -> None:
        """Positive control for §10.2 — CLAUDE.md §2.

        The point of writing names to the driver is that its own are wrong. If
        the configured names ever coincided with the ZWO defaults, the tests
        that check the overwrite would pass whether or not it happened.
        """
        zwo_defaults = {
            1: "Red",
            2: "Green",
            3: "Blue",
            4: "H_Alpha",
            5: "SII",
            6: "OIII",
            7: "LPR",
            8: "Luminance",
        }
        overlapping = [
            position
            for position, name in self.FITTED.items()
            if zwo_defaults.get(position) == name
        ]
        assert not overlapping, (
            f"slots {overlapping} agree with the ZWO factory names, so an "
            "overwrite test through those slots would prove nothing"
        )


class TestTheDriverIsAuthoritativeForItsOwnLimits:
    """docs/FIELD-NOTES-M1.md §12."""

    def test_focuser_max_position_is_not_invented(self, config_dir: Path) -> None:
        """The driver reports 100000; the 60000 that was here came from nowhere."""
        focuser = load_equipment_config(config_dir / "equipment.yaml").focuser
        assert focuser.max_position is None

    def test_focuser_backlash_is_unset_rather_than_zero(self, config_dir: Path) -> None:
        """0 looked like a measurement. It was not, and the EAF ships 180."""
        focuser = load_equipment_config(config_dir / "equipment.yaml").focuser
        assert focuser.backlash_steps is None

    def test_the_imaging_bit_depth_is_the_converter_not_the_container(
        self, config_dir: Path
    ) -> None:
        """CCD_INFO.CCD_BITSPERPIXEL reads 16. The IMX533 ADC is 14-bit.

        SPEC §2.1 is right and the driver is reporting the container size. This
        test exists so that nobody "corrects" the config to match the driver.
        """
        camera = load_equipment_config(config_dir / "equipment.yaml").imaging_camera
        assert camera.bit_depth == 14
