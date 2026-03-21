"""Tests for forms/_pdf_forms.py - field classification and helpers."""

from document_agent.forms._pdf_forms import (
    FF_PUSHBUTTON,
    FF_RADIO,
    FT_BUTTON,
    FT_CHOICE,
    FT_SIGNATURE,
    FT_TEXT,
    _classify_field,
    _format_value,
)


class TestClassifyField:
    def test_text(self):
        assert _classify_field(FT_TEXT, 0) == "text"

    def test_checkbox(self):
        assert _classify_field(FT_BUTTON, 0) == "checkbox"

    def test_radio(self):
        assert _classify_field(FT_BUTTON, FF_RADIO) == "radio"

    def test_pushbutton(self):
        assert _classify_field(FT_BUTTON, FF_PUSHBUTTON) == "button"

    def test_dropdown(self):
        assert _classify_field(FT_CHOICE, 1 << 17) == "dropdown"

    def test_listbox(self):
        assert _classify_field(FT_CHOICE, 0) == "listbox"

    def test_signature(self):
        assert _classify_field(FT_SIGNATURE, 0) == "signature"

    def test_unknown(self):
        assert _classify_field("", 0) == "unknown"
        assert _classify_field("/Custom", 0) == "/Custom"


class TestFormatValue:
    def test_none(self):
        assert _format_value(None) is None

    def test_string(self):
        assert _format_value("hello") == "hello"

    def test_number(self):
        assert _format_value(42) == "42"
