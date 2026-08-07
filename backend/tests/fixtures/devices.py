"""The four ZWO devices, built from the recorded property dump.

``hardware/devices-properties.txt`` is ``indi_getprop`` output captured from the
real cameras, filter wheel and focuser during the M1 bench tests
(docs/FIELD-NOTES-M1.md §9). Parsing it back means the property names, elements
and *defaults* the tests run against are the drivers', not the author's — and
the defaults are the whole point here, since what is being tested is that
Nocturne replaces them.

Same standing as ``eqmod.py``, and the same caveat: ``indi_getprop`` records
neither switch rules nor permissions, so do not write a test that depends on
either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from nocturne.executor.indi.protocol import PropertyKind
from tests.fixtures.eqmod import coerce_value, infer_kind
from tests.fixtures.fake_indi import FakeProperty

PROPERTY_DUMP: Final = Path(__file__).parent / "hardware" / "devices-properties.txt"

#: The names the drivers announce themselves under, exactly.
IMAGING_CAMERA: Final = "ZWO CCD ASI533MM Pro"
GUIDE_CAMERA: Final = "ZWO CCD ASI120MM Mini"
EFW: Final = "ZWO EFW"
FOCUSER: Final = "ZWO EAF"

ALL_DEVICES: Final = (IMAGING_CAMERA, GUIDE_CAMERA, EFW, FOCUSER)

#: Vectors whose kind cannot be inferred safely from their recorded values.
#: FILTER_NAME holds free text that must not be read as a number; CCD_CONTROLS
#: and the focuser's limits are numbers even when they read like integers.
_KNOWN_KINDS: Final[dict[str, PropertyKind]] = {
    "CONNECTION": PropertyKind.SWITCH,
    "DRIVER_INFO": PropertyKind.TEXT,
    "FILTER_NAME": PropertyKind.TEXT,
    "FILTER_SLOT": PropertyKind.NUMBER,
    "CCD_CONTROLS": PropertyKind.NUMBER,
    "CCD_INFO": PropertyKind.NUMBER,
    "FOCUS_MAX": PropertyKind.NUMBER,
    "FOCUS_BACKLASH_STEPS": PropertyKind.NUMBER,
    "ABS_FOCUS_POSITION": PropertyKind.NUMBER,
    "FOCUS_TEMPERATURE": PropertyKind.NUMBER,
}


def load_recorded_properties(
    path: Path = PROPERTY_DUMP,
) -> dict[str, dict[str, FakeProperty]]:
    """Parse the dump into ``{device: {vector: FakeProperty}}``.

    Lines are ``Device.VECTOR.ELEMENT=value``. Device names contain spaces and
    element names may too, but a vector name never contains a dot — so the
    device is the first dotted segment, the element the last, and the vector
    whatever is between.

    Raises:
        AssertionError: if the dump yields nothing, or is missing a device that
            is certainly in it. Every test built on this reads a default out of
            it and asserts it was replaced; a fixture that quietly returned
            nothing would make all of them pass (CLAUDE.md §2).
    """
    collected: dict[str, dict[str, dict[str, str]]] = {}
    order: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        path_part, _, value = line.partition("=")
        parts = path_part.split(".")
        if len(parts) < 3:
            continue
        device, vector, element = parts[0], ".".join(parts[1:-1]), parts[-1]
        if device not in collected:
            collected[device] = {}
            order[device] = []
        if vector not in collected[device]:
            collected[device][vector] = {}
            order[device].append(vector)
        collected[device][vector][element] = value

    missing = [device for device in ALL_DEVICES if device not in collected]
    assert not missing, (
        f"{path} yielded nothing for {missing}. Every test built on this fixture "
        "reads a driver default out of it; a fixture that returns nothing makes "
        "all of them pass while checking nothing."
    )

    devices: dict[str, dict[str, FakeProperty]] = {}
    for device, vectors in collected.items():
        built: dict[str, FakeProperty] = {}
        for vector in order[device]:
            elements = vectors[vector]
            kind = _KNOWN_KINDS.get(vector) or infer_kind(elements.values())
            built[vector] = FakeProperty(
                name=vector,
                kind=kind,
                values={name: coerce_value(kind, value) for name, value in elements.items()},
                rule="AnyOfMany" if kind is PropertyKind.SWITCH else None,
            )
        devices[device] = built
    return devices


def recorded_devices() -> dict[str, dict[str, FakeProperty]]:
    """The four devices exactly as recorded, connected."""
    return load_recorded_properties()


def fresh_devices() -> dict[str, dict[str, FakeProperty]]:
    """The four devices as their drivers present them before CONNECT.

    One documented departure from the dump and no others: CONNECTION reads
    disconnected. Everything else — the gains, the offsets, the ZWO factory
    filter names — is left exactly as the hardware had it, because those
    defaults are what the tests exist to see replaced.
    """
    devices = load_recorded_properties()
    for properties in devices.values():
        connection = properties.get("CONNECTION")
        if connection is not None:
            connection.values = {"CONNECT": False, "DISCONNECT": True}
    return devices


__all__ = [
    "ALL_DEVICES",
    "EFW",
    "FOCUSER",
    "GUIDE_CAMERA",
    "IMAGING_CAMERA",
    "PROPERTY_DUMP",
    "fresh_devices",
    "load_recorded_properties",
    "recorded_devices",
]
