from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from filesystem_agent.vfs import VirtualFileSystem
from fsspec.implementations.local import LocalFileSystem

TEST_DATA = Path(__file__).parent.parent / "test" / "data"


@pytest.fixture
def vfs() -> VirtualFileSystem:
    """Read-only VFS with two local roots on the committed test data."""
    v = VirtualFileSystem()
    v.add_root("local", LocalFileSystem(), base_path=str(TEST_DATA / "local_fs"))
    v.add_root("webdav", LocalFileSystem(), base_path=str(TEST_DATA / "webdav_fs"))
    return v


@pytest.fixture
def tmp_vfs(tmp_path: Path) -> VirtualFileSystem:
    """Writable VFS on tmp dirs (copies test data so mutations are safe)."""
    src_a = tmp_path / "a"
    src_b = tmp_path / "b"
    shutil.copytree(TEST_DATA / "local_fs", src_a)
    shutil.copytree(TEST_DATA / "webdav_fs", src_b)
    v = VirtualFileSystem()
    v.add_root("alpha", LocalFileSystem(), base_path=str(src_a))
    v.add_root("beta", LocalFileSystem(), base_path=str(src_b))
    return v
