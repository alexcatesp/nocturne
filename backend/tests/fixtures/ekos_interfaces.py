"""The KStars/Ekos DBus interface, as recorded from the reference rig.

Two files, answering two different questions, both captured 2026-08-07 against
KStars 3.8.3 (commit ``61d849b0``) — see ``docs/ekos-dbus-capture.md``:

``hardware/kstars-dbus-interfaces.xml``
    The nineteen ``org.kde.kstars*.xml`` adaptor definitions checked into the
    KStars source tree that was built. What KStars **declares**.

``hardware/kstars-live-at-rest.xml``
    ``gdbus introspect`` against the running process, before Ekos is started.
    What KStars **exports** at that moment.

The distinction is not pedantry. A method in the first and not the second is an
object that has not been created yet — ``/KStars/Ekos/Capture`` does not exist
until Ekos starts. A method in *neither* is a guess, and every guess in
``nocturne/executor/ekos.py`` was one until these files existed
(https://github.com/alexcatesp/nocturne/issues/2).

This module exists so that tests can assert against what the machine said rather
than against ``fake_kstars.py``, which is a stub written by the author of the
bridge it verifies and therefore shares its assumptions. That is exactly how
``INDI.connect`` came to be wrong in the bridge *and* right-looking in the stub.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

HARDWARE = Path(__file__).parent / "hardware"
DECLARED_DUMP: Final = HARDWARE / "kstars-dbus-interfaces.xml"
EXPORTED_DUMP: Final = HARDWARE / "kstars-live-at-rest.xml"

#: Sections are introduced by a line the capture script printed. One source file
#: ended without a newline, so the marker can begin mid-line; matching on the
#: marker itself rather than on line starts is deliberate.
_SECTION = re.compile(r"=====\s*(\S+)\s*$", re.MULTILINE)

#: Interfaces every DBus object carries. Not KStars', and never evidence that a
#: path is the one wanted.
STANDARD_INTERFACES: Final = frozenset(
    {
        "org.freedesktop.DBus.Introspectable",
        "org.freedesktop.DBus.Peer",
        "org.freedesktop.DBus.Properties",
    }
)


@dataclass(frozen=True)
class Method:
    """One method as declared, with its DBus signatures."""

    name: str
    in_signature: str
    out_signature: str
    argument_names: tuple[str, ...]


@dataclass
class Interface:
    """One DBus interface: its methods, properties and signals."""

    name: str
    #: Keyed by member name. A list, because KStars overloads: ``setAltAz`` and
    #: ``exportImage`` are each declared several times with different arities.
    methods: dict[str, list[Method]] = field(default_factory=dict)
    properties: dict[str, str] = field(default_factory=dict)
    signals: set[str] = field(default_factory=set)

    def method_names(self) -> set[str]:
        return set(self.methods)


def _split_sections(text: str) -> dict[str, str]:
    """``{section label: xml}`` from a concatenated capture."""
    matches = list(_SECTION.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.end() : end]
    return sections


def _parse_node(xml: str) -> dict[str, Interface]:
    # S314: the input is a fixture committed to this repository and captured
    # from the reference rig, not untrusted input. defusedxml would be a new
    # dependency and therefore an ADR (CLAUDE.md section 6) for no gain here.
    root = ElementTree.fromstring(xml)  # noqa: S314
    interfaces: dict[str, Interface] = {}
    for element in root.findall("interface"):
        name = element.get("name")
        if name is None:
            continue
        interface = interfaces.setdefault(name, Interface(name=name))
        for method in element.findall("method"):
            member = method.get("name")
            if member is None:
                continue
            in_types: list[str] = []
            out_types: list[str] = []
            names: list[str] = []
            for arg in method.findall("arg"):
                # KStars' own XML omits direction on some out args; DBus treats
                # a missing direction on a method arg as "in", and so does this.
                target = out_types if arg.get("direction") == "out" else in_types
                target.append(arg.get("type") or "")
                names.append(arg.get("name") or "")
            interface.methods.setdefault(member, []).append(
                Method(
                    name=member,
                    in_signature="".join(in_types),
                    out_signature="".join(out_types),
                    argument_names=tuple(names),
                )
            )
        for prop in element.findall("property"):
            prop_name = prop.get("name")
            if prop_name is not None:
                interface.properties[prop_name] = prop.get("type") or ""
        for signal in element.findall("signal"):
            signal_name = signal.get("name")
            if signal_name is not None:
                interface.signals.add(signal_name)
    return interfaces


def _child_nodes(xml: str) -> set[str]:
    root = ElementTree.fromstring(xml)  # noqa: S314  # see _parse_node
    return {node.get("name") or "" for node in root.findall("node")} - {""}


def declared_interfaces() -> dict[str, Interface]:
    """Every interface KStars 3.8.3 declares, merged across the nineteen files.

    Raises:
        AssertionError: if the parse yields nothing, or is missing an interface
            that is certainly in the dump. Tests here assert that the bridge's
            names appear in this mapping; one that quietly came back empty would
            fail loudly, but one that came back *partially* would not — so the
            landmarks are checked explicitly (CLAUDE.md section 2).
    """
    sections = _split_sections(DECLARED_DUMP.read_text(encoding="utf-8"))
    assert len(sections) == 19, (
        f"expected 19 adaptor files in {DECLARED_DUMP}, got {len(sections)}"
    )

    interfaces: dict[str, Interface] = {}
    for xml in sections.values():
        for name, interface in _parse_node(xml).items():
            existing = interfaces.get(name)
            if existing is None:
                interfaces[name] = interface
                continue
            existing.methods.update(interface.methods)
            existing.properties.update(interface.properties)
            existing.signals |= interface.signals

    for landmark in ("org.kde.kstars.Ekos", "org.kde.kstars.INDI", EKOS_OPTICAL_TRAIN):
        assert landmark in interfaces, f"{landmark} is missing from {DECLARED_DUMP}"
    return interfaces


def exported_objects() -> dict[str, dict[str, Interface]]:
    """``{object path: {interface: Interface}}``, as the running KStars answered.

    Raises:
        AssertionError: if the parse yields nothing or loses a path that is in
            the capture.
    """
    sections = _split_sections(EXPORTED_DUMP.read_text(encoding="utf-8"))
    assert sections, f"no object paths parsed from {EXPORTED_DUMP}"

    objects = {path: _parse_node(xml) for path, xml in sections.items()}
    for landmark in ("/KStars", "/KStars/Ekos", "/KStars/INDI"):
        assert landmark in objects, f"{landmark} is missing from {EXPORTED_DUMP}"
    return objects


def exported_child_nodes() -> dict[str, set[str]]:
    """``{object path: child node names}`` from the same capture."""
    sections = _split_sections(EXPORTED_DUMP.read_text(encoding="utf-8"))
    assert sections, f"no object paths parsed from {EXPORTED_DUMP}"
    return {path: _child_nodes(xml) for path, xml in sections.items()}


#: The interface ADR 0006 and ADR 0008 both cite as the reason for requiring
#: KStars >= 3.8.2. Declared in the built tree; its objects do not exist until
#: Ekos has been started, so nothing here asserts it is exported.
EKOS_OPTICAL_TRAIN: Final = "org.kde.kstars.Ekos.OpticalTrain"

__all__ = [
    "DECLARED_DUMP",
    "EKOS_OPTICAL_TRAIN",
    "EXPORTED_DUMP",
    "STANDARD_INTERFACES",
    "Interface",
    "Method",
    "declared_interfaces",
    "exported_child_nodes",
    "exported_objects",
]
