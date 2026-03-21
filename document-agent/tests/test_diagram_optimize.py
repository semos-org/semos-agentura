"""Tests for composition/_diagram_optimize.py - code extraction and review parsing."""

import json

from document_agent.composition._diagram_optimize import (
    _build_initial_messages,
    _extract_code,
    _parse_review,
)
from document_agent.composition._diagram_source import DiagramSource


class TestExtractCode:
    def test_plain_code(self):
        code = "graph TD\n  A-->B"
        assert _extract_code(code, "mermaid") == code

    def test_mermaid_fenced(self):
        text = "Here's the diagram:\n```mermaid\ngraph TD\n  A-->B\n```\nDone."
        assert _extract_code(text, "mermaid") == "graph TD\n  A-->B"

    def test_xml_fenced(self):
        text = "```xml\n<mxfile>content</mxfile>\n```"
        assert _extract_code(text, "drawio") == "<mxfile>content</mxfile>"

    def test_generic_fenced(self):
        text = "```\ngraph TD\n  A-->B\n```"
        assert _extract_code(text, "mermaid") == "graph TD\n  A-->B"

    def test_drawio_fenced(self):
        text = "```drawio\n<mxfile/>\n```"
        assert _extract_code(text, "drawio") == "<mxfile/>"

    def test_whitespace_stripped(self):
        text = "  \n graph TD\n  A-->B  \n "
        assert _extract_code(text, "mermaid") == "graph TD\n  A-->B"


class TestParseReview:
    def test_valid_json(self):
        text = '{"pass": true, "issues": [], "suggestions": ""}'
        result = _parse_review(text)
        assert result["pass"] is True
        assert result["issues"] == []

    def test_json_in_fences(self):
        text = '```json\n{"pass": false, "issues": ["bad layout"]}\n```'
        result = _parse_review(text)
        assert result["pass"] is False
        assert "bad layout" in result["issues"]

    def test_malformed_json_fallback(self):
        text = "This is not JSON at all"
        result = _parse_review(text)
        assert result["pass"] is False
        assert "Unparseable review" in result["issues"]

    def test_partial_json(self):
        text = '{"pass": false, "issues": ["x"], "suggestions": "fix it"}'
        result = _parse_review(text)
        assert result["suggestions"] == "fix it"


class TestBuildInitialMessages:
    def test_description_only(self):
        msgs = _build_initial_messages("sys", "Draw a flowchart", None, "mermaid")
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "sys"
        assert msgs[1]["role"] == "user"
        assert "flowchart" in msgs[1]["content"]

    def test_source_code_with_description(self):
        source = DiagramSource(code="graph TD\n  A-->B", diagram_type="mermaid")
        msgs = _build_initial_messages("sys", "Add colors", source, "mermaid")
        assert msgs[1]["role"] == "assistant"
        assert "graph TD" in msgs[1]["content"]
        assert msgs[2]["role"] == "user"
        assert "Add colors" in msgs[2]["content"]

    def test_source_code_without_description(self):
        source = DiagramSource(code="graph TD\n  A-->B", diagram_type="mermaid")
        msgs = _build_initial_messages("sys", None, source, "mermaid")
        assert msgs[2]["role"] == "user"
        assert "Improve" in msgs[2]["content"]

    def test_source_description_only(self):
        source = DiagramSource(description="A hand-drawn flowchart with 3 boxes", diagram_type="unknown")
        msgs = _build_initial_messages("sys", "Make it blue", source, "mermaid")
        assert "hand-drawn" in msgs[1]["content"]
        assert "Make it blue" in msgs[1]["content"]

    def test_no_source_no_description_raises(self):
        import pytest
        with pytest.raises(ValueError, match="At least one"):
            _build_initial_messages("sys", None, None, "mermaid")
