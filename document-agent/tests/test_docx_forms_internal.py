"""Tests for forms/_docx_forms.py - internal helpers."""

from datetime import datetime

from document_agent.forms._docx_forms import (
    _format_date_word,
    _make_unique,
    _normalize_label,
)


class TestNormalizeLabel:
    def test_strips_colon(self):
        assert _normalize_label("Name:") == "Name"

    def test_strips_parenthetical(self):
        assert _normalize_label("Abreiseort (Wohnort/Dienstort):") == "Abreiseort"

    def test_strips_multiple_parentheticals(self):
        assert _normalize_label("Field (hint1) (hint2):") == "Field"

    def test_takes_first_line(self):
        assert _normalize_label("Line 1\nLine 2") == "Line 1"

    def test_collapses_whitespace(self):
        assert _normalize_label("Some   long   label:") == "Some long label"

    def test_empty_string(self):
        assert _normalize_label("") == ""


class TestMakeUnique:
    def test_first_occurrence(self):
        seen = {}
        assert _make_unique("Name", seen) == "Name"

    def test_second_occurrence(self):
        seen = {}
        _make_unique("Name", seen)
        assert _make_unique("Name", seen) == "Name #2"

    def test_third_occurrence(self):
        seen = {}
        _make_unique("Name", seen)
        _make_unique("Name", seen)
        assert _make_unique("Name", seen) == "Name #3"

    def test_different_names_no_collision(self):
        seen = {}
        assert _make_unique("A", seen) == "A"
        assert _make_unique("B", seen) == "B"


class TestFormatDateWord:
    def test_dd_mm_yyyy(self):
        dt = datetime(2026, 3, 17)
        assert _format_date_word(dt, "dd.MM.yyyy") == "17.03.2026"

    def test_mm_dd_yyyy(self):
        dt = datetime(2026, 3, 17)
        assert _format_date_word(dt, "MM/dd/yyyy") == "03/17/2026"

    def test_short_year(self):
        dt = datetime(2026, 1, 5)
        assert _format_date_word(dt, "dd.MM.yy") == "05.01.26"

    def test_long_month(self):
        dt = datetime(2026, 12, 25)
        result = _format_date_word(dt, "MMMM dd, yyyy")
        assert "December" in result
        assert "25" in result
