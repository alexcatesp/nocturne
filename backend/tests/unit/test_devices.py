"""A configured serial port that is not there — CLAUDE.md section 6.

``mount.port`` is optional: omitted, the driver finds the mount itself
(docs/FIELD-NOTES-M1.md section 2.3). Setting it is an override, and an
override exists precisely because the operator wants *that* device and not
whatever the driver picked.

So an override naming a device that does not exist is a hard failure, with the
path in the message. Falling back to auto-detection would be the software
quietly choosing a different mount than the one it was told to use, which is
exactly how a swapped cable goes unnoticed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nocturne.devices import (
    MissingSerialDeviceError,
    describe_configured_port,
    require_configured_port,
    serial_device_exists,
)

LABEL = "Wave 150i"

#: A character device that exists on every machine the suite runs on. The
#: schema would not accept it as a mount port, which is why these functions
#: take a path and not a Mount: the check is about the filesystem, and tying it
#: to the config schema would mean it could only be tested through the schema.
A_REAL_CHARACTER_DEVICE = "/dev/null"


class TestDetectingTheDevice:
    def test_a_character_device_exists(self) -> None:
        assert serial_device_exists("/dev/null")

    def test_a_path_that_is_not_there_does_not_exist(self, tmp_path: Path) -> None:
        assert not serial_device_exists(str(tmp_path / "ttyACM7"))

    def test_a_regular_file_is_not_a_serial_device(self, tmp_path: Path) -> None:
        """A stale file where a device node should be is worse than nothing."""
        impostor = tmp_path / "ttyACM0"
        impostor.write_text("not a device", encoding="utf-8")
        assert not serial_device_exists(str(impostor))

    def test_a_dangling_symlink_does_not_exist(self, tmp_path: Path) -> None:
        """What a /dev/serial/by-id/ path becomes when the mount is unplugged."""
        link = tmp_path / "usb-Some_Mount-if00"
        link.symlink_to(tmp_path / "ttyACM0")
        assert not serial_device_exists(str(link))

    def test_a_symlink_to_a_device_exists(self, tmp_path: Path) -> None:
        """What /dev/serial/by-id/ is when the mount is plugged in."""
        link = tmp_path / "usb-Some_Mount-if00"
        link.symlink_to("/dev/null")
        assert serial_device_exists(str(link))


class TestTheStartupCheck:
    def test_an_unset_port_is_fine_because_the_driver_will_find_it(self) -> None:
        require_configured_port(None, label=LABEL)

    def test_a_configured_port_that_exists_is_fine(self) -> None:
        require_configured_port(A_REAL_CHARACTER_DEVICE, label=LABEL)

    def test_a_configured_port_that_is_absent_fails(self, tmp_path: Path) -> None:
        absent = str(tmp_path / "usb-Wave_150i-if00")
        with pytest.raises(MissingSerialDeviceError) as raised:
            require_configured_port(absent, label=LABEL)
        assert absent in str(raised.value)
        assert LABEL in str(raised.value)

    def test_the_message_says_what_to_do(self, tmp_path: Path) -> None:
        """Read on a phone, in the dark, on a terrace."""
        with pytest.raises(MissingSerialDeviceError) as raised:
            require_configured_port(str(tmp_path / "usb-Wave_150i-if00"), label=LABEL)

        message = str(raised.value)
        assert "/dev/serial/by-id/" in message
        assert "dialout" in message
        assert "equipment.yaml" in message

    def test_it_does_not_fall_back_to_auto_detection(self, tmp_path: Path) -> None:
        """The failure this check exists to prevent, stated as a test.

        If a configured-but-absent port silently degraded to "let the driver
        choose", a cable moved to the wrong socket would look like success.
        """
        with pytest.raises(MissingSerialDeviceError):
            require_configured_port(str(tmp_path / "gone"), label=LABEL)


class TestTheDescription:
    def test_an_unset_port_says_the_driver_decides(self) -> None:
        assert "driver" in describe_configured_port(None)

    def test_a_present_port_is_named_and_marked_present(self) -> None:
        line = describe_configured_port(A_REAL_CHARACTER_DEVICE)
        assert A_REAL_CHARACTER_DEVICE in line
        assert "present" in line

    def test_an_absent_port_is_named_and_marked_absent(self, tmp_path: Path) -> None:
        absent = str(tmp_path / "usb-Wave_150i-if00")
        line = describe_configured_port(absent)
        assert absent in line
        assert "NOT PRESENT" in line

    def test_the_description_is_one_line(self, tmp_path: Path) -> None:
        for port in (None, A_REAL_CHARACTER_DEVICE, str(tmp_path / "gone")):
            assert "\n" not in describe_configured_port(port)

    def test_an_unreadable_path_is_reported_as_absent_not_as_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(_path: str) -> os.stat_result:
            raise PermissionError("nope")

        monkeypatch.setattr(os, "stat", refuse)
        assert not serial_device_exists("/dev/null")
