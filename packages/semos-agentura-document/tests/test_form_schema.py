"""Unit tests for forms/_schema.py and probe-value generation."""

from semos.agentura.document.forms._schema import (
    build_data_from_values,
    field_to_property,
    fields_to_data,
    fields_to_schema,
    flatten_to_field_ids,
)
from semos.agentura.document.forms._visual_inspect import _generate_probe_values, _is_cryptic


def _fields():
    return [
        {"name": "sdt_0", "type": "date", "value": ""},
        {"name": "Approved", "type": "checkbox", "value": False},
        {"name": "City", "type": "dropdown", "options": ["Berlin", "Bonn"], "value": "Berlin"},
        {"name": "Note", "type": "text", "max_length": 30, "value": "hi", "label": "Bemerkung"},
    ]


class TestFieldToProperty:
    def test_type_mapping(self):
        assert field_to_property({"name": "a", "type": "checkbox"})["type"] == "boolean"
        assert field_to_property({"name": "a", "type": "text"})["type"] == "string"
        assert field_to_property({"name": "a", "type": "date"})["format"] == "date"

    def test_x_field_id_and_title(self):
        prop = field_to_property({"name": "sdt_0", "type": "text", "label": "Abreise"})
        assert prop["x-field-id"] == "sdt_0"
        assert prop["title"] == "Abreise"
        assert prop["x-field-type"] == "text"

    def test_enum_and_maxlength_and_default(self):
        prop = field_to_property(
            {"name": "City", "type": "dropdown", "options": ["A", "B"], "max_length": 5, "value": "A"}
        )
        assert prop["enum"] == ["A", "B"]
        assert prop["maxLength"] == 5
        assert prop["default"] == "A"


class TestSchemaAndData:
    def test_schema_keys_equal_field_ids(self):
        schema = fields_to_schema(_fields())
        assert set(schema["properties"]) == {"sdt_0", "Approved", "City", "Note"}

    def test_duplicate_names_disambiguated(self):
        schema = fields_to_schema([{"name": "x", "type": "text"}, {"name": "x", "type": "text"}])
        assert set(schema["properties"]) == {"x", "x__2"}
        assert schema["properties"]["x__2"]["x-field-id"] == "x"

    def test_data_holds_current_values(self):
        data = fields_to_data(_fields())
        assert data == {"Approved": False, "City": "Berlin", "Note": "hi"}


class TestFlatten:
    def test_flat_roundtrip(self):
        schema = fields_to_schema(_fields())
        data = {"Note": "hello", "Approved": True}
        flat = flatten_to_field_ids(schema, data)
        assert flat == {"Note": "hello", "Approved": True}

    def test_renamed_key_resolves_via_x_field_id(self):
        schema = {"type": "object", "properties": {"bemerkung": {"type": "string", "x-field-id": "Note"}}}
        flat = flatten_to_field_ids(schema, {"bemerkung": "hi"})
        assert flat == {"Note": "hi"}

    def test_nested_object_flattens(self):
        schema = {
            "type": "object",
            "properties": {
                "zielort": {
                    "type": "object",
                    "properties": {
                        "strasse": {"type": "string", "x-field-id": "Zielort inkl. Adresse"},
                        "uhrzeit": {"type": "string", "x-field-id": "Uhrzeit #2"},
                    },
                }
            },
        }
        data = {"zielort": {"strasse": "Unter den Linden 6", "uhrzeit": "14:00"}}
        flat = flatten_to_field_ids(schema, data)
        assert flat == {"Zielort inkl. Adresse": "Unter den Linden 6", "Uhrzeit #2": "14:00"}

    def test_unknown_keys_pass_through(self):
        schema = {"type": "object", "properties": {}}
        assert flatten_to_field_ids(schema, {"raw_id": "v"}) == {"raw_id": "v"}

    def test_build_data_from_values_inverse(self):
        schema = {
            "type": "object",
            "properties": {
                "zielort": {
                    "type": "object",
                    "properties": {"strasse": {"type": "string", "x-field-id": "Z"}},
                },
                "note": {"type": "string", "x-field-id": "Note"},
            },
        }
        data = build_data_from_values(schema, {"Z": "addr", "Note": "n"})
        assert data == {"zielort": {"strasse": "addr"}, "note": "n"}


class TestProbeValues:
    def test_type_aware_and_capacity(self):
        probes = _generate_probe_values(_fields())
        assert len(probes["Note"]) == 3  # compact 3-char text probe
        assert isinstance(probes["Approved"], bool)
        assert probes["City"] == "Berlin"  # first option
        assert probes["sdt_0"].endswith("2026")  # date

    def test_text_probes_are_unique(self):
        fields = [{"name": f"f{i}", "type": "text"} for i in range(50)]
        probes = _generate_probe_values(fields)
        assert len(set(probes.values())) == 50

    def test_respects_max_length(self):
        probes = _generate_probe_values([{"name": "tiny", "type": "text", "max_length": 2}])
        assert len(probes["tiny"]) <= 2

    def test_is_cryptic(self):
        assert _is_cryptic({"name": "sdt_0", "type": "date"})
        assert _is_cryptic({"name": "1015", "type": "text"})
        assert _is_cryptic({"name": "1015", "type": "text", "label": "1015"})  # label == name
        assert not _is_cryptic({"name": "x", "type": "text", "label": "Full Name"})
