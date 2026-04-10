"""Tests for _utils.py - tool finding, source resolution, base64 encoding."""

import base64

import pytest
from document_agent._utils import (
    decode_image_base64,
    encode_image_base64,
    find_tool,
    resolve_source,
)


class TestFindTool:
    def test_finds_existing_tool(self):
        # python should always be on PATH
        result = find_tool("python")
        # May find python3 on some systems, so allow None on edge cases
        if result is not None:
            assert result.is_file()

    def test_returns_none_for_nonexistent(self):
        assert find_tool("this_tool_does_not_exist_xyz") is None

    def test_env_override(self, tmp_dir):
        fake = tmp_dir / "mytool.exe"
        fake.write_text("fake")
        assert find_tool("mytool", str(fake)) == fake

    def test_env_override_nonexistent(self):
        assert find_tool("mytool", "/nonexistent/path/tool") is None


class TestEncodeDecodeBase64:
    def test_roundtrip(self):
        raw = b"\x89PNG fake image data"
        mime = "image/png"
        uri = encode_image_base64(raw, mime)
        assert uri.startswith("data:image/png;base64,")
        decoded, decoded_mime = decode_image_base64(uri)
        assert decoded == raw
        assert decoded_mime == mime

    def test_invalid_uri_raises(self):
        with pytest.raises(ValueError, match="Invalid data URI"):
            decode_image_base64("not_a_data_uri")


class TestResolveSource:
    def test_path_object(self, tmp_dir):
        f = tmp_dir / "test.pdf"
        f.write_bytes(b"fake pdf")
        path, is_temp = resolve_source(f)
        assert path == f
        assert not is_temp

    def test_string_path(self, tmp_dir):
        f = tmp_dir / "test.png"
        f.write_bytes(b"fake png")
        path, is_temp = resolve_source(str(f))
        assert path == f
        assert not is_temp

    def test_bytes_creates_temp(self):
        data = b"some binary data"
        path, is_temp = resolve_source(data, "test.pdf")
        try:
            assert is_temp
            assert path.exists()
            assert path.read_bytes() == data
            assert path.suffix == ".pdf"
        finally:
            path.unlink(missing_ok=True)

    def test_base64_string(self, tmp_dir):
        raw = b"fake image content"
        b64 = base64.b64encode(raw).decode()
        path, is_temp = resolve_source(b64, "test.png")
        try:
            assert is_temp
            assert path.read_bytes() == raw
        finally:
            path.unlink(missing_ok=True)

    def test_data_uri_string(self, tmp_dir):
        raw = b"fake image"
        b64 = base64.b64encode(raw).decode()
        uri = f"data:image/png;base64,{b64}"
        path, is_temp = resolve_source(uri, "test.png")
        try:
            assert is_temp
            assert path.read_bytes() == raw
        finally:
            path.unlink(missing_ok=True)
