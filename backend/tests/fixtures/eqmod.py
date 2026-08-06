"""A fake EQMod mount built from the real property dump.

``hardware/wave150i-properties.txt`` is ``indi_getprop`` output captured from
the actual Sky-Watcher Wave 150i on the reference rig (docs/FIELD-NOTES-M1.md).
This module parses it and serves it back through :class:`FakeIndiServer`, so
tests of the eqmod path run against property names, types and values the
hardware really produced.

That distinction matters. ``fake_kstars.py`` is a stub written by the same
author as the bridge it verifies, so the two share any misconception; this
fixture cannot, because nobody wrote its contents. Where a test can be pointed
at recorded hardware rather than a stub, it should be.

Two things ``indi_getprop`` does not record, and this module therefore cannot
reproduce: the switch rule (OneOfMany / AtMostOne / AnyOfMany) and the
permission. Both are defaulted below; do not write a test that depends on them.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Final

from nocturne.executor.indi.protocol import PropertyKind
from tests.fixtures.fake_indi import FakeProperty

#: The device name indi_eqmod announces. Not the operator's label for the mount.
EQMOD_DEVICE: Final = "EQMod Mount"

PROPERTY_DUMP: Final = Path(__file__).parent / "hardware" / "wave150i-properties.txt"

#: Switch elements read "On"/"Off"; everything else is a number or free text.
_SWITCH_VALUES: Final = frozenset({"On", "Off"})

#: Vectors whose kind cannot be inferred safely from the recorded values, with
#: the kind indi_eqmod actually defines them as. DEVICE_PORT holds a path that
#: never parses as a number, but a text vector whose value happened to be "0"
#: would be misread as a number, so anything a test asserts on is pinned here.
_KNOWN_KINDS: Final[dict[str, PropertyKind]] = {
    "CONNECTION": PropertyKind.SWITCH,
    "DEVICE_PORT": PropertyKind.TEXT,
    "DEVICE_BAUD_RATE": PropertyKind.SWITCH,
    "DRIVER_INFO": PropertyKind.TEXT,
    "TELESCOPE_SLEW_RATE": PropertyKind.SWITCH,
    "SLEWSPEEDS": PropertyKind.NUMBER,
    "MOUNTINFORMATION": PropertyKind.TEXT,
    "STEPPERS": PropertyKind.NUMBER,
    "EQUATORIAL_EOD_COORD": PropertyKind.NUMBER,
    "GEOGRAPHIC_COORD": PropertyKind.NUMBER,
    "TELESCOPE_TRACK_STATE": PropertyKind.SWITCH,
    "TELESCOPE_TRACK_MODE": PropertyKind.SWITCH,
    "RASTATUS": PropertyKind.TEXT,
    "DESTATUS": PropertyKind.TEXT,
}


def load_recorded_properties(path: Path = PROPERTY_DUMP) -> dict[str, FakeProperty]:
    """Parse the recorded dump into properties a FakeIndiServer can serve.

    Lines are ``Device.VECTOR.ELEMENT=value``. Element names may contain
    spaces ("READ INCREMENT"); vector names may not contain dots, so the
    device is the first segment, the element the last, and the vector
    everything between.
    """
    collected: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        path_part, _, value = line.partition("=")
        parts = path_part.split(".")
        if len(parts) < 3 or parts[0] != EQMOD_DEVICE:
            continue
        vector = ".".join(parts[1:-1])
        if vector not in collected:
            collected[vector] = {}
            order.append(vector)
        collected[vector][parts[-1]] = value

    properties: dict[str, FakeProperty] = {}
    for vector in order:
        elements = collected[vector]
        kind = _KNOWN_KINDS.get(vector) or _infer_kind(elements.values())
        properties[vector] = FakeProperty(
            name=vector,
            kind=kind,
            values={name: _coerce(kind, value) for name, value in elements.items()},
            # indi_getprop records neither the rule nor the permission. AnyOfMany
            # is the permissive choice: it lets a test write one element without
            # the fixture pretending to know the others were cleared.
            rule="AnyOfMany" if kind is PropertyKind.SWITCH else None,
        )
    return properties


def _infer_kind(values: Iterable[str]) -> PropertyKind:
    listed = list(values)
    if listed and all(value in _SWITCH_VALUES for value in listed):
        return PropertyKind.SWITCH
    if listed and all(_looks_numeric(value) for value in listed):
        return PropertyKind.NUMBER
    return PropertyKind.TEXT


def _looks_numeric(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _coerce(kind: PropertyKind, value: str) -> float | str | bool:
    if kind is PropertyKind.SWITCH:
        return value == "On"
    if kind is PropertyKind.NUMBER:
        return float(value)
    return value


def recorded_mount() -> dict[str, dict[str, FakeProperty]]:
    """The whole recorded mount, ready to hand to :class:`FakeIndiServer`.

    The dump was taken from a connected mount, so CONNECTION.CONNECT is On and
    the baud rate is already 115200. Use :func:`fresh_driver` to test bring-up.
    """
    return {EQMOD_DEVICE: load_recorded_properties()}


def fresh_driver() -> dict[str, dict[str, FakeProperty]]:
    """The recorded mount as the driver presents it *before* CONNECT.

    Two documented departures from the dump, and no others:

    * CONNECTION reads disconnected.
    * DEVICE_BAUD_RATE reads 9600. The driver starts there and has to be told
      otherwise on every connection (FIELD-NOTES-M1 section 2.2); the dump
      shows 115200 because it was taken after Nocturne had set it.

    Note what this fixture *cannot* reproduce: the real driver defines its
    mount-specific vectors (SLEWSPEEDS, STEPPERS, MOUNTINFORMATION) only once
    it has talked to the controller, so they are absent until after CONNECT.
    Here they are present from the start. Anything that reads one of them must
    therefore wait for it rather than assume the cache holds it, and that
    waiting is tested separately against a server that withholds them.
    """
    properties = load_recorded_properties()
    connection = properties["CONNECTION"]
    connection.values = {"CONNECT": False, "DISCONNECT": True}
    baud = properties["DEVICE_BAUD_RATE"]
    baud.values = {name: name == "9600" for name in baud.values}
    return {EQMOD_DEVICE: properties}
