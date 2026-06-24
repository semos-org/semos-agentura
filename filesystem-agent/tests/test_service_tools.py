"""Tests for FilesystemAgentService tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from filesystem_agent.service import FilesystemAgentService
from filesystem_agent.vfs import VirtualFileSystem

# grep


@pytest.mark.asyncio
async def test_grep_finds_match(service: FilesystemAgentService):
    result = json.loads(await service._grep("Annual", uri="local://"))
    assert len(result) > 0
    assert any(m["uri"] == "local://documents/report_2025.txt" for m in result)
    assert any("Annual" in m["line"] for m in result)


@pytest.mark.asyncio
async def test_grep_returns_line_numbers(service: FilesystemAgentService):
    result = json.loads(await service._grep("Annual", uri="local://"))
    for m in result:
        assert "line_number" in m
        assert isinstance(m["line_number"], int)
        assert m["line_number"] >= 1


@pytest.mark.asyncio
async def test_grep_no_match(service: FilesystemAgentService):
    result = json.loads(await service._grep("ZZZZNONEXISTENT", uri="local://"))
    assert result == []


@pytest.mark.asyncio
async def test_grep_regex(service: FilesystemAgentService):
    result = json.loads(await service._grep(r"\d{4}", uri="local://"))
    assert len(result) > 0


@pytest.mark.asyncio
async def test_grep_invalid_regex(service: FilesystemAgentService):
    result = json.loads(await service._grep("[invalid", uri="local://"))
    assert "error" in result


@pytest.mark.asyncio
async def test_grep_respects_max_results(service: FilesystemAgentService):
    result = json.loads(await service._grep(".", uri="local://", max_results=2))
    assert len(result) <= 2


@pytest.mark.asyncio
async def test_grep_across_roots(service: FilesystemAgentService):
    result = json.loads(await service._grep("notes", uri=""))
    uris = {m["uri"] for m in result}
    has_local = any(u.startswith("local://") for u in uris)
    has_webdav = any(u.startswith("webdav://") for u in uris)
    assert has_local or has_webdav


@pytest.mark.asyncio
async def test_grep_csv_content(service: FilesystemAgentService):
    result = json.loads(await service._grep("Hardware", uri="local://documents"))
    assert len(result) > 0
    assert any(m["uri"] == "local://documents/budget.csv" for m in result)


# glob


@pytest.mark.asyncio
async def test_glob_finds_txt(service: FilesystemAgentService):
    result = json.loads(await service._glob("*.txt", uri="local://"))
    names = [e["name"] for e in result]
    assert "report_2025.txt" in names
    assert "meeting_notes.txt" in names


@pytest.mark.asyncio
async def test_glob_finds_csv(service: FilesystemAgentService):
    result = json.loads(await service._glob("*.csv", uri="local://"))
    names = [e["name"] for e in result]
    assert "budget.csv" in names


@pytest.mark.asyncio
async def test_glob_finds_md(service: FilesystemAgentService):
    result = json.loads(await service._glob("*.md", uri="local://"))
    names = [e["name"] for e in result]
    assert "README.md" in names


@pytest.mark.asyncio
async def test_glob_finds_zip(service: FilesystemAgentService):
    result = json.loads(await service._glob("*.zip", uri="local://"))
    names = [e["name"] for e in result]
    assert "sample.zip" in names


@pytest.mark.asyncio
async def test_glob_no_match(service: FilesystemAgentService):
    result = json.loads(await service._glob("*.nonexistent", uri="local://"))
    assert result == []


@pytest.mark.asyncio
async def test_glob_prefix_pattern(service: FilesystemAgentService):
    result = json.loads(await service._glob("report_*", uri="local://"))
    assert all("report_" in e["name"] for e in result)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_glob_across_roots(service: FilesystemAgentService):
    result = json.loads(await service._glob("*.txt", uri=""))
    uris = {e.get("uri", "") for e in result}
    has_local = any(u.startswith("local://") for u in uris)
    has_webdav = any(u.startswith("webdav://") for u in uris)
    assert has_local and has_webdav


@pytest.mark.asyncio
async def test_glob_respects_max_results(service: FilesystemAgentService):
    result = json.loads(await service._glob("*", uri="local://", max_results=3))
    assert len(result) <= 3


@pytest.mark.asyncio
async def test_glob_returns_metadata(service: FilesystemAgentService):
    result = json.loads(await service._glob("*.csv", uri="local://"))
    assert len(result) > 0
    entry = result[0]
    assert "name" in entry
    assert "uri" in entry


# edit_file


@pytest.mark.asyncio
async def test_edit_file_replaces_text(tmp_service: FilesystemAgentService):
    result = json.loads(await tmp_service._edit_file("alpha://documents/report_2025.txt", "Annual", "Quarterly"))
    assert result["edited"] == "alpha://documents/report_2025.txt"
    assert result["replacements"] == 1
    vfs = tmp_service._ensure_vfs()
    content = vfs.cat("alpha://documents/report_2025.txt").decode("utf-8")
    assert "Quarterly" in content
    assert "Annual" not in content


@pytest.mark.asyncio
async def test_edit_file_not_found_error(tmp_service: FilesystemAgentService):
    result = json.loads(await tmp_service._edit_file("alpha://documents/report_2025.txt", "NONEXISTENT", "replacement"))
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_edit_file_ambiguous_error(tmp_service: FilesystemAgentService):
    vfs = tmp_service._ensure_vfs()
    vfs.put("alpha://dup.txt", b"foo bar foo baz foo")
    result = json.loads(await tmp_service._edit_file("alpha://dup.txt", "foo", "qux"))
    assert "error" in result
    assert "Ambiguous" in result["error"]
    assert "3" in result["error"]


@pytest.mark.asyncio
async def test_edit_file_replace_all(tmp_service: FilesystemAgentService):
    vfs = tmp_service._ensure_vfs()
    vfs.put("alpha://dup.txt", b"foo bar foo baz foo")
    result = json.loads(await tmp_service._edit_file("alpha://dup.txt", "foo", "qux", replace_all=True))
    assert result["replacements"] == 3
    content = vfs.cat("alpha://dup.txt").decode("utf-8")
    assert content == "qux bar qux baz qux"


@pytest.mark.asyncio
async def test_edit_file_preserves_other_content(tmp_service: FilesystemAgentService):
    vfs = tmp_service._ensure_vfs()
    vfs.put("alpha://multi.txt", b"line1\nline2\nline3")
    await tmp_service._edit_file("alpha://multi.txt", "line2", "CHANGED")
    content = vfs.cat("alpha://multi.txt").decode("utf-8")
    assert content == "line1\nCHANGED\nline3"


# batch_edit


@pytest.mark.asyncio
async def test_batch_edit_multiple_replacements(tmp_service: FilesystemAgentService):
    vfs = tmp_service._ensure_vfs()
    vfs.put("alpha://doc.txt", b"Hello World\nFoo Bar\nBaz Qux")
    result = json.loads(
        await tmp_service._batch_edit(
            "alpha://doc.txt",
            edits=[
                {"old": "Hello", "new": "Greetings"},
                {"old": "Foo", "new": "Changed"},
            ],
        )
    )
    assert result["edits_applied"] == 2
    content = vfs.cat("alpha://doc.txt").decode("utf-8")
    assert "Greetings World" in content
    assert "Changed Bar" in content
    assert "Baz Qux" in content


@pytest.mark.asyncio
async def test_batch_edit_sequential_application(tmp_service: FilesystemAgentService):
    """Edits are sequential: later edits see the result of prior ones."""
    vfs = tmp_service._ensure_vfs()
    vfs.put("alpha://seq.txt", b"AAA")
    result = json.loads(
        await tmp_service._batch_edit(
            "alpha://seq.txt",
            edits=[
                {"old": "AAA", "new": "BBB"},
                {"old": "BBB", "new": "CCC"},
            ],
        )
    )
    assert result["edits_applied"] == 2
    content = vfs.cat("alpha://seq.txt").decode("utf-8")
    assert content == "CCC"


@pytest.mark.asyncio
async def test_batch_edit_error_on_missing_old(tmp_service: FilesystemAgentService):
    vfs = tmp_service._ensure_vfs()
    vfs.put("alpha://doc.txt", b"Hello World")
    result = json.loads(
        await tmp_service._batch_edit(
            "alpha://doc.txt",
            edits=[
                {"old": "Hello", "new": "Hi"},
                {"old": "NONEXISTENT", "new": "X"},
            ],
        )
    )
    assert "error" in result
    assert "Edit 1" in result["error"]


@pytest.mark.asyncio
async def test_batch_edit_error_on_empty_old(tmp_service: FilesystemAgentService):
    vfs = tmp_service._ensure_vfs()
    vfs.put("alpha://doc.txt", b"Hello")
    result = json.loads(
        await tmp_service._batch_edit(
            "alpha://doc.txt",
            edits=[{"old": "", "new": "X"}],
        )
    )
    assert "error" in result
    assert "empty" in result["error"]


@pytest.mark.asyncio
async def test_batch_edit_no_edits_error(tmp_service: FilesystemAgentService):
    result = json.loads(await tmp_service._batch_edit("alpha://doc.txt"))
    assert "error" in result


@pytest.mark.asyncio
async def test_batch_edit_single_edit(tmp_service: FilesystemAgentService):
    vfs = tmp_service._ensure_vfs()
    vfs.put("alpha://single.txt", b"one two three")
    result = json.loads(
        await tmp_service._batch_edit(
            "alpha://single.txt",
            edits=[{"old": "two", "new": "TWO"}],
        )
    )
    assert result["edits_applied"] == 1
    content = vfs.cat("alpha://single.txt").decode("utf-8")
    assert content == "one TWO three"


# list_files


@pytest.mark.asyncio
async def test_list_files_root(service: FilesystemAgentService):
    result = json.loads(await service._list_files("local://"))
    names = [e["name"] for e in result]
    assert "documents" in names
    assert "README.md" in names


@pytest.mark.asyncio
async def test_list_files_empty_lists_roots(service: FilesystemAgentService):
    result = json.loads(await service._list_files(""))
    names = [e["name"] for e in result]
    assert "local" in names
    assert "webdav" in names


# read_file


@pytest.mark.asyncio
async def test_read_file_text(service: FilesystemAgentService):
    result = await service._read_file("local://documents/report_2025.txt")
    assert "Annual report 2025" in result


@pytest.mark.asyncio
async def test_read_file_no_uri(service: FilesystemAgentService):
    result = await service._read_file("")
    assert "Error" in result


# write_file


@pytest.mark.asyncio
async def test_write_file(tmp_service: FilesystemAgentService):
    result = await tmp_service._write_file("alpha://new.txt", "hello world")
    assert "Written" in result
    vfs = tmp_service._ensure_vfs()
    assert vfs.cat("alpha://new.txt") == b"hello world"


@pytest.mark.asyncio
async def test_write_file_append(tmp_service: FilesystemAgentService):
    """mode='append' accumulates into one file instead of overwriting."""
    vfs = tmp_service._ensure_vfs()
    await tmp_service._write_file("alpha://log.md", "# Part 1\n")
    await tmp_service._write_file("alpha://log.md", "## Part 2\n", mode="append")
    await tmp_service._write_file("alpha://log.md", "## Part 3\n", mode="append")
    assert vfs.cat("alpha://log.md").decode() == "# Part 1\n## Part 2\n## Part 3\n"


@pytest.mark.asyncio
async def test_write_file_append_creates_when_missing(tmp_service: FilesystemAgentService):
    """Appending to a non-existent file creates it."""
    vfs = tmp_service._ensure_vfs()
    await tmp_service._write_file("alpha://fresh.txt", "hi", mode="append")
    assert vfs.cat("alpha://fresh.txt") == b"hi"


@pytest.mark.asyncio
async def test_write_file_overwrite_default(tmp_service: FilesystemAgentService):
    """Default mode overwrites."""
    vfs = tmp_service._ensure_vfs()
    await tmp_service._write_file("alpha://o.txt", "first")
    await tmp_service._write_file("alpha://o.txt", "second")
    assert vfs.cat("alpha://o.txt") == b"second"


def test_write_file_mode_is_enum():
    """mode is exposed as a WriteMode enum (closed set) in the schema."""
    from filesystem_agent.tools import WriteFileInput, WriteMode
    from pydantic import ValidationError

    assert [m.value for m in WriteMode] == ["overwrite", "append"]
    with pytest.raises(ValidationError):
        WriteFileInput.model_validate({"uri": "x://a", "content": "c", "mode": "bogus"})


@pytest.mark.asyncio
async def test_write_file_empty_string_allowed(tmp_service: FilesystemAgentService):
    """Explicitly passing an empty string writes an empty file."""
    result = await tmp_service._write_file("alpha://empty.txt", "")
    assert "Written 0 chars" in result
    vfs = tmp_service._ensure_vfs()
    assert vfs.cat("alpha://empty.txt") == b""


@pytest.mark.asyncio
async def test_write_file_unicode(tmp_service: FilesystemAgentService):
    """Em-dash and other non-ASCII content round-trips via UTF-8."""
    content = "dash — geq ≥ end"
    await tmp_service._write_file("alpha://uni.txt", content)
    vfs = tmp_service._ensure_vfs()
    assert vfs.cat("alpha://uni.txt").decode("utf-8") == content


def test_write_file_required_in_schema():
    """Schema must mark uri and content required so the LLM provides them."""
    from filesystem_agent.tools import WriteFileTool

    schema = WriteFileTool(_service=None).get_input_schema()
    assert set(schema.get("required", [])) >= {"uri", "content"}


def test_write_file_pydantic_rejects_missing_content():
    """Pydantic validation rejects a call that omits content - this is what
    the MCP wrapper enforces, preventing silent 0-char writes."""
    import pytest as _pytest
    from filesystem_agent.tools import WriteFileInput
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        WriteFileInput.model_validate({"uri": "session://x.md"})

    # uri + content provided -> valid
    ok = WriteFileInput.model_validate({"uri": "session://x.md", "content": "hi"})
    assert ok.content == "hi"


# file_info


@pytest.mark.asyncio
async def test_file_info(service: FilesystemAgentService):
    result = json.loads(await service._file_info("local://documents/budget.csv"))
    assert result["type"] == "file"
    assert result["size"] > 0


# file_tree


@pytest.mark.asyncio
async def test_file_tree(service: FilesystemAgentService):
    result = json.loads(await service._file_tree("local://", depth=1))
    names = [e["name"] for e in result]
    assert "documents" in names


# move_file


@pytest.mark.asyncio
async def test_move_file(tmp_service: FilesystemAgentService):
    vfs = tmp_service._ensure_vfs()
    vfs.put("alpha://to_move.txt", b"moving")
    result = await tmp_service._move_file("alpha://to_move.txt", "alpha://moved.txt")
    assert "Moved" in result
    assert vfs.cat("alpha://moved.txt") == b"moving"


# copy_file


@pytest.mark.asyncio
async def test_copy_file(tmp_service: FilesystemAgentService):
    result = await tmp_service._copy_file("alpha://documents/budget.csv", "alpha://budget_copy.csv")
    assert "Copied" in result
    vfs = tmp_service._ensure_vfs()
    assert vfs.cat("alpha://budget_copy.csv") == vfs.cat("alpha://documents/budget.csv")


# delete_file


@pytest.mark.asyncio
async def test_delete_file(tmp_service: FilesystemAgentService):
    vfs = tmp_service._ensure_vfs()
    vfs.put("alpha://deleteme.txt", b"bye")
    result = await tmp_service._delete_file("alpha://deleteme.txt")
    assert "Deleted" in result
    with pytest.raises(FileNotFoundError):
        vfs.cat("alpha://deleteme.txt")


# create_folder


@pytest.mark.asyncio
async def test_create_folder(tmp_service: FilesystemAgentService):
    result = await tmp_service._create_folder("alpha://newfolder")
    assert "Created" in result
    vfs = tmp_service._ensure_vfs()
    assert vfs.isdir("alpha://newfolder")


# list_archive


@pytest.mark.asyncio
async def test_list_archive(service: FilesystemAgentService):
    result = json.loads(await service._list_archive("local://documents/sample.zip"))
    names = [e["name"] for e in result]
    assert "readme.txt" in names


# read_archive_file


@pytest.mark.asyncio
async def test_read_archive_file(service: FilesystemAgentService):
    result = await service._read_archive_file("local://documents/sample.zip!/readme.txt")
    assert "Hello from inside the archive" in result


# add_root / remove_root / list_roots


@pytest.mark.asyncio
async def test_add_remove_list_roots(tmp_path: Path):
    vfs = VirtualFileSystem()
    svc = FilesystemAgentService(vfs=vfs)

    (tmp_path / "test.txt").write_text("hi")
    result = json.loads(await svc._add_root(name="myroot", protocol="local", base_path=str(tmp_path)))
    assert result["mounted"] == "myroot"

    roots = json.loads(await svc._list_roots())
    assert any(r["name"] == "myroot" for r in roots)

    result = json.loads(await svc._remove_root("myroot"))
    assert result["unmounted"] == "myroot"

    roots = json.loads(await svc._list_roots())
    assert not any(r["name"] == "myroot" for r in roots)


@pytest.mark.asyncio
async def test_add_root_with_kwargs(tmp_path: Path):
    """Test that add_root accepts structured kwargs dict."""
    vfs = VirtualFileSystem()
    svc = FilesystemAgentService(vfs=vfs)
    result = json.loads(await svc._add_root(name="mem", protocol="memory", kwargs={"target_protocol": "memory"}))
    assert result["mounted"] == "mem"


# search_sharepoint (skipped - needs real SharePoint connection)


@pytest.mark.asyncio
async def test_search_sharepoint_no_config(service: FilesystemAgentService):
    """search_sharepoint returns error when SharePoint is not configured."""
    result = json.loads(await service._search_sharepoint("test query"))
    # Should fail gracefully since no SharePoint is configured
    assert isinstance(result, (list, dict))
