"""Integration tests for Google Drive via sharing links.

Requires all three environment variables:
    GOOGLE_DRIVE_CLIENT_ID        - GCP OAuth client ID
    GOOGLE_DRIVE_CLIENT_SECRET    - GCP OAuth client secret
    TEST_GOOGLE_DRIVE_FOLDER_LINK - folder share link

Optional (for extra tests):
    TEST_GOOGLE_DRIVE_FILE_LINK   - file share link
    TEST_GOOGLE_DRIVE_DOC_LINK    - Google Doc share link

Run with: pytest -m integration tests/test_google_drive_integration.py -v -s
"""

from __future__ import annotations

import json
import os

import pytest
from semos.agentura.files.service import FilesystemAgentService
from semos.agentura.files.vfs import VirtualFileSystem

_CLIENT_ID = os.environ.get("GOOGLE_DRIVE_CLIENT_ID", "")
_CLIENT_SECRET = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", "")
_FOLDER_LINK = os.environ.get("TEST_GOOGLE_DRIVE_FOLDER_LINK", "")
_FILE_LINK = os.environ.get("TEST_GOOGLE_DRIVE_FILE_LINK", "")
_DOC_LINK = os.environ.get("TEST_GOOGLE_DRIVE_DOC_LINK", "")

_HAS_CREDS = bool(_CLIENT_ID and _CLIENT_SECRET and _FOLDER_LINK)
_SKIP_REASON = "Set GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET, and TEST_GOOGLE_DRIVE_FOLDER_LINK"


async def _mount(name: str, share_url: str) -> tuple[FilesystemAgentService, dict]:
    svc = FilesystemAgentService(vfs=VirtualFileSystem())
    result = json.loads(
        await svc._add_root(
            name=name,
            protocol="google_drive",
            kwargs={"share_url": share_url},
        )
    )
    return svc, result


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(not _HAS_CREDS, reason=_SKIP_REASON)
async def test_gdrive_folder_list():
    """Mount a Google Drive folder and list files."""
    svc, result = await _mount("gdrive", _FOLDER_LINK)
    assert "mounted" in result, f"Mount failed: {result}"
    print(f"\nMounted: {result}")

    entries = json.loads(await svc._list_files("gdrive://"))
    assert isinstance(entries, list)
    print(f"Folder has {len(entries)} entries:")
    for e in entries[:10]:
        print(f"  {e.get('type', '?'):9s} {e.get('name', '?')}")


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(not _HAS_CREDS, reason=_SKIP_REASON)
async def test_gdrive_write_read_delete():
    """Write a file, read it back, then delete it."""
    svc, result = await _mount("gdrive", _FOLDER_LINK)
    assert "mounted" in result

    test_uri = "gdrive://_test_integration_scratch.txt"
    test_content = "Hello from integration test"
    await svc._write_file(test_uri, test_content)

    read_back = await svc._read_file(test_uri)
    assert test_content in read_back

    await svc._delete_file(test_uri)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    not (_HAS_CREDS and _FILE_LINK),
    reason=_SKIP_REASON + " and TEST_GOOGLE_DRIVE_FILE_LINK",
)
async def test_gdrive_file_link():
    """Mount a single-file sharing link."""
    svc, result = await _mount("gdfile", _FILE_LINK)
    assert "mounted" in result, f"Mount failed: {result}"
    assert result.get("mode") == "single-file"
    print(f"\nMounted file: {result}")

    entries = json.loads(await svc._list_files("gdfile://"))
    assert len(entries) == 1
    print(f"File: {entries[0].get('name')}")


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    not (_HAS_CREDS and _DOC_LINK),
    reason=_SKIP_REASON + " and TEST_GOOGLE_DRIVE_DOC_LINK",
)
async def test_gdrive_doc_export():
    """Mount a Google Doc and read it (exported as DOCX)."""
    svc, result = await _mount("gddoc", _DOC_LINK)
    assert "mounted" in result, f"Mount failed: {result}"
    assert result.get("mode") == "export"
    print(f"\nMounted doc: {result}")

    filename = result.get("file", "")
    assert filename.endswith(".docx"), f"Expected .docx export, got: {filename}"
