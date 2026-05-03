"""Integration tests for SharePoint via WebDAV.

Requires environment variables:
    TEST_SHAREPOINT_SITE_URL   - e.g. https://org.sharepoint.com/sites/MySite
    TEST_SHAREPOINT_DOC_LIBRARY - e.g. Freigegebene Dokumente
    TEST_SHAREPOINT_SUBFOLDER  - e.g. General

These tests need a live SharePoint session (smartcard/SSO).
They are skipped unless all three env vars are set.
Run with: pytest -m integration tests/test_sharepoint_integration.py -v -s
"""

from __future__ import annotations

import json
import os

import pytest
from filesystem_agent.service import FilesystemAgentService
from filesystem_agent.vfs import VirtualFileSystem

_SITE_URL = os.environ.get("TEST_SHAREPOINT_SITE_URL", "")
_DOC_LIB = os.environ.get("TEST_SHAREPOINT_DOC_LIBRARY", "")
_SUBFOLDER = os.environ.get("TEST_SHAREPOINT_SUBFOLDER", "")

_SKIP_REASON = (
    "Set TEST_SHAREPOINT_SITE_URL, TEST_SHAREPOINT_DOC_LIBRARY, "
    "TEST_SHAREPOINT_SUBFOLDER to run SharePoint integration tests"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_SITE_URL and _DOC_LIB and _SUBFOLDER),
        reason=_SKIP_REASON,
    ),
]


async def _build_sp_service() -> FilesystemAgentService:
    """Build a service by mounting SharePoint via the add_root tool (protocol=sharepoint)."""
    from pathlib import Path

    Path(".tokens").mkdir(exist_ok=True)

    svc = FilesystemAgentService(vfs=VirtualFileSystem())
    # Override settings so _search_sharepoint uses the test site
    svc._settings.sharepoint_site_url = _SITE_URL
    svc._settings.sharepoint_doc_library = _DOC_LIB
    svc._settings.sharepoint_subfolder = _SUBFOLDER

    result = json.loads(
        await svc._add_root(
            name="sp",
            protocol="sharepoint",
            kwargs={
                "site_url": _SITE_URL,
                "doc_library": _DOC_LIB,
                "subfolder": _SUBFOLDER,
            },
        )
    )
    assert "mounted" in result, f"add_root(sharepoint) failed: {result}"
    print(f"\nMounted SharePoint via add_root tool: {result}")
    return svc


@pytest.fixture(scope="module")
async def sp_service() -> FilesystemAgentService:
    return await _build_sp_service()


# list_files


@pytest.mark.asyncio
async def test_sp_list_files(sp_service: FilesystemAgentService):
    result = json.loads(await sp_service._list_files("sp://"))
    assert isinstance(result, list)
    assert len(result) > 0
    print(f"\nSharePoint root has {len(result)} entries:")
    for e in result[:10]:
        print(f"  {e.get('type') or '?':9s} {e.get('name') or '?'}")


# file_tree


@pytest.mark.asyncio
async def test_sp_file_tree(sp_service: FilesystemAgentService):
    result = json.loads(await sp_service._file_tree("sp://", depth=1))
    assert isinstance(result, list)
    assert len(result) > 0
    print(f"\nSharePoint tree (depth=1): {len(result)} top-level entries")


# glob


@pytest.mark.asyncio
async def test_sp_glob(sp_service: FilesystemAgentService):
    result = json.loads(await sp_service._glob("*", uri="sp://", depth=1))
    assert isinstance(result, list)
    print(f"\nSharePoint glob '*': {len(result)} matches")


# read_file (reads the first text file found)


@pytest.mark.asyncio
async def test_sp_read_file(sp_service: FilesystemAgentService):
    entries = json.loads(await sp_service._list_files("sp://"))
    text_files = [e for e in entries if e.get("type") == "file"]
    if not text_files:
        pytest.skip("No files found in SharePoint root")
    uri = text_files[0]["uri"]
    result = await sp_service._read_file(uri)
    assert isinstance(result, str)
    assert len(result) > 0
    print(f"\nRead {uri}: {len(result)} chars")


# write + edit + delete (round-trip on a scratch file)


@pytest.mark.asyncio
async def test_sp_write_edit_delete(sp_service: FilesystemAgentService):
    test_uri = "sp://_test_integration_scratch.txt"

    # write
    write_result = await sp_service._write_file(test_uri, "hello from integration test")
    assert "Written" in write_result
    print(f"\nWrote {test_uri}")

    # read back
    content = await sp_service._read_file(test_uri)
    assert "hello from integration test" in content

    # edit
    edit_result = json.loads(await sp_service._edit_file(test_uri, "hello", "goodbye"))
    assert edit_result.get("replacements") == 1
    content2 = await sp_service._read_file(test_uri)
    assert "goodbye from integration test" in content2
    print(f"Edited {test_uri}")

    # delete
    del_result = await sp_service._delete_file(test_uri)
    assert "Deleted" in del_result
    print(f"Deleted {test_uri}")


# grep


@pytest.mark.asyncio
async def test_sp_grep(sp_service: FilesystemAgentService):
    # Write a known text file so grep is guaranteed to find something
    test_uri = "sp://_test_grep_target.txt"
    await sp_service._write_file(test_uri, "grep_marker_42 this line should be found")
    try:
        # Invalidate fsspec directory cache so tree() picks up the new file
        vfs = sp_service._ensure_vfs()
        root = vfs.get_root("sp")
        root.fs.dircache.clear()
        result = json.loads(await sp_service._grep("grep_marker_42", uri="sp://", depth=1, max_results=10))
        assert len(result) > 0, "grep should find the marker in the test file"
        assert any("grep_marker_42" in m.get("line", "") for m in result)
        assert all("uri" in m and "line" in m and "line_number" in m for m in result)
        print(f"\nGrep found {len(result)} matches:")
        for m in result[:5]:
            print(f"  {m['uri']}:{m['line_number']}: {m['line'][:60]}")
    finally:
        await sp_service._delete_file(test_uri)


# search_sharepoint


@pytest.mark.asyncio
async def test_sp_search(sp_service: FilesystemAgentService):
    result = json.loads(await sp_service._search_sharepoint("test", limit=5))
    assert isinstance(result, list)
    print(f"\nSharePoint search 'test': {len(result)} results")
    for r in result[:3]:
        print(f"  {r.get('filename', '?')} - {r.get('title', '?')}")


# copy + move


@pytest.mark.asyncio
async def test_sp_copy_move(sp_service: FilesystemAgentService):
    src = "sp://_test_copy_src.txt"
    dst_copy = "sp://_test_copy_dst.txt"
    dst_move = "sp://_test_move_dst.txt"

    await sp_service._write_file(src, "copy me")
    try:
        # copy
        copy_result = await sp_service._copy_file(src, dst_copy)
        assert "Copied" in copy_result
        content = await sp_service._read_file(dst_copy)
        assert "copy me" in content
        print(f"\nCopied {src} to {dst_copy}")

        # move
        move_result = await sp_service._move_file(dst_copy, dst_move)
        assert "Moved" in move_result
        content2 = await sp_service._read_file(dst_move)
        assert "copy me" in content2
        print(f"Moved {dst_copy} to {dst_move}")
    finally:
        # cleanup
        for uri in [src, dst_copy, dst_move]:
            try:
                await sp_service._delete_file(uri)
            except Exception:
                pass
