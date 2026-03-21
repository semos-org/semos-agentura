"""Tests for composition/_diagram_source.py - diagram type detection."""

from pathlib import Path

from document_agent.composition._diagram_source import (
    _detect_type_from_path,
    _detect_type_from_string,
)


class TestDetectTypeFromString:
    def test_mermaid_graph(self):
        assert _detect_type_from_string("graph TD\n  A-->B") == "mermaid"

    def test_mermaid_flowchart(self):
        assert _detect_type_from_string("flowchart LR\n  Start --> End") == "mermaid"

    def test_mermaid_sequence(self):
        assert _detect_type_from_string("sequenceDiagram\n  A->>B: msg") == "mermaid"

    def test_mermaid_pie(self):
        assert _detect_type_from_string("pie\n  \"A\": 50") == "mermaid"

    def test_drawio_mxfile(self):
        assert _detect_type_from_string("<mxfile><diagram/></mxfile>") == "drawio"

    def test_drawio_mxgraphmodel(self):
        assert _detect_type_from_string("<mxGraphModel><root/></mxGraphModel>") == "drawio"

    def test_unknown(self):
        assert _detect_type_from_string("just some text") == "unknown"

    def test_leading_whitespace(self):
        assert _detect_type_from_string("  graph TD\n  A-->B") == "mermaid"
        assert _detect_type_from_string("  <mxfile/>") == "drawio"


class TestDetectTypeFromPath:
    def test_mmd(self):
        assert _detect_type_from_path(Path("diagram.mmd")) == "mermaid"

    def test_mermaid_ext(self):
        assert _detect_type_from_path(Path("flow.mermaid")) == "mermaid"

    def test_drawio(self):
        assert _detect_type_from_path(Path("arch.drawio")) == "drawio"

    def test_drawio_xml(self):
        assert _detect_type_from_path(Path("arch.drawio.xml")) == "drawio"

    def test_drawio_png(self):
        assert _detect_type_from_path(Path("arch.drawio.png")) == "drawio"

    def test_svg_unknown(self):
        assert _detect_type_from_path(Path("diagram.svg")) == "unknown"

    def test_png_unknown(self):
        assert _detect_type_from_path(Path("photo.png")) == "unknown"

    def test_jpg_unknown(self):
        assert _detect_type_from_path(Path("sketch.jpg")) == "unknown"
