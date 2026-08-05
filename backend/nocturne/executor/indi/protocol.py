"""The INDI wire protocol — SPEC section 3, layer 0.

INDI is a stream of XML fragments with no enclosing root element. A client
sends ``getProperties`` and ``new*Vector``; the server replies with
``def*Vector`` (a property is defined), ``set*Vector`` (its value or state
changed), ``delProperty`` (it went away) and ``message`` (free text).

This module is pure: it turns bytes into message objects and values into bytes.
It owns no sockets and no state beyond the incremental parser's own buffer.

Reference: the INDI protocol specification, version 1.7.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final, TypeVar, final
from xml.etree import ElementTree  # local trusted peer; see module docstring
from xml.sax.saxutils import escape, quoteattr

#: Protocol version advertised in ``getProperties``.
PROTOCOL_VERSION: Final = "1.7"

#: BLOB delivery modes (INDI ``enableBLOB``). Nocturne pulls FITS from disk, not
#: over the wire, so the client never leaves this at anything but ``Never`` in M1.
BLOB_MODES: Final[frozenset[str]] = frozenset({"Never", "Also", "Only"})


class IndiProtocolError(Exception):
    """The peer sent something that is not valid INDI."""


class PropertyKind(StrEnum):
    """The five INDI property vector types."""

    TEXT = "Text"
    NUMBER = "Number"
    SWITCH = "Switch"
    LIGHT = "Light"
    BLOB = "BLOB"


class PropertyState(StrEnum):
    """Vector state as reported by the driver."""

    IDLE = "Idle"
    OK = "Ok"
    BUSY = "Busy"
    ALERT = "Alert"


class PropertyPermission(StrEnum):
    """Client access to a vector."""

    READ_ONLY = "ro"
    WRITE_ONLY = "wo"
    READ_WRITE = "rw"


class SwitchRule(StrEnum):
    """Switch vector selection rule."""

    ONE_OF_MANY = "OneOfMany"
    AT_MOST_ONE = "AtMostOne"
    ANY_OF_MANY = "AnyOfMany"


#: The value an element can hold, by kind: text -> str, number -> float,
#: switch -> bool, light -> str (a PropertyState name), BLOB -> bytes.
ElementValue = str | float | bool | bytes

_KIND_BY_DEF_TAG: Final[Mapping[str, PropertyKind]] = {
    f"def{kind.value}Vector": kind for kind in PropertyKind
}
_KIND_BY_SET_TAG: Final[Mapping[str, PropertyKind]] = {
    f"set{kind.value}Vector": kind for kind in PropertyKind
}
_SEXAGESIMAL_SEPARATORS: Final = re.compile(r"[:\s;]+")

_EnumT = TypeVar("_EnumT", bound=StrEnum)


def parse_number(text: str) -> float:
    """Parse an INDI number: decimal, or sexagesimal ``d:m:s`` / ``d m s``.

    Raises:
        IndiProtocolError: if ``text`` is not a number in either form.
    """
    stripped = text.strip()
    if not stripped:
        raise IndiProtocolError("empty number")
    try:
        return float(stripped)
    except ValueError:
        pass

    parts = _SEXAGESIMAL_SEPARATORS.split(stripped)
    if not 2 <= len(parts) <= 3:
        raise IndiProtocolError(f"not a number: {text!r}")
    try:
        values = [float(part) for part in parts]
    except ValueError as exc:
        raise IndiProtocolError(f"not a number: {text!r}") from exc

    # The sign belongs to the whole quantity, not just the degrees field:
    # "-0:30:00" is minus half a degree, not minus zero plus half.
    sign = -1.0 if stripped.lstrip().startswith("-") else 1.0
    magnitude = abs(values[0]) + values[1] / 60.0
    if len(values) == 3:
        magnitude += values[2] / 3600.0
    return sign * magnitude


def format_number(value: float) -> str:
    """Render a number for the wire. INDI accepts plain decimal everywhere."""
    return repr(float(value))


def _require(element: ElementTree.Element, attribute: str) -> str:
    value = element.get(attribute)
    if value is None:
        raise IndiProtocolError(
            f"<{element.tag}> is missing the required attribute {attribute!r}"
        )
    return value


def _parse_enum(enum_type: type[_EnumT], raw: str, attribute: str) -> _EnumT:
    try:
        return enum_type(raw)
    except ValueError as exc:
        permitted = ", ".join(member.value for member in enum_type)
        raise IndiProtocolError(
            f"invalid {attribute} {raw!r}; expected one of: {permitted}"
        ) from exc


def _parse_optional_float(element: ElementTree.Element, attribute: str) -> float | None:
    raw = element.get(attribute)
    if raw is None:
        return None
    return parse_number(raw)


@final
@dataclass(frozen=True, slots=True)
class Element:
    """One member of a property vector."""

    name: str
    label: str
    value: ElementValue
    #: Number elements only; ``None`` for every other kind.
    format: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None


@final
@dataclass(frozen=True, slots=True)
class Property:
    """A property vector: the unit INDI defines, updates and deletes."""

    device: str
    name: str
    kind: PropertyKind
    label: str
    group: str
    state: PropertyState
    permission: PropertyPermission
    timeout: float
    elements: Mapping[str, Element]
    rule: SwitchRule | None = None
    timestamp: str | None = None

    def __getitem__(self, element: str) -> ElementValue:
        """Value of ``element``.

        Raises:
            KeyError: if the vector has no such element.
        """
        try:
            return self.elements[element].value
        except KeyError as exc:
            available = ", ".join(sorted(self.elements)) or "<none>"
            raise KeyError(
                f"{self.device}.{self.name} has no element {element!r}; it has: {available}"
            ) from exc

    def get(self, element: str, default: ElementValue | None = None) -> ElementValue | None:
        """Value of ``element``, or ``default`` if it is absent."""
        member = self.elements.get(element)
        return default if member is None else member.value

    @property
    def is_writable(self) -> bool:
        """Whether a client may write this vector."""
        return self.permission in (
            PropertyPermission.WRITE_ONLY,
            PropertyPermission.READ_WRITE,
        )

    @property
    def selected_switch(self) -> str | None:
        """Name of the switch that is On, for OneOfMany vectors."""
        for element in self.elements.values():
            if element.value is True:
                return element.name
        return None

    def apply(self, update: SetVector) -> Property:
        """Return a copy with ``update`` applied.

        Element metadata (limits, format) is preserved: a ``set*Vector`` carries
        values only. Elements the driver did not define are ignored rather than
        invented.
        """
        elements = dict(self.elements)
        for name, value in update.values.items():
            existing = elements.get(name)
            if existing is None:
                continue
            elements[name] = replace(existing, value=value)
        return replace(
            self,
            elements=elements,
            state=self.state if update.state is None else update.state,
            timeout=self.timeout if update.timeout is None else update.timeout,
            timestamp=update.timestamp or self.timestamp,
        )


@final
@dataclass(frozen=True, slots=True)
class DefVector:
    """A driver defined a property vector."""

    property: Property


@final
@dataclass(frozen=True, slots=True)
class SetVector:
    """A driver changed the value or state of an existing vector."""

    device: str
    name: str
    kind: PropertyKind
    state: PropertyState | None
    timeout: float | None
    timestamp: str | None
    values: Mapping[str, ElementValue]


@final
@dataclass(frozen=True, slots=True)
class DelProperty:
    """A property vector, or a whole device, went away."""

    device: str
    name: str | None = None
    timestamp: str | None = None

    @property
    def is_whole_device(self) -> bool:
        """True when the driver, not just one property, disappeared."""
        return self.name is None


@final
@dataclass(frozen=True, slots=True)
class Message:
    """Free text from a driver or from the server."""

    text: str
    device: str | None = None
    timestamp: str | None = None


IndiMessage = DefVector | SetVector | DelProperty | Message


def _element_value(kind: PropertyKind, node: ElementTree.Element) -> ElementValue:
    raw = (node.text or "").strip()
    match kind:
        case PropertyKind.NUMBER:
            return parse_number(raw)
        case PropertyKind.SWITCH:
            if raw not in ("On", "Off"):
                raise IndiProtocolError(f"invalid switch value {raw!r}; expected On or Off")
            return raw == "On"
        case PropertyKind.BLOB:
            if not raw:
                return b""
            try:
                return base64.b64decode(raw, validate=True)
            except ValueError as exc:
                raise IndiProtocolError("invalid base64 in BLOB element") from exc
        case _:
            return raw


def _parse_def_vector(kind: PropertyKind, node: ElementTree.Element) -> DefVector:
    elements: dict[str, Element] = {}
    for child in node:
        name = _require(child, "name")
        elements[name] = Element(
            name=name,
            label=child.get("label", name),
            value=_element_value(kind, child),
            format=child.get("format"),
            minimum=_parse_optional_float(child, "min"),
            maximum=_parse_optional_float(child, "max"),
            step=_parse_optional_float(child, "step"),
        )

    # Light vectors carry no perm attribute: they are always read-only.
    if kind is PropertyKind.LIGHT:
        permission = PropertyPermission.READ_ONLY
    else:
        permission = _parse_enum(PropertyPermission, _require(node, "perm"), "perm")

    raw_rule = node.get("rule")
    device = _require(node, "device")
    name = _require(node, "name")
    return DefVector(
        property=Property(
            device=device,
            name=name,
            kind=kind,
            label=node.get("label", name),
            group=node.get("group", ""),
            state=_parse_enum(PropertyState, _require(node, "state"), "state"),
            permission=permission,
            timeout=_parse_optional_float(node, "timeout") or 0.0,
            elements=elements,
            rule=None if raw_rule is None else _parse_enum(SwitchRule, raw_rule, "rule"),
            timestamp=node.get("timestamp"),
        )
    )


def _parse_set_vector(kind: PropertyKind, node: ElementTree.Element) -> SetVector:
    values: dict[str, ElementValue] = {
        _require(child, "name"): _element_value(kind, child) for child in node
    }
    raw_state = node.get("state")
    return SetVector(
        device=_require(node, "device"),
        name=_require(node, "name"),
        kind=kind,
        state=None if raw_state is None else _parse_enum(PropertyState, raw_state, "state"),
        timeout=_parse_optional_float(node, "timeout"),
        timestamp=node.get("timestamp"),
        values=values,
    )


def _build_message(node: ElementTree.Element) -> IndiMessage | None:
    tag = node.tag
    if (def_kind := _KIND_BY_DEF_TAG.get(tag)) is not None:
        return _parse_def_vector(def_kind, node)
    if (set_kind := _KIND_BY_SET_TAG.get(tag)) is not None:
        return _parse_set_vector(set_kind, node)
    if tag == "delProperty":
        return DelProperty(
            device=_require(node, "device"),
            name=node.get("name"),
            timestamp=node.get("timestamp"),
        )
    if tag == "message":
        return Message(
            text=node.get("message", ""),
            device=node.get("device"),
            timestamp=node.get("timestamp"),
        )
    # Forward compatibility: a tag this version does not know is not an error.
    return None


_LT: Final = ord("<")
_GT: Final = ord(">")
_SLASH: Final = ord("/")
_QUOTES: Final = (ord('"'), ord("'"))
_QUESTION: Final = ord("?")
_BANG: Final = ord("!")

#: Refuse to buffer more than this for a single message. An INDI message is a
#: few hundred bytes; anything approaching this is a peer that has lost framing,
#: and buffering it without bound would be the failure, not the symptom.
MAX_MESSAGE_BYTES: Final = 1 << 20


@final
class _Framer:
    """Splits the INDI byte stream into complete top-level XML fragments.

    INDI sends a bare sequence of elements with no document element, so an XML
    parser cannot tell where one message ends. Framing is done here, explicitly,
    rather than by relying on when the underlying expat parser happens to
    surface an end-element event: expat defers those, and the deferral is not
    controllable on every Python that Raspberry Pi OS ships. A message that has
    arrived in full is a message we can act on immediately.

    The grammar INDI needs is small — elements, attributes, escaped text. There
    are no comments, CDATA sections or entity declarations on the wire, and any
    ``<?...?>`` or ``<!...>`` that did appear is skipped rather than counted.
    """

    def __init__(self, max_message_bytes: int = MAX_MESSAGE_BYTES) -> None:
        self._max_message_bytes = max_message_bytes
        self._buffer = bytearray()
        self._scan = 0
        self._depth = 0
        self._fragment_start: int | None = None
        self._in_tag = False
        self._tag_start = 0
        self._quote: int | None = None

    @property
    def buffered_bytes(self) -> int:
        """Bytes held for a message that has not finished arriving."""
        return len(self._buffer)

    def feed(self, data: bytes) -> Iterator[bytes]:
        """Append ``data`` and yield every complete top-level fragment in it."""
        self._buffer += data
        while self._scan < len(self._buffer):
            byte = self._buffer[self._scan]

            if self._quote is not None:
                if byte == self._quote:
                    self._quote = None
                self._scan += 1
                continue

            if not self._in_tag:
                if byte == _LT:
                    self._in_tag = True
                    self._tag_start = self._scan
                    if self._depth == 0:
                        self._fragment_start = self._scan
                self._scan += 1
                continue

            if byte in _QUOTES:
                self._quote = byte
                self._scan += 1
                continue

            if byte != _GT:
                self._scan += 1
                continue

            fragment = self._close_tag()
            self._scan += 1
            if fragment is not None:
                yield fragment

        self._drop_inter_message_noise()
        self._guard_buffer_size()

    def _close_tag(self) -> bytes | None:
        """Handle the ``>`` at ``self._scan``; return a fragment if one ended."""
        self._in_tag = False
        first = self._tag_start + 1
        after_lt = self._buffer[first] if first < len(self._buffer) else 0
        before_gt = self._buffer[self._scan - 1] if self._scan > self._tag_start else 0

        if after_lt in (_QUESTION, _BANG):
            # Declaration or comment: not part of the element structure.
            if self._depth == 0:
                self._discard_through(self._scan)
            return None

        if after_lt == _SLASH:
            self._depth -= 1
        elif before_gt != _SLASH:
            self._depth += 1

        if self._depth > 0 or self._fragment_start is None:
            return None

        fragment = bytes(self._buffer[self._fragment_start : self._scan + 1])
        self._discard_through(self._scan)
        return fragment

    def _drop_inter_message_noise(self) -> None:
        """Discard whatever sits between messages: it is whitespace, not data."""
        if self._depth == 0 and not self._in_tag and self._fragment_start is None:
            self._buffer.clear()
            self._scan = 0

    def _discard_through(self, index: int) -> None:
        del self._buffer[: index + 1]
        self._scan = -1  # incremented by the caller back to 0
        self._fragment_start = None
        self._depth = 0

    def _guard_buffer_size(self) -> None:
        if len(self._buffer) > self._max_message_bytes:
            raise IndiProtocolError(
                f"INDI message exceeds {self._max_message_bytes} bytes without "
                "closing; the stream has lost framing"
            )


@final
class IndiParser:
    """Incremental parser for the INDI stream.

    Feed it whatever arrives from the socket, in whatever sized chunks; it
    yields complete messages as soon as they close. Nothing is retained between
    messages, so a night-long stream costs constant memory.
    """

    def __init__(self, max_message_bytes: int = MAX_MESSAGE_BYTES) -> None:
        self._framer = _Framer(max_message_bytes)

    @property
    def buffered_bytes(self) -> int:
        """Bytes held for a message that has not finished arriving."""
        return self._framer.buffered_bytes

    def feed(self, data: bytes) -> Iterator[IndiMessage]:
        """Parse ``data`` and yield every message that completed within it."""
        for fragment in self._framer.feed(data):
            try:
                # The peer is an indiserver on the loopback interface that this
                # host started; the payload is not attacker-controlled input.
                node = ElementTree.fromstring(fragment)  # noqa: S314
            except ElementTree.ParseError as exc:
                raise IndiProtocolError(
                    f"malformed INDI message: {exc}: {fragment[:200]!r}"
                ) from exc
            message = _build_message(node)
            if message is not None:
                yield message


def get_properties(device: str | None = None, name: str | None = None) -> bytes:
    """Ask the server to define properties, optionally narrowed to one device."""
    attributes = [f"version={quoteattr(PROTOCOL_VERSION)}"]
    if device is not None:
        attributes.append(f"device={quoteattr(device)}")
    if name is not None:
        attributes.append(f"name={quoteattr(name)}")
    return f"<getProperties {' '.join(attributes)}/>".encode()


def _render_value(kind: PropertyKind, value: ElementValue) -> str:
    match kind:
        case PropertyKind.SWITCH:
            return "On" if value else "Off"
        case PropertyKind.NUMBER:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"a number element needs a number, got {value!r}")
            return format_number(value)
        case PropertyKind.BLOB:
            if not isinstance(value, bytes):
                raise ValueError(f"a BLOB element needs bytes, got {type(value).__name__}")
            return base64.b64encode(value).decode("ascii")
        case _:
            return escape(str(value))


def new_vector(
    kind: PropertyKind,
    *,
    device: str,
    name: str,
    values: Mapping[str, ElementValue],
) -> bytes:
    """Serialise a client write of ``values`` into ``device``'s ``name`` vector.

    Raises:
        ValueError: for an empty write, or for a light vector, which is
            read-only by definition.
    """
    if kind is PropertyKind.LIGHT:
        raise ValueError("a LIGHT vector is read-only and cannot be written by a client")
    if not values:
        raise ValueError(f"refusing to send an empty {kind.value} vector to {device}.{name}")

    one = f"one{kind.value}"
    body = "".join(
        f"<{one} name={quoteattr(element)}>{_render_value(kind, value)}</{one}>"
        for element, value in values.items()
    )
    return (
        f"<new{kind.value}Vector device={quoteattr(device)} name={quoteattr(name)}>"
        f"{body}"
        f"</new{kind.value}Vector>"
    ).encode()


def enable_blob(mode: str, device: str | None = None, name: str | None = None) -> bytes:
    """Set BLOB delivery for a device or property.

    Raises:
        ValueError: for a mode INDI does not define.
    """
    if mode not in BLOB_MODES:
        raise ValueError(
            f"unknown enableBLOB mode {mode!r}; expected one of: "
            f"{', '.join(sorted(BLOB_MODES))}"
        )
    attributes = ""
    if device is not None:
        attributes += f" device={quoteattr(device)}"
    if name is not None:
        attributes += f" name={quoteattr(name)}"
    return f"<enableBLOB{attributes}>{mode}</enableBLOB>".encode()
