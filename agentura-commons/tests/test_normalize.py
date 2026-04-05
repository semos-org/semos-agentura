"""Tests for tool result normalization in mcp_server.py."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agentura_commons.base import NamedFile, ToolResult
from agentura_commons.mcp_server import (
    _is_file_like,
    _materialize_file,
    _normalize_to_tool_result,
    _tool_result_to_call_tool_result,
)


# _is_file_like

class TestIsFileLike:
    def test_bytesio(self):
        assert _is_file_like(io.BytesIO(b"data"))

    def test_stringio(self):
        assert _is_file_like(io.StringIO("text"))

    def test_real_file(self, tmp_path):
        p = tmp_path / "test.txt"
        p.write_text("hello")
        with open(p) as f:
            assert _is_file_like(f)

    def test_string_not_file_like(self):
        assert not _is_file_like("hello")

    def test_path_not_file_like(self, tmp_path):
        assert not _is_file_like(tmp_path / "test.txt")

    def test_none_not_file_like(self):
        assert not _is_file_like(None)

    def test_dict_not_file_like(self):
        assert not _is_file_like({"read": True})


# _materialize_file

class TestMaterializeFile:
    def test_bytesio(self, tmp_path):
        buf = io.BytesIO(b"binary data")
        result = _materialize_file(buf, tmp_path)
        assert result.exists()
        assert result.read_bytes() == b"binary data"
        assert result.parent == tmp_path

    def test_stringio(self, tmp_path):
        buf = io.StringIO("text content")
        result = _materialize_file(buf, tmp_path)
        assert result.exists()
        assert result.read_bytes() == b"text content"

    def test_named_file_object(self, tmp_path):
        buf = io.BytesIO(b"data")
        buf.name = "report.pdf"
        result = _materialize_file(buf, tmp_path)
        assert result.exists()
        assert "report.pdf" in result.name

    def test_unnamed_gets_uuid(self, tmp_path):
        buf = io.BytesIO(b"data")
        result = _materialize_file(buf, tmp_path)
        assert "_file_" in result.name
        assert result.suffix == ".bin"


# _normalize_to_tool_result

class TestNormalizeToToolResult:
    # str
    def test_plain_string(self):
        r = _normalize_to_tool_result("hello")
        assert r.text == "hello"
        assert r.data is None
        assert r.files == []

    def test_json_string_dict(self):
        r = _normalize_to_tool_result('{"key": "value"}')
        assert r.data == {"key": "value"}

    def test_json_string_list(self):
        r = _normalize_to_tool_result('[1, 2, 3]')
        assert r.data == [1, 2, 3]

    def test_json_string_download_url(self):
        s = json.dumps({"download_url": "http://x", "filename": "f.pdf"})
        r = _normalize_to_tool_result(s)
        assert r.data["download_url"] == "http://x"

    # dict / list
    def test_dict(self):
        r = _normalize_to_tool_result({"a": 1})
        assert r.data == {"a": 1}

    def test_data_list(self):
        r = _normalize_to_tool_result([{"a": 1}, {"b": 2}])
        assert r.data == [{"a": 1}, {"b": 2}]

    # None
    def test_none(self):
        r = _normalize_to_tool_result(None)
        assert r.text == ""
        assert r.data is None
        assert r.files == []

    # Path
    def test_single_path(self, tmp_path):
        p = tmp_path / "out.docx"
        p.write_bytes(b"doc")
        r = _normalize_to_tool_result(p)
        assert len(r.files) == 1
        assert r.files[0] == p

    # NamedFile
    def test_named_file(self, tmp_path):
        p = tmp_path / "uuid_report.docx"
        p.write_bytes(b"doc")
        nf = NamedFile(path=p, name="report.docx")
        r = _normalize_to_tool_result(nf)
        assert len(r.files) == 1
        assert isinstance(r.files[0], NamedFile)
        assert r.files[0].name == "report.docx"

    # File-like object
    def test_single_file_like(self, tmp_path):
        buf = io.BytesIO(b"content")
        buf.name = "data.csv"
        r = _normalize_to_tool_result(buf, output_dir=tmp_path)
        assert len(r.files) == 1
        assert r.files[0].exists()
        assert "data.csv" in r.files[0].name

    # List of Paths
    def test_path_list(self, tmp_path):
        p1 = tmp_path / "a.pdf"
        p2 = tmp_path / "b.pdf"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")
        r = _normalize_to_tool_result([p1, p2])
        assert len(r.files) == 2
        assert r.files[0] == p1
        assert r.files[1] == p2

    # List of NamedFiles
    def test_named_file_list(self, tmp_path):
        p1 = tmp_path / "x.pdf"
        p1.write_bytes(b"x")
        nf1 = NamedFile(path=p1, name="doc1.pdf")
        nf2 = NamedFile(path=p1, name="doc2.pdf")
        r = _normalize_to_tool_result([nf1, nf2])
        assert len(r.files) == 2

    # List of file-like objects
    def test_file_like_list(self, tmp_path):
        b1 = io.BytesIO(b"one")
        b1.name = "one.txt"
        b2 = io.BytesIO(b"two")
        b2.name = "two.txt"
        r = _normalize_to_tool_result([b1, b2], output_dir=tmp_path)
        assert len(r.files) == 2
        assert all(f.exists() for f in r.files)

    # Mixed list (Path + NamedFile + file-like)
    def test_mixed_file_list(self, tmp_path):
        p = tmp_path / "file.pdf"
        p.write_bytes(b"pdf")
        nf = NamedFile(path=p, name="named.pdf")
        buf = io.BytesIO(b"buf")
        buf.name = "buf.txt"
        r = _normalize_to_tool_result([p, nf, buf], output_dir=tmp_path)
        assert len(r.files) == 3

    # List of non-files stays data
    def test_mixed_list_not_files(self):
        r = _normalize_to_tool_result(["a", "b", "c"])
        assert r.data == ["a", "b", "c"]
        assert r.files == []

    # ToolResult passthrough
    def test_tool_result_passthrough(self):
        tr = ToolResult(text="hi", data={"x": 1})
        r = _normalize_to_tool_result(tr)
        assert r is tr

    # Fallback
    def test_int_stringified(self):
        r = _normalize_to_tool_result(42)
        assert r.text == "42"


# _tool_result_to_call_tool_result

class TestToolResultToCallToolResult:
    def test_text_only(self):
        tr = ToolResult(text="hello")
        ctr = _tool_result_to_call_tool_result(tr, "http://localhost")
        assert len(ctr.content) == 1
        assert ctr.content[0].text == "hello"
        assert ctr.structuredContent is None

    def test_data_dict(self):
        tr = ToolResult(data={"key": "val"})
        ctr = _tool_result_to_call_tool_result(tr, "http://localhost")
        assert ctr.structuredContent == {"key": "val"}
        # Text fallback for LLMs
        assert any("key" in c.text for c in ctr.content if hasattr(c, "text"))

    def test_data_list_wrapped(self):
        tr = ToolResult(data=[1, 2, 3])
        ctr = _tool_result_to_call_tool_result(tr, "http://localhost")
        assert ctr.structuredContent == {"items": [1, 2, 3]}

    def test_file_produces_resource_link(self, tmp_path):
        p = tmp_path / "report.docx"
        p.write_bytes(b"content")
        tr = ToolResult(files=[p])
        ctr = _tool_result_to_call_tool_result(tr, "http://localhost")
        links = [c for c in ctr.content if c.type == "resource_link"]
        assert len(links) == 1
        assert links[0].name == "report.docx"
        assert "report.docx" in str(links[0].uri)
        assert ctr.structuredContent is not None
        assert "download_url" in ctr.structuredContent

    def test_named_file_uses_display_name(self, tmp_path):
        p = tmp_path / "abc123_report.docx"
        p.write_bytes(b"content")
        nf = NamedFile(path=p, name="My Report.docx")
        tr = ToolResult(files=[nf])
        ctr = _tool_result_to_call_tool_result(tr, "http://localhost")
        links = [c for c in ctr.content if c.type == "resource_link"]
        assert links[0].name == "My Report.docx"

    def test_multiple_files(self, tmp_path):
        p1 = tmp_path / "a.pdf"
        p2 = tmp_path / "b.pdf"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")
        tr = ToolResult(files=[p1, p2])
        ctr = _tool_result_to_call_tool_result(tr, "http://localhost")
        links = [c for c in ctr.content if c.type == "resource_link"]
        assert len(links) == 2

    def test_empty_result(self):
        tr = ToolResult()
        ctr = _tool_result_to_call_tool_result(tr, "http://localhost")
        assert len(ctr.content) == 1
        assert ctr.content[0].text == ""

    def test_text_plus_files(self, tmp_path):
        p = tmp_path / "out.xlsx"
        p.write_bytes(b"excel")
        tr = ToolResult(text="Generated spreadsheet", files=[p])
        ctr = _tool_result_to_call_tool_result(tr, "http://localhost")
        texts = [c for c in ctr.content if hasattr(c, "text")]
        links = [c for c in ctr.content if c.type == "resource_link"]
        assert len(texts) >= 1
        assert len(links) == 1
        assert any("Generated spreadsheet" in c.text for c in texts)
