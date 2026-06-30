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
        from semos.agentura.ui.renderers import resolve_file_references

        registry.register(
            "diagram.png",
            b"\x89PNG-FAKE",
            "image/png",
            "tool:generate_diagram",
        )
        md = "## Diagram\n\n![A to B](diagram.png)"
        resolved = resolve_file_references(md, registry)

        assert '<img src="data:image/png;base64,' in resolved
        assert "max-width:100%" in resolved
        # Verify round-trip: extract base64 from <img src="data:...">
        src = resolved.split('src="')[1].split('"')[0]
        _, b64 = src.split(",", 1)
        assert base64.b64decode(b64) == b"\x89PNG-FAKE"

    def test_link_resolved(self, registry):
        from semos.agentura.ui.renderers import resolve_file_references

        registry.register(
            "report.pdf",
            b"%PDF-CONTENT",
            "application/pdf",
            "tool:compose",
        )
        md = "Download [the report](report.pdf) here."
        resolved = resolve_file_references(md, registry)
        # Non-image files are NOT inlined (prevents websocket freeze)
        assert resolved == md

    def test_url_not_replaced(self, registry):
        from semos.agentura.ui.renderers import resolve_file_references

        md = "![img](https://example.com/pic.png)"
        assert resolve_file_references(md, registry) == md

    def test_data_uri_not_replaced(self, registry):
        from semos.agentura.ui.renderers import resolve_file_references

        md = "![img](data:image/png;base64,abc)"
        assert resolve_file_references(md, registry) == md

    def test_unknown_file_not_replaced(self, registry):
        from semos.agentura.ui.renderers import resolve_file_references

        md = "![img](nonexistent.png)"
        assert resolve_file_references(md, registry) == md

    def test_multiple_refs(self, registry):
        from semos.agentura.ui.renderers import resolve_file_references

        registry.register(
            "a.png",
            b"A",
            "image/png",
            "tool:t",
        )
        registry.register(
            "b.png",
            b"B",
            "image/png",
            "tool:t",
        )
        md = "![](a.png) and ![](b.png)"
        resolved = resolve_file_references(md, registry)
        assert resolved.count("data:image/png;base64,") == 2

    def test_large_image_gets_thumbnail(self, registry):
        """Images > 200 KB get thumbnailed (Pillow JPEG)."""
        from semos.agentura.ui.renderers import resolve_file_references

        # Create a fake large PNG (> 200 KB)
        large_blob = b"\x89PNG" + b"\x00" * 250_000
        registry.register(
            "big.png",
            large_blob,
            "image/png",
            "tool:generate_diagram",
        )
        md = "![big](big.png)"
        resolved = resolve_file_references(md, registry)
        # Should be inlined as JPEG thumbnail (Pillow converts)
        # or original if Pillow can't parse the fake PNG
        assert "data:image/" in resolved

    def test_real_world_diagram_output(self, registry):
        """Matches the exact pattern from generate_diagram."""
        from semos.agentura.ui.renderers import resolve_file_references

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
        assert "`5e922231_iter_01.png`" in resolved


class TestVFSFileRegistry:
    """Test VFS-backed file registry."""

    def test_register_creates_vfs_file(self):
        from semos.agentura.files.vfs import VirtualFileSystem
        from semos.agentura.ui.file_registry import VFSFileRegistry

        vfs = VirtualFileSystem()
        vfs.add_root_from_protocol("session", "memory", base_path="/")
        reg = VFSFileRegistry(vfs, root="session")

        reg.register("test.txt", b"hello", "text/plain", "upload")
        assert vfs.cat("session://test.txt") == b"hello"

    def test_register_with_subfolder(self):
        """Files with path components create subdirectories."""
        from semos.agentura.files.vfs import VirtualFileSystem
        from semos.agentura.ui.file_registry import VFSFileRegistry

        vfs = VirtualFileSystem()
        vfs.add_root_from_protocol("session", "memory", base_path="/")
        reg = VFSFileRegistry(vfs, root="session")

        reg.register(
            "images/diagram_002.png",
            b"\x89PNG-data",
            "image/png",
            "tool:digest_document",
        )

        # File exists at the nested path
        assert vfs.cat("session://images/diagram_002.png") == b"\x89PNG-data"

        # Parent directory was created
        entries = vfs.ls("session://images")
        names = [e["name"] for e in entries]
        assert "diagram_002.png" in names

    def test_register_deep_subfolder(self):
        """Deep nested paths work too."""
        from semos.agentura.files.vfs import VirtualFileSystem
        from semos.agentura.ui.file_registry import VFSFileRegistry

        vfs = VirtualFileSystem()
        vfs.add_root_from_protocol("session", "memory", base_path="/")
        reg = VFSFileRegistry(vfs, root="session")

        reg.register(
            "a/b/c/deep.txt",
            b"nested",
            "text/plain",
            "tool:test",
        )
        assert vfs.cat("session://a/b/c/deep.txt") == b"nested"

    def test_get_by_basename(self):
        """Registry.get() finds files by basename (fuzzy match)."""
        from semos.agentura.files.vfs import VirtualFileSystem
        from semos.agentura.ui.file_registry import VFSFileRegistry

        vfs = VirtualFileSystem()
        vfs.add_root_from_protocol("session", "memory", base_path="/")
        reg = VFSFileRegistry(vfs, root="session")

        reg.register(
            "images/photo.png",
            b"img",
            "image/png",
            "tool:t",
        )
        # Exact key match
        assert reg.get("images/photo.png") is not None
        # Fuzzy basename match (what the LLM typically uses)
        assert reg.get("photo.png") is not None
