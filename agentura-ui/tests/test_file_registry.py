"""Tests for UI-specific file reference resolution.

Protocol-level middleware tests (FileRegistry, pre/post_process, etc.)
now live in agentura-commons/tests/test_file_middleware.py.
This file only tests the renderers.resolve_file_references function
which produces HTML <img> tags for Panel chat display.
"""

from __future__ import annotations

import base64


# resolve_file_references (UI-specific: produces <img> tags)


class TestResolveFileReferences:
    """Test inline markdown file reference resolution."""

    def test_image_resolved_to_data_uri(self, registry):
        from agentura_ui.renderers import resolve_file_references

        registry.register(
            "diagram.png", b"\x89PNG-FAKE",
            "image/png", "tool:generate_diagram",
        )
        md = "## Diagram\n\n![A to B](diagram.png)"
        resolved = resolve_file_references(md, registry)

        assert '<img src="data:image/png;base64,' in resolved
        assert 'max-width:100%' in resolved
        # Verify round-trip: extract base64 from <img src="data:...">
        src = resolved.split('src="')[1].split('"')[0]
        _, b64 = src.split(",", 1)
        assert base64.b64decode(b64) == b"\x89PNG-FAKE"

    def test_link_resolved(self, registry):
        from agentura_ui.renderers import resolve_file_references

        registry.register(
            "report.pdf", b"%PDF-CONTENT",
            "application/pdf", "tool:compose",
        )
        md = "Download [the report](report.pdf) here."
        resolved = resolve_file_references(md, registry)
        assert "(data:application/pdf;base64," in resolved

    def test_url_not_replaced(self, registry):
        from agentura_ui.renderers import resolve_file_references

        md = "![img](https://example.com/pic.png)"
        assert resolve_file_references(md, registry) == md

    def test_data_uri_not_replaced(self, registry):
        from agentura_ui.renderers import resolve_file_references

        md = "![img](data:image/png;base64,abc)"
        assert resolve_file_references(md, registry) == md

    def test_unknown_file_not_replaced(self, registry):
        from agentura_ui.renderers import resolve_file_references

        md = "![img](nonexistent.png)"
        assert resolve_file_references(md, registry) == md

    def test_multiple_refs(self, registry):
        from agentura_ui.renderers import resolve_file_references

        registry.register(
            "a.png", b"A", "image/png", "tool:t",
        )
        registry.register(
            "b.png", b"B", "image/png", "tool:t",
        )
        md = "![](a.png) and ![](b.png)"
        resolved = resolve_file_references(md, registry)
        assert resolved.count("data:image/png;base64,") == 2

    def test_real_world_diagram_output(self, registry):
        """Matches the exact pattern from generate_diagram."""
        from agentura_ui.renderers import resolve_file_references

        registry.register(
            "5e922231_iter_01.png",
            b"\x89PNG diagram bytes",
            "image/png",
            "tool:generate_diagram",
        )
        md = (
            "## Diagram\n\n"
            "### Output File\n"
            "- **Filename:** `5e922231_iter_01.png`\n\n"
            "![A to B to C](5e922231_iter_01.png)"
        )
        resolved = resolve_file_references(md, registry)
        assert "data:image/png;base64," in resolved
        # The backtick filename reference should NOT be replaced
        assert "`5e922231_iter_01.png`" in resolved
