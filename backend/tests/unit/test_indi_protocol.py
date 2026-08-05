"""INDI wire protocol — parsing and serialisation.

The INDI stream is a sequence of XML fragments with no enclosing root element,
delivered over a socket in arbitrary chunks. The parser must therefore be
incremental and must not care where a chunk boundary falls.
"""

from __future__ import annotations

import pytest

from nocturne.executor.indi.protocol import (
    DefVector,
    DelProperty,
    IndiParser,
    IndiProtocolError,
    Message,
    PropertyKind,
    PropertyPermission,
    PropertyState,
    SetVector,
    SwitchRule,
    enable_blob,
    format_number,
    get_properties,
    new_vector,
    parse_number,
)

DEF_SWITCH = b"""<defSwitchVector device="Telescope Simulator" name="CONNECTION"
 label="Connection" group="Main Control" state="Idle" perm="rw" rule="OneOfMany"
 timeout="60" timestamp="2026-08-05T21:00:00">
  <defSwitch name="CONNECT" label="Connect">Off</defSwitch>
  <defSwitch name="DISCONNECT" label="Disconnect">On</defSwitch>
</defSwitchVector>"""

DEF_NUMBER = b"""<defNumberVector device="Focuser Simulator" name="ABS_FOCUS_POSITION"
 label="Absolute Position" group="Main Control" state="Ok" perm="rw" timeout="60">
  <defNumber name="FOCUS_ABSOLUTE_POSITION" label="Ticks" format="%4.0f"
   min="0" max="100000" step="1000">50000</defNumber>
</defNumberVector>"""

DEF_TEXT = b"""<defTextVector device="CCD Simulator" name="DRIVER_INFO" label="Driver Info"
 group="General Info" state="Idle" perm="ro" timeout="60">
  <defText name="DRIVER_NAME" label="Name">CCD Simulator</defText>
  <defText name="DRIVER_EXEC" label="Exec">indi_simulator_ccd</defText>
</defTextVector>"""

DEF_LIGHT = b"""<defLightVector device="Telescope Simulator" name="TELESCOPE_STATUS"
 label="Status" group="Main Control" state="Ok">
  <defLight name="SLEWING" label="Slewing">Idle</defLight>
  <defLight name="TRACKING" label="Tracking">Ok</defLight>
</defLightVector>"""

DEF_BLOB = b"""<defBLOBVector device="CCD Simulator" name="CCD1" label="Image Data"
 group="Image Info" state="Idle" perm="ro" timeout="60">
  <defBLOB name="CCD1" label="Image"/>
</defBLOBVector>"""


def parse_one(payload: bytes) -> object:
    """Feed ``payload`` to a fresh parser and return the single message."""
    messages = list(IndiParser().feed(payload))
    assert len(messages) == 1, messages
    return messages[0]


class TestParseDefinitions:
    def test_def_switch_vector(self) -> None:
        message = parse_one(DEF_SWITCH)
        assert isinstance(message, DefVector)
        prop = message.property
        assert prop.device == "Telescope Simulator"
        assert prop.name == "CONNECTION"
        assert prop.kind is PropertyKind.SWITCH
        assert prop.label == "Connection"
        assert prop.group == "Main Control"
        assert prop.state is PropertyState.IDLE
        assert prop.permission is PropertyPermission.READ_WRITE
        assert prop.rule is SwitchRule.ONE_OF_MANY
        assert prop.timeout == pytest.approx(60.0)
        assert prop["CONNECT"] is False
        assert prop["DISCONNECT"] is True
        assert prop.elements["CONNECT"].label == "Connect"

    def test_def_number_vector_keeps_element_metadata(self) -> None:
        message = parse_one(DEF_NUMBER)
        assert isinstance(message, DefVector)
        element = message.property.elements["FOCUS_ABSOLUTE_POSITION"]
        assert element.value == pytest.approx(50000.0)
        assert element.minimum == pytest.approx(0.0)
        assert element.maximum == pytest.approx(100000.0)
        assert element.step == pytest.approx(1000.0)
        assert element.format == "%4.0f"

    def test_def_text_vector(self) -> None:
        message = parse_one(DEF_TEXT)
        assert isinstance(message, DefVector)
        assert message.property.kind is PropertyKind.TEXT
        assert message.property.permission is PropertyPermission.READ_ONLY
        assert message.property["DRIVER_EXEC"] == "indi_simulator_ccd"

    def test_def_light_vector(self) -> None:
        message = parse_one(DEF_LIGHT)
        assert isinstance(message, DefVector)
        assert message.property.kind is PropertyKind.LIGHT
        assert message.property["TRACKING"] == "Ok"
        # A light vector has no permission of its own; it is always read-only.
        assert message.property.permission is PropertyPermission.READ_ONLY

    def test_def_blob_vector_has_no_values_yet(self) -> None:
        message = parse_one(DEF_BLOB)
        assert isinstance(message, DefVector)
        assert message.property.kind is PropertyKind.BLOB
        assert message.property.elements["CCD1"].value == b""

    def test_missing_device_attribute_is_a_protocol_error(self) -> None:
        with pytest.raises(IndiProtocolError, match="device"):
            parse_one(b'<defTextVector name="X" state="Idle" perm="ro"/>')

    def test_unknown_state_is_a_protocol_error(self) -> None:
        payload = b'<defTextVector device="D" name="X" state="Confused" perm="ro"/>'
        with pytest.raises(IndiProtocolError, match="Confused"):
            parse_one(payload)


class TestParseUpdates:
    def test_set_number_vector_carries_only_changed_elements(self) -> None:
        payload = (
            b'<setNumberVector device="Focuser Simulator" name="ABS_FOCUS_POSITION"'
            b' state="Busy" timeout="60" timestamp="2026-08-05T21:00:01">'
            b'<oneNumber name="FOCUS_ABSOLUTE_POSITION">42000</oneNumber>'
            b"</setNumberVector>"
        )
        message = parse_one(payload)
        assert isinstance(message, SetVector)
        assert message.device == "Focuser Simulator"
        assert message.name == "ABS_FOCUS_POSITION"
        assert message.state is PropertyState.BUSY
        assert message.values == {"FOCUS_ABSOLUTE_POSITION": 42000.0}

    def test_set_switch_vector(self) -> None:
        payload = (
            b'<setSwitchVector device="Telescope Simulator" name="CONNECTION" state="Ok">'
            b'<oneSwitch name="CONNECT">On</oneSwitch>'
            b'<oneSwitch name="DISCONNECT">Off</oneSwitch>'
            b"</setSwitchVector>"
        )
        message = parse_one(payload)
        assert isinstance(message, SetVector)
        assert message.values == {"CONNECT": True, "DISCONNECT": False}

    def test_set_vector_without_state_leaves_state_unknown(self) -> None:
        payload = (
            b'<setTextVector device="D" name="P"><oneText name="E">v</oneText></setTextVector>'
        )
        message = parse_one(payload)
        assert isinstance(message, SetVector)
        assert message.state is None

    def test_del_property_for_one_property(self) -> None:
        payload = b'<delProperty device="Focuser Simulator" name="ABS_FOCUS_POSITION"/>'
        message = parse_one(payload)
        assert isinstance(message, DelProperty)
        assert message.device == "Focuser Simulator"
        assert message.name == "ABS_FOCUS_POSITION"

    def test_del_property_for_a_whole_device(self) -> None:
        """This is how a driver going away is announced."""
        message = parse_one(b'<delProperty device="Focuser Simulator"/>')
        assert isinstance(message, DelProperty)
        assert message.name is None
        assert message.is_whole_device

    def test_message(self) -> None:
        payload = (
            b'<message device="Telescope Simulator" timestamp="2026-08-05T21:00:02"'
            b' message="[INFO] Telescope is online."/>'
        )
        message = parse_one(payload)
        assert isinstance(message, Message)
        assert message.device == "Telescope Simulator"
        assert message.text == "[INFO] Telescope is online."


class TestIncrementalParsing:
    def test_several_messages_in_one_chunk(self) -> None:
        messages = list(IndiParser().feed(DEF_SWITCH + DEF_NUMBER + DEF_TEXT))
        assert [type(m).__name__ for m in messages] == ["DefVector"] * 3

    @pytest.mark.parametrize("split_at", [1, 17, 60, 120, 200])
    def test_a_message_split_across_chunks(self, split_at: int) -> None:
        parser = IndiParser()
        head, tail = DEF_SWITCH[:split_at], DEF_SWITCH[split_at:]
        assert list(parser.feed(head)) == []
        messages = list(parser.feed(tail))
        assert len(messages) == 1
        assert isinstance(messages[0], DefVector)

    def test_byte_at_a_time(self) -> None:
        parser = IndiParser()
        received = [m for byte in DEF_NUMBER for m in parser.feed(bytes([byte]))]
        assert len(received) == 1

    def test_parser_does_not_accumulate_parsed_messages(self) -> None:
        """A night's stream is millions of messages; nothing may be retained."""
        parser = IndiParser()
        for _ in range(200):
            list(parser.feed(DEF_NUMBER))
        assert parser.buffered_bytes == 0

    def test_a_message_is_yielded_without_waiting_for_the_next_one(self) -> None:
        """A reply must not sit in the buffer until unrelated traffic arrives."""
        parser = IndiParser()
        assert len(list(parser.feed(DEF_SWITCH))) == 1
        assert parser.buffered_bytes == 0

    def test_greater_than_inside_an_attribute_does_not_end_the_message(self) -> None:
        payload = b'<message device="D" message="slew &gt; limit: use &lt;x&gt;"/>'
        message = parse_one(payload)
        assert isinstance(message, Message)
        assert message.text == "slew > limit: use <x>"

    def test_quote_inside_an_attribute_does_not_end_the_message(self) -> None:
        message = parse_one(b"""<message device='D' message='he said \"go\"'/>""")
        assert isinstance(message, Message)
        assert message.text == 'he said "go"'

    def test_whitespace_between_messages_is_ignored(self) -> None:
        parser = IndiParser()
        messages = list(parser.feed(b"\n  " + DEF_TEXT + b"\n\n" + DEF_LIGHT + b"  \n"))
        assert len(messages) == 2
        assert parser.buffered_bytes == 0

    def test_xml_declaration_is_skipped(self) -> None:
        parser = IndiParser()
        messages = list(parser.feed(b'<?xml version="1.0"?>' + DEF_TEXT))
        assert len(messages) == 1

    def test_malformed_xml_raises(self) -> None:
        parser = IndiParser()
        with pytest.raises(IndiProtocolError, match="malformed"):
            list(parser.feed(b"<defSwitchVector device=unquoted></defSwitchVector>"))

    def test_an_unbounded_message_is_refused(self) -> None:
        parser = IndiParser(max_message_bytes=64)
        with pytest.raises(IndiProtocolError, match="lost framing"):
            list(parser.feed(b"<defTextVector " + b"x" * 200))

    def test_unknown_element_is_ignored(self) -> None:
        """Forward compatibility: an unrecognised top-level tag is skipped."""
        parser = IndiParser()
        assert list(parser.feed(b"<somethingNew device='D'/>" + DEF_TEXT)) != []


class TestNumbers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("42", 42.0),
            ("-3.5", -3.5),
            ("  7  ", 7.0),
            ("12:30:00", 12.5),
            ("12:30", 12.5),
            ("-12:30:00", -12.5),
            ("12 30 00", 12.5),
            ("-0:30:00", -0.5),
            ("1:00:36", 1.01),
        ],
    )
    def test_parse_number(self, text: str, expected: float) -> None:
        assert parse_number(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "abc", "12:xx", "1:2:3:4"])
    def test_unparseable_number_raises(self, text: str) -> None:
        with pytest.raises(IndiProtocolError):
            parse_number(text)

    def test_negative_sexagesimal_applies_the_sign_to_every_part(self) -> None:
        assert parse_number("-1:30:00") == pytest.approx(-1.5)

    def test_format_number_round_trips(self) -> None:
        assert parse_number(format_number(12.5)) == pytest.approx(12.5)


class TestSerialisation:
    def test_get_properties_without_arguments(self) -> None:
        payload = get_properties()
        assert payload.startswith(b"<getProperties")
        assert b'version="1.7"' in payload

    def test_get_properties_for_one_device(self) -> None:
        assert b'device="CCD Simulator"' in get_properties(device="CCD Simulator")

    def test_get_properties_for_one_property(self) -> None:
        payload = get_properties(device="CCD Simulator", name="CONNECTION")
        assert b'name="CONNECTION"' in payload

    def test_new_switch_vector(self) -> None:
        payload = new_vector(
            PropertyKind.SWITCH,
            device="Telescope Simulator",
            name="CONNECTION",
            values={"CONNECT": True, "DISCONNECT": False},
        )
        assert b"<newSwitchVector" in payload
        assert b'<oneSwitch name="CONNECT">On</oneSwitch>' in payload
        assert b'<oneSwitch name="DISCONNECT">Off</oneSwitch>' in payload

    def test_new_number_vector(self) -> None:
        payload = new_vector(
            PropertyKind.NUMBER,
            device="Focuser Simulator",
            name="ABS_FOCUS_POSITION",
            values={"FOCUS_ABSOLUTE_POSITION": 42000},
        )
        assert b"<newNumberVector" in payload
        assert b'<oneNumber name="FOCUS_ABSOLUTE_POSITION">42000' in payload

    def test_new_text_vector_escapes_markup(self) -> None:
        payload = new_vector(
            PropertyKind.TEXT,
            device="D",
            name="P",
            values={"E": '<script>&"'},
        )
        assert b"<script>" not in payload.split(b"<oneText")[1]
        assert b"&lt;script&gt;" in payload

    def test_attribute_values_are_escaped(self) -> None:
        payload = new_vector(PropertyKind.TEXT, device='D"><evil', name="P", values={"E": "v"})
        assert b'"><evil' not in payload

    def test_new_vector_round_trips_through_the_parser(self) -> None:
        payload = new_vector(PropertyKind.NUMBER, device="D", name="P", values={"E": 12.5})
        message = parse_one(payload.replace(b"newNumberVector", b"setNumberVector"))
        assert isinstance(message, SetVector)
        assert message.values == {"E": pytest.approx(12.5)}

    def test_new_vector_rejects_light_kind(self) -> None:
        """Lights are read-only by definition; a client cannot write one."""
        with pytest.raises(ValueError, match="LIGHT"):
            new_vector(PropertyKind.LIGHT, device="D", name="P", values={"E": "Ok"})

    def test_new_vector_rejects_empty_values(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            new_vector(PropertyKind.NUMBER, device="D", name="P", values={})

    def test_enable_blob(self) -> None:
        assert enable_blob("Never") == b"<enableBLOB>Never</enableBLOB>"
        payload = enable_blob("Also", device="CCD Simulator")
        assert b'device="CCD Simulator"' in payload

    def test_enable_blob_rejects_unknown_mode(self) -> None:
        with pytest.raises(ValueError, match="Sometimes"):
            enable_blob("Sometimes")


class TestPropertyValueAccess:
    def test_unknown_element_raises_key_error(self) -> None:
        message = parse_one(DEF_SWITCH)
        assert isinstance(message, DefVector)
        with pytest.raises(KeyError, match="MAYBE"):
            message.property["MAYBE"]

    def test_property_is_writable(self) -> None:
        writable = parse_one(DEF_SWITCH)
        read_only = parse_one(DEF_TEXT)
        assert isinstance(writable, DefVector)
        assert isinstance(read_only, DefVector)
        assert writable.property.is_writable
        assert not read_only.property.is_writable

    def test_applying_an_update_returns_a_new_property(self) -> None:
        message = parse_one(DEF_NUMBER)
        assert isinstance(message, DefVector)
        original = message.property
        update = SetVector(
            device=original.device,
            name=original.name,
            kind=PropertyKind.NUMBER,
            state=PropertyState.OK,
            timeout=None,
            timestamp=None,
            values={"FOCUS_ABSOLUTE_POSITION": 123.0},
        )
        updated = original.apply(update)
        assert updated["FOCUS_ABSOLUTE_POSITION"] == pytest.approx(123.0)
        assert original["FOCUS_ABSOLUTE_POSITION"] == pytest.approx(50000.0)
        assert updated.elements["FOCUS_ABSOLUTE_POSITION"].maximum == pytest.approx(100000.0)

    def test_applying_an_update_for_an_unknown_element_is_ignored(self) -> None:
        message = parse_one(DEF_NUMBER)
        assert isinstance(message, DefVector)
        updated = message.property.apply(
            SetVector(
                device="Focuser Simulator",
                name="ABS_FOCUS_POSITION",
                kind=PropertyKind.NUMBER,
                state=None,
                timeout=None,
                timestamp=None,
                values={"NOT_A_REAL_ELEMENT": 1.0},
            )
        )
        assert "NOT_A_REAL_ELEMENT" not in updated.elements
