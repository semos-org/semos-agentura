"""Demo: VFS tree browser with local test data, optional SharePoint, and remote archives.

Usage:
    uv run panel serve examples/demo_tree.py --show
"""

from __future__ import annotations

import os
from pathlib import Path

import panel as pn
from filesystem_agent.panel_tree import VFSTreeBrowser
from filesystem_agent.vfs import VirtualFileSystem
from fsspec.implementations.local import LocalFileSystem

pn.extension()

# Restore test data from golden snapshot
TEST_DATA = Path(__file__).parent.parent / "test" / "data"
GOLDEN_ZIP = TEST_DATA / "test_data_golden.zip"


def restore_test_data():
    import shutil
    import zipfile

    for subdir in ("local_fs", "webdav_fs"):
        target = TEST_DATA / subdir
        if target.exists():
            shutil.rmtree(target)
    with zipfile.ZipFile(GOLDEN_ZIP, "r") as z:
        z.extractall(TEST_DATA)


restore_test_data()

# Build VFS with local test roots
vfs = VirtualFileSystem()
vfs.add_root("local", LocalFileSystem(), base_path=str(TEST_DATA / "local_fs"))
vfs.add_root("webdav", LocalFileSystem(), base_path=str(TEST_DATA / "webdav_fs"))

# Optional SharePoint root (set SKIP_SHAREPOINT=1 to disable)
if not os.environ.get("SKIP_SHAREPOINT"):
    try:
        from filesystem_agent.config import Settings

        _settings = Settings()
        if _settings.sharepoint_site_url:
            import nest_asyncio

            nest_asyncio.apply()

            from filesystem_agent.auth import CookieAuth, extract_sharepoint_cookies

            print(f"[VFS] Authenticating to {_settings.sharepoint_site_url} ...", flush=True)
            _cookies = extract_sharepoint_cookies(_settings.sharepoint_site_url)
            if _cookies.get("FedAuth"):
                from webdav4.fsspec import WebdavFileSystem

                _sp_fs = WebdavFileSystem(
                    _settings.webdav_base_url,
                    auth=CookieAuth(_cookies),
                )
                vfs.add_root("sharepoint", _sp_fs, base_path=_settings.webdav_folder_path.lstrip("/"))
                print(f"[VFS] SharePoint root added: {_settings.sharepoint_site_url}", flush=True)
            else:
                print("[VFS] SharePoint: no FedAuth cookie (login may be needed)", flush=True)
    except Exception as e:
        print(f"[VFS] SharePoint root skipped: {e}", flush=True)

# Remote archives (downloaded/streamed on demand)
REMOTE_ARCHIVES = [
    {
        "title": "KIproBatt Dataset v0.3.2 (Zenodo, 74 MB, range)",
        "url": "https://zenodo.org/records/11895571/files/KIproBatt/kiprobatt-dataset-v0.3.2.zip?download=1",
        "icon": "bi bi-cloud-arrow-down",
        "range": True,
    },
    {
        "title": "Zenodo #18214281 (full download)",
        "url": "https://zenodo.org/api/records/18214281/files-archive",
        "icon": "bi bi-cloud-arrow-down",
    },
    {
        "title": "OSW Core (GitHub, full download)",
        "url": "https://github.com/OpenSemanticWorld-Packages/world.opensemantic.core/archive/refs/heads/main.zip",
        "icon": "bi bi-github",
    },
]

# Create browser and serve
browser = VFSTreeBrowser(vfs, remote_archives=REMOTE_ARCHIVES)
template = browser.create_template()
template.servable()
