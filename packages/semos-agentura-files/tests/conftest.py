from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from fsspec.implementations.local import LocalFileSystem
from semos.agentura.files.service import FilesystemAgentService
from semos.agentura.files.vfs import VirtualFileSystem

GOLDEN_ZIP = Path(__file__).parent.parent / "test" / "data" / "test_data_golden.zip"


def _extract_golden(dest: Path) -> None:
    """Extract the golden test data zip to *dest*."""
    with zipfile.ZipFile(GOLDEN_ZIP) as zf:
        zf.extractall(dest)


@pytest.fixture
def vfs(tmp_path: Path) -> VirtualFileSystem:
    """Read-only VFS backed by golden test data (extracted to tmp dir)."""
    _extract_golden(tmp_path)
    v = VirtualFileSystem()
    v.add_root("local", LocalFileSystem(), base_path=str(tmp_path / "local_fs"))
    v.add_root("webdav", LocalFileSystem(), base_path=str(tmp_path / "webdav_fs"))
    return v


@pytest.fixture
def tmp_vfs(tmp_path: Path) -> VirtualFileSystem:
    """Writable VFS on tmp dirs (extracts golden data so mutations are safe)."""
    _extract_golden(tmp_path / "golden")
    src_a = tmp_path / "a"
    src_b = tmp_path / "b"
    # Copy so each root gets its own tree
    import shutil

    shutil.copytree(tmp_path / "golden" / "local_fs", src_a)
    shutil.copytree(tmp_path / "golden" / "webdav_fs", src_b)
    v = VirtualFileSystem()
    v.add_root("alpha", LocalFileSystem(), base_path=str(src_a))
    v.add_root("beta", LocalFileSystem(), base_path=str(src_b))
    return v


@pytest.fixture
def service(vfs: VirtualFileSystem) -> FilesystemAgentService:
    """Read-only service backed by golden test data."""
    return FilesystemAgentService(vfs=vfs)


@pytest.fixture
def tmp_service(tmp_vfs: VirtualFileSystem) -> FilesystemAgentService:
    """Writable service on temp dirs."""
    return FilesystemAgentService(vfs=tmp_vfs)
