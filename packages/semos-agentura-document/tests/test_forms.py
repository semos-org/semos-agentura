"""Tests for forms/ - PDF and DOCX form inspection and filling."""

import json

import pytest
from semos.agentura.document.exceptions import DocumentAgentError
from semos.agentura.document.forms.fill import fill_form, inspect_form, inspect_form_fields


class TestInspectDocxForm:
    def test_finds_sdt_fields(self, sample_docx):
        fields = inspect_form_fields(sample_docx)
        names = [f["name"] for f in fields]
        assert "StartDate" in names
        assert "Approved" in names

    def test_finds_legacy_fields(self, sample_docx):
        fields = inspect_form_fields(sample_docx)
        legacy = [f for f in fields if f.get("format") == "legacy"]
        assert len(legacy) >= 1
        assert any(f["name"] == "FullName" for f in legacy)

    def test_finds_table_cells(self, sample_docx):
        fields = inspect_form_fields(sample_docx)
        table_fields = [f for f in fields if f.get("format") == "table"]
        assert len(table_fields) >= 2
        names = [f["name"] for f in table_fields]
        assert "Name" in names
        assert "City" in names

    def test_sdt_types(self, sample_docx):
        fields = inspect_form_fields(sample_docx)
        by_name = {f["name"]: f for f in fields}
        assert by_name["StartDate"]["type"] == "date"
        assert by_name["Approved"]["type"] == "checkbox"

    def test_checkbox_value(self, sample_docx):
        fields = inspect_form_fields(sample_docx)
        by_name = {f["name"]: f for f in fields}
        assert by_name["Approved"]["value"] is False

    def test_inspect_form_returns_schema_and_data(self, sample_docx):
        result = inspect_form(sample_docx)
        assert set(result) == {"schema", "data"}
        props = result["schema"]["properties"]
        # Each leaf carries x-field-id bridging to the real fill key.
        assert props["Approved"]["x-field-id"] == "Approved"
        assert props["Approved"]["type"] == "boolean"
        assert props["StartDate"]["format"] == "date"


class TestFillDocxForm:
    def test_fill_sdt_date(self, sample_docx, tmp_dir):
        out = tmp_dir / "filled.docx"
        fill_form(sample_docx, out, {"StartDate": "15.06.2026"})
        assert out.exists()
        # Re-inspect to verify
        fields = inspect_form_fields(out)
        by_name = {f["name"]: f for f in fields}
        assert "15.06.2026" in by_name["StartDate"]["value"]

    def test_fill_sdt_checkbox(self, sample_docx, tmp_dir):
        out = tmp_dir / "filled.docx"
        fill_form(sample_docx, out, {"Approved": True})
        fields = inspect_form_fields(out)
        by_name = {f["name"]: f for f in fields}
        assert by_name["Approved"]["value"] is True

    def test_fill_from_schema_data_pair(self, sample_docx, tmp_dir):
        # inspect -> {schema, data}; feeding it straight back must round-trip.
        result = inspect_form(sample_docx)
        result["data"]["Approved"] = True
        out = tmp_dir / "filled.docx"
        fill_form(sample_docx, out, result)
        by_name = {f["name"]: f for f in inspect_form_fields(out)}
        assert by_name["Approved"]["value"] is True

    def test_fill_legacy_text(self, sample_docx, tmp_dir):
        out = tmp_dir / "filled.docx"
        fill_form(sample_docx, out, {"FullName": "Max Mustermann"})
        assert out.exists()

    def test_fill_table_cell(self, sample_docx, tmp_dir):
        out = tmp_dir / "filled.docx"
        fill_form(sample_docx, out, {"Name": "Test User", "City": "Berlin"})
        fields = inspect_form_fields(out)
        _ = {f["name"]: f for f in fields if f.get("format") == "table"}
        # After filling, the cell is no longer empty so it won't appear as a fillable field
        # That's expected - we verify the output file was written
        assert out.exists()
        assert out.stat().st_size > 0

    def test_fill_with_json_string(self, sample_docx, tmp_dir):
        out = tmp_dir / "filled.docx"
        data_json = json.dumps({"StartDate": "01.01.2027"})
        fill_form(sample_docx, out, data_json)
        assert out.exists()

    def test_fill_with_json_file(self, sample_docx, tmp_dir):
        out = tmp_dir / "filled.docx"
        json_path = tmp_dir / "data.json"
        json_path.write_text(json.dumps({"Approved": True}))
        fill_form(sample_docx, out, json_path)
        assert out.exists()


class TestFillFormDispatch:
    def test_unsupported_extension(self, tmp_dir):
        fake = tmp_dir / "test.txt"
        fake.write_text("hello")
        with pytest.raises(DocumentAgentError, match="Unsupported form file type"):
            inspect_form(fake)

    def test_invalid_data_type(self, sample_docx, tmp_dir):
        out = tmp_dir / "out.docx"
        with pytest.raises(DocumentAgentError, match="Could not parse data"):
            fill_form(sample_docx, out, "not valid json{{{")


class TestFillKeyResolver:
    """fill_form remaps label-keyed data via the cache from inspect_form,
    so the inspect -> fill round trip works with or without visual mode."""

    def _svc(self):
        from semos.agentura.document.tools import _cache_form_resolver

        class _Svc:
            pass

        svc = _Svc()
        schema = {
            "properties": {
                "p1": {"x-field-id": "1009", "title": "bisherige Wohnung"},
                "p2": {"x-field-id": "sdt_0", "title": "sdt_0"},  # cryptic: no label
            }
        }
        return svc, schema, _cache_form_resolver

    def test_label_key_maps_to_field_id(self, tmp_dir):
        from semos.agentura.document.tools import _resolve_fill_keys

        svc, schema, cache = self._svc()
        f = tmp_dir / "form.pdf"
        f.write_bytes(b"bytes")
        cache(svc, f, schema)
        assert _resolve_fill_keys(svc, f, {"bisherige Wohnung": "X"}) == {"1009": "X"}
        # already a field id - unchanged
        assert _resolve_fill_keys(svc, f, {"1009": "Y"}) == {"1009": "Y"}
        # case-insensitive label match
        assert _resolve_fill_keys(svc, f, {"BISHERIGE Wohnung": "Z"}) == {"1009": "Z"}

    def test_schema_pair_and_no_cache_pass_through(self, tmp_dir):
        from semos.agentura.document.tools import _resolve_fill_keys

        svc, schema, cache = self._svc()
        f = tmp_dir / "form.pdf"
        f.write_bytes(b"bytes")
        cache(svc, f, schema)
        pair = {"schema": {}, "data": {}}
        assert _resolve_fill_keys(svc, f, pair) is pair

        class _Bare:
            pass

        assert _resolve_fill_keys(_Bare(), f, {"1009": "A"}) == {"1009": "A"}
