"""Unit tests for VirtualFileSystem."""

from __future__ import annotations

import pytest
from filesystem_agent.vfs import VirtualFileSystem

# -- root listing --


def test_list_roots(vfs: VirtualFileSystem):
    assert vfs.roots == ["local", "webdav"]


def test_ls_empty_returns_roots(vfs: VirtualFileSystem):
    entries = vfs.ls("", detail=True)
    names = [e["name"] for e in entries]
    assert "local" in names
    assert "webdav" in names
    assert all(e["type"] == "directory" for e in entries)


def test_ls_empty_no_detail(vfs: VirtualFileSystem):
    uris = vfs.ls("", detail=False)
    assert "local://" in uris
    assert "webdav://" in uris


# -- listing children --


def test_ls_root(vfs: VirtualFileSystem):
    entries = vfs.ls("local://", detail=True)
    names = [e["name"] for e in entries]
    assert "documents" in names
    assert "images" in names
    assert "README.md" in names


def test_ls_returns_uris(vfs: VirtualFileSystem):
    entries = vfs.ls("local://", detail=True)
    for e in entries:
        assert "uri" in e
        assert e["uri"].startswith("local://")


def test_ls_nested(vfs: VirtualFileSystem):
    entries = vfs.ls("webdav://archive", detail=True)
    names = [e["name"] for e in entries]
    assert "2024" in names
    assert "2023" in names


def test_ls_no_detail(vfs: VirtualFileSystem):
    uris = vfs.ls("local://documents", detail=False)
    assert all(isinstance(u, str) and u.startswith("local://") for u in uris)


# -- info --


def test_info_file(vfs: VirtualFileSystem):
    info = vfs.info("local://README.md")
    assert info["type"] == "file"
    assert info["uri"] == "local://README.md"
    assert info["size"] > 0


def test_info_directory(vfs: VirtualFileSystem):
    info = vfs.info("local://documents")
    assert info["type"] == "directory"


def test_info_root(vfs: VirtualFileSystem):
    info = vfs.info("local://")
    assert info["type"] == "directory"
    assert info["name"] == "local"


def test_isdir(vfs: VirtualFileSystem):
    assert vfs.isdir("local://documents") is True
    assert vfs.isdir("local://README.md") is False


# -- cat --


def test_cat(vfs: VirtualFileSystem):
    data = vfs.cat("local://documents/report_2025.txt")
    assert b"Annual report 2025" in data


# -- URI helpers --


def test_uri_roundtrip():
    root, rel = VirtualFileSystem.parse_uri("local://docs/f.txt")
    assert root == "local"
    assert rel == "docs/f.txt"
    assert VirtualFileSystem.make_uri(root, rel) == "local://docs/f.txt"


def test_uri_root_only():
    root, rel = VirtualFileSystem.parse_uri("webdav://")
    assert root == "webdav"
    assert rel == ""
    assert VirtualFileSystem.make_uri(root, rel) == "webdav://"


def test_invalid_uri():
    with pytest.raises(ValueError, match="missing"):
        VirtualFileSystem.parse_uri("no-scheme-here")


# -- tree (preload_depth) --


def test_tree_depth_0(vfs: VirtualFileSystem):
    nodes = vfs.tree("local://", depth=0)
    assert len(nodes) > 0
    for n in nodes:
        if n["type"] == "directory":
            assert n["children"] == []  # not yet loaded
        else:
            assert n["children"] is None


def test_tree_depth_1(vfs: VirtualFileSystem):
    nodes = vfs.tree("local://", depth=1)
    docs = next(n for n in nodes if n["name"] == "documents")
    assert isinstance(docs["children"], list)
    assert len(docs["children"]) > 0
    # children of documents should have children=[] (not loaded deeper)
    for child in docs["children"]:
        if child["type"] == "directory":
            assert child["children"] == []


def test_tree_depth_2(vfs: VirtualFileSystem):
    nodes = vfs.tree("webdav://", depth=2)
    archive = next(n for n in nodes if n["name"] == "archive")
    y2024 = next(n for n in archive["children"] if n["name"] == "2024")
    assert isinstance(y2024["children"], list)
    assert any(c["name"] == "old_report.txt" for c in y2024["children"])


# -- write operations --


def test_unknown_root(vfs: VirtualFileSystem):
    with pytest.raises(FileNotFoundError):
        vfs.ls("nonexistent://")


def test_mkdir_put_rm(tmp_vfs: VirtualFileSystem):
    tmp_vfs.mkdir("alpha://new_dir")
    assert tmp_vfs.isdir("alpha://new_dir")

    tmp_vfs.put("alpha://new_dir/hello.txt", b"hello world")
    assert tmp_vfs.cat("alpha://new_dir/hello.txt") == b"hello world"

    tmp_vfs.rm("alpha://new_dir/hello.txt")
    entries = tmp_vfs.ls("alpha://new_dir", detail=False)
    assert not any("hello.txt" in e for e in entries)

    tmp_vfs.rm("alpha://new_dir", recursive=True)


def test_mv(tmp_vfs: VirtualFileSystem):
    tmp_vfs.put("alpha://moveme.txt", b"moving")
    tmp_vfs.mv("alpha://moveme.txt", "alpha://documents/moved.txt")
    assert tmp_vfs.cat("alpha://documents/moved.txt") == b"moving"
    with pytest.raises(FileNotFoundError):
        tmp_vfs.cat("alpha://moveme.txt")


def test_cp_same_root(tmp_vfs: VirtualFileSystem):
    tmp_vfs.cp("alpha://documents/report_2025.txt", "alpha://documents/report_copy.txt")
    assert tmp_vfs.cat("alpha://documents/report_copy.txt") == tmp_vfs.cat("alpha://documents/report_2025.txt")


def test_cp_cross_root(tmp_vfs: VirtualFileSystem):
    original = tmp_vfs.cat("alpha://documents/report_2025.txt")
    tmp_vfs.cp("alpha://documents/report_2025.txt", "beta://shared/report_copy.txt")
    assert tmp_vfs.cat("beta://shared/report_copy.txt") == original


# -- archive support --


def test_is_archive(vfs: VirtualFileSystem):
    assert vfs.is_archive("local://documents/sample.zip")
    assert vfs.is_archive("webdav://shared/reports.zip")
    assert not vfs.is_archive("local://documents/report_2025.txt")
    assert not vfs.is_archive("local://documents")


def test_is_archive_with_inner_path(vfs: VirtualFileSystem):
    # A path inside an archive is NOT an archive itself
    assert not vfs.is_archive("local://documents/sample.zip!/data/file.csv")
    # But the archive file itself is
    assert vfs.is_archive("local://documents/sample.zip")


def test_split_archive_uri(vfs: VirtualFileSystem):
    archive, inner = vfs.split_archive_uri("local://documents/sample.zip!/data/config.json")
    assert archive == "local://documents/sample.zip"
    assert inner == "data/config.json"

    archive2, inner2 = vfs.split_archive_uri("local://documents/sample.zip")
    assert archive2 == "local://documents/sample.zip"
    assert inner2 == ""


def test_ls_archive_root(vfs: VirtualFileSystem):
    entries = vfs.ls_archive("local://documents/sample.zip")
    names = {e["name"] for e in entries}
    assert "readme.txt" in names
    assert "data" in names
    assert "images" in names


def test_ls_archive_subfolder(vfs: VirtualFileSystem):
    entries = vfs.ls_archive("local://documents/sample.zip!/data")
    names = {e["name"] for e in entries}
    assert "measurements.csv" in names
    assert "config.json" in names


def test_ls_archive_uris(vfs: VirtualFileSystem):
    entries = vfs.ls_archive("local://documents/sample.zip")
    for e in entries:
        assert "!" in e["uri"], f"Archive entry URI should contain '!': {e['uri']}"
        assert e["uri"].startswith("local://documents/sample.zip!")


def test_cat_archive(vfs: VirtualFileSystem):
    data = vfs.cat_archive("local://documents/sample.zip!/readme.txt")
    assert data == b"Hello from inside the archive!"


def test_cat_archive_nested(vfs: VirtualFileSystem):
    data = vfs.cat_archive("local://documents/sample.zip!/data/config.json")
    assert b'"version"' in data


def test_ls_archive_webdav(vfs: VirtualFileSystem):
    entries = vfs.ls_archive("webdav://shared/reports.zip")
    names = {e["name"] for e in entries}
    assert "Q1_report.txt" in names
    assert "figures" in names


def test_archive_size(vfs: VirtualFileSystem):
    size = vfs.archive_size("local://documents/sample.zip")
    assert size > 0


def test_archive_size_with_inner_path(vfs: VirtualFileSystem):
    size = vfs.archive_size("local://documents/sample.zip!/readme.txt")
    assert size > 0  # should return the archive file size, not the inner file


def test_put_archive(tmp_vfs: VirtualFileSystem):
    # Write a new file into the zip
    tmp_vfs.put_archive("alpha://documents/sample.zip!/new_file.txt", b"new content")
    # Read it back
    data = tmp_vfs.cat_archive("alpha://documents/sample.zip!/new_file.txt")
    assert data == b"new content"
    # Existing files should still be there
    readme = tmp_vfs.cat_archive("alpha://documents/sample.zip!/readme.txt")
    assert readme == b"Hello from inside the archive!"


def test_put_archive_overwrite(tmp_vfs: VirtualFileSystem):
    original = tmp_vfs.cat_archive("alpha://documents/sample.zip!/readme.txt")
    assert original == b"Hello from inside the archive!"
    # Overwrite existing file
    tmp_vfs.put_archive("alpha://documents/sample.zip!/readme.txt", b"overwritten")
    updated = tmp_vfs.cat_archive("alpha://documents/sample.zip!/readme.txt")
    assert updated == b"overwritten"


def test_put_archive_nested(tmp_vfs: VirtualFileSystem):
    tmp_vfs.put_archive("alpha://documents/sample.zip!/data/new_data.csv", b"col1,col2\na,b")
    data = tmp_vfs.cat_archive("alpha://documents/sample.zip!/data/new_data.csv")
    assert data == b"col1,col2\na,b"


def test_rm_archive(tmp_vfs: VirtualFileSystem):
    # Verify it exists first
    entries_before = tmp_vfs.ls_archive("alpha://documents/sample.zip")
    names_before = {e["name"] for e in entries_before}
    assert "readme.txt" in names_before
    # Remove it
    tmp_vfs.rm_archive("alpha://documents/sample.zip!/readme.txt")
    # Verify it's gone
    entries_after = tmp_vfs.ls_archive("alpha://documents/sample.zip")
    names_after = {e["name"] for e in entries_after}
    assert "readme.txt" not in names_after
    # Other files should still exist
    assert "data" in names_after


def test_rm_archive_folder(tmp_vfs: VirtualFileSystem):
    # Remove the data/ folder
    tmp_vfs.rm_archive("alpha://documents/sample.zip!/data")
    entries = tmp_vfs.ls_archive("alpha://documents/sample.zip")
    names = {e["name"] for e in entries}
    assert "data" not in names
    assert "readme.txt" in names


# -- add_root_from_protocol --


def test_add_root_from_protocol(tmp_path):
    vfs = VirtualFileSystem()
    (tmp_path / "hello.txt").write_text("world")
    vfs.add_root_from_protocol("test", "local", base_path=str(tmp_path))
    assert "test" in vfs.roots
    entries = vfs.ls("test://")
    names = [e["name"] for e in entries]
    assert "hello.txt" in names


def test_add_root_from_protocol_memory():
    vfs = VirtualFileSystem()
    vfs.add_root_from_protocol("mem", "memory", base_path="/")
    assert "mem" in vfs.roots
    vfs.put("mem://test.txt", b"hello")
    assert vfs.cat("mem://test.txt") == b"hello"


def test_roots_info(tmp_path):
    vfs = VirtualFileSystem()
    vfs.add_root_from_protocol("disk", "local", base_path=str(tmp_path))
    vfs.add_root_from_protocol("mem", "memory")
    info = vfs.roots_info()
    names = {r["name"] for r in info}
    assert names == {"disk", "mem"}
    disk_info = next(r for r in info if r["name"] == "disk")
    assert disk_info["protocol"] == "file"


def test_on_roots_changed_callback(tmp_path):
    calls = []
    vfs = VirtualFileSystem(on_roots_changed=lambda: calls.append(1))
    vfs.add_root_from_protocol("a", "local", base_path=str(tmp_path))
    assert len(calls) == 1
    vfs.remove_root("a")
    assert len(calls) == 2


def test_remove_root():
    vfs = VirtualFileSystem()
    vfs.add_root_from_protocol("temp", "memory")
    assert "temp" in vfs.roots
    vfs.remove_root("temp")
    assert "temp" not in vfs.roots
    with pytest.raises(FileNotFoundError):
        vfs.ls("temp://")
