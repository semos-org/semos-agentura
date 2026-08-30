# Filesystem Agent

A virtual filesystem (VFS) with a web-based tree browser. Multiple heterogeneous storage backends (local disk, WebDAV/SharePoint, remote HTTP archives) are unified under a single symbolic URI scheme. The browser is a [Panel](https://panel.holoviz.org/) app using the [Wunderbaum](https://github.com/mar10/wunderbaum) tree widget via [panelini](https://github.com/opensemanticworld/panelini/).

## Quick Start

```bash
uv sync
cp .env.example .env   # edit with your SharePoint URL if needed
uv run panel serve app_tree.py:show
```

For tests:
```bash
uv pip install -e ".[test]"
uv run playwright install chromium
uv run pytest test/ -v
```

## Architecture

```
app_tree.py           Panel app, wires VFS to Wunderbaum tree widget
search_sharepoint.py  CLI: search SharePoint via REST API (cookie auth)
search_teams.py       CLI: list Teams channels/chats, post messages (token auth)
test_webdav.py        Quick smoke test for WebDAV connectivity

src/semos/agentura/files/
  vfs.py              VirtualFileSystem, multi-root fsspec abstraction + archive support
  auth.py             Playwright-based cookie/token extraction (smartcard/SSO login)
  config.py           Pydantic settings (reads .env)
```

## VFS URI Scheme

Every file and directory is addressed by a symbolic URI: `{root}://{path}`.

```
local://documents/report.txt        local filesystem
webdav://shared/data.csv            WebDAV server
sharepoint://General/file.xlsx      SharePoint Online (via WebDAV + cookie auth)
```

### Archive Inner Paths

Archives (zip, tar, etc.) use `!` as separator for inner paths (following the Java `jar:` URI convention):

```
local://documents/sample.zip                  the archive file itself
local://documents/sample.zip!data/file.csv    a file inside the archive
local://documents/sample.zip!images/          a folder inside the archive
```

`!` was chosen over `#` because `#` is a valid filename character on all platforms, making URIs ambiguous. `!` is technically valid in filenames but essentially never used in practice.

### Remote URL Archives

Remote archives are addressed with the `url://` pseudo-root:

```
url://https://zenodo.org/records/123/files/data.zip!readme.txt
```

These are downloaded on demand (or browsed via HTTP range requests if the server supports them).

## VFS API (`VirtualFileSystem`)

```python
from fsspec.implementations.local import LocalFileSystem
from semos.agentura.files.vfs import VirtualFileSystem

vfs = VirtualFileSystem()
vfs.add_root("local", LocalFileSystem(), base_path="/data/local")
vfs.add_root("webdav", some_webdav_fs, base_path="General")
```

### Read operations

| Method | Description |
|--------|-------------|
| `vfs.ls(uri)` | List directory (empty URI lists all roots) |
| `vfs.info(uri)` | Get file/dir metadata (type, size, name) |
| `vfs.isdir(uri)` | Check if URI is a directory |
| `vfs.cat(uri)` | Read file contents as bytes |
| `vfs.tree(uri, depth=N)` | Recursive tree structure (nested dicts) |

### Write operations

| Method | Description |
|--------|-------------|
| `vfs.mkdir(uri)` | Create directory (with parents) |
| `vfs.put(uri, data)` | Write bytes to file |
| `vfs.rm(uri, recursive=)` | Delete file or directory |
| `vfs.mv(src, dst)` | Move/rename (cross-root = copy+delete) |
| `vfs.cp(src, dst)` | Copy (cross-root supported) |

### Archive operations

| Method | Description |
|--------|-------------|
| `vfs.is_archive(uri)` | Check if URI is a supported archive file |
| `vfs.split_archive_uri(uri)` | Split `a.zip!path` into `("a.zip", "path")` |
| `vfs.ls_archive(uri)` | List entries inside an archive |
| `vfs.cat_archive(uri)` | Read a file inside an archive |
| `vfs.put_archive(uri, data)` | Add/replace a file in a zip (rebuilds archive) |
| `vfs.rm_archive(uri)` | Remove a file from a zip (rebuilds archive) |
| `vfs.archive_size(uri)` | Get the outer archive file size |

## Authentication

SharePoint Online requires smartcard/SSO authentication. Since the Windows WebDAV redirector (`net use`) does not support smartcard auth, we use **Playwright** to automate a browser login and extract cookies.

### Flow

1. Playwright opens a Chromium window to the SharePoint site URL
2. User completes smartcard/SSO login
3. Script extracts `FedAuth` + `rtFa` cookies from the browser context
4. Cookies are saved to `.tokens/sharepoint_session.json` for reuse
5. `CookieAuth` injects cookies into all subsequent `httpx`/`webdav4` requests

Session files are cached and reused until they expire (typically ~1 hour for FedAuth). When expired, a new browser window opens automatically.

### Token files

All tokens and session files are stored in `.tokens/` (gitignored):

```
.tokens/
  sharepoint_session.json   SharePoint FedAuth cookies
  teams_session.json        Teams web session state
  teams_tokens_all.json     Teams API bearer tokens (ic3, csa, graph)
  fehrmannhh_session.json   Cross-tenant session (example)
```

### Teams API Access

Teams tokens are captured by intercepting network requests when Teams web loads:

| Token audience | Used for |
|---------------|----------|
| `chatsvcagg.teams.microsoft.com` | Teams/channels list (CSA API) |
| `ic3.teams.office.com` | Chat messages, posting (chatsvc API) |
| `graph.microsoft.com` | Mail, files, calendar (captured via Ctrl+E search trigger) |

The Graph token from Teams **lacks `Chat.Read` scope**, so Teams message search is done by scanning conversations via the chatsvc API, not Graph search.

## WebDAV Quirks (SharePoint Online)

SharePoint's WebDAV implementation has several non-standard behaviors:

- **PROPFIND on individual files fails**:only works on folders. Workaround: fall back to HTTP HEAD/GET/DELETE.
- **No range request support**:the WebDAV endpoint does not support `Range` headers. Range requests work on the SharePoint REST API (`/_api/`) URLs but not on WebDAV URLs.
- **Path encoding**:spaces in folder names (e.g. `Freigegebene Dokumente`) must be URL-encoded.
- **Cross-tenant access**:each SharePoint tenant requires its own set of cookies. The session file only works for the tenant it was created for.

The `VirtualFileSystem` class includes fallback methods (`_cat_robust`, `_get_http_url`) that detect WebDAV backends and fall back to direct HTTP operations when the standard fsspec methods fail.

## Tree Browser (`app_tree.py`)

The Panel app renders the VFS as an interactive tree using Wunderbaum (via panelini). Features:

- **Multi-root tree**:each VFS root is a top-level node
- **Lazy loading**:archive contents and deep directories load on expand
- **Drag and drop**:default=move, Ctrl+drag=copy, cross-root supported
- **Context menu**:right-click for New Folder, New File, Delete, Copy, Paste
- **Inline rename**:F2 or click-active to rename files/folders
- **Archive browsing**:zip files appear as expandable nodes
- **Archive write**:drop/paste/delete inside zips triggers rebuild with confirmation popup
- **Remote archives**:Zenodo, GitHub zips browsable via HTTP range requests
- **SharePoint integration**:if `SHAREPOINT_SITE_URL` is set, appears as a root
- **File upload**:sidebar file picker to upload to selected folder
- **Golden test data**:`test/data/test_data_golden.zip` restores clean state on every app start

### Wunderbaum Integration Notes

The tree widget runs inside Bokeh's **shadow DOM**, which has implications:

- **CSS selectors**:Playwright tests must use `css=.wb-row` (shadow-piercing) instead of regular selectors
- **Ctrl key detection**:Playwright's `keyboard.down("Control")` does not reach shadow DOM `keydown` listeners. A `window.__wbForceCopy` test hook was added to panelini to force copy mode in tests.
- **`set_source()` resets expansion**:avoid unless necessary. Use `add_file()`, `add_folder()`, `remove_node()`, `update_node()`, `move_node()` for incremental updates.
- **`update_node(old_key, {"key": new_key})`**:essential after DnD moves and renames to keep node keys in sync with VFS URIs.

### Key Event Flow

```
User drags A onto B (JS)
  1. Wunderbaum calls moveTo/copyTo (visual update)
  2. Vue emits "drop" event with sourceKey, targetKey, region, copy flag
  3. Python on_tree_event("drop", ...) receives it
  4. VFS mv/cp/put/rm executed
  5. tree.update_node() fixes the node key (no full rebuild)
```

For archive writes:
```
Drop into zip (or delete from zip)
  1. _request_archive_write() shows confirmation popup with archive size
  2. User clicks Confirm
  3. _do_archive_confirm() reads file, rebuilds zip, writes back
  4. tree.add_file() / tree.remove_node() updates tree incrementally
```

## Test Data

Test data lives in `test/data/` with two filesystem roots:

```
test/data/
  test_data_golden.zip     golden snapshot, extracted on every app start
  local_fs/                "local" root
    README.md
    documents/
      budget.csv, meeting_notes.txt, report_2025.txt, sample.zip
    images/
      logo.txt, screenshot.txt
  webdav_fs/               "webdav" root
    notes.txt
    archive/
      2023/legacy_data.txt
      2024/old_report.txt
    shared/
      contract_draft.txt, presentation.txt, reports.zip
```

`sample.zip` and `reports.zip` contain test data for archive browsing/writing.

### Updating the Golden Snapshot

After changing test data, recreate the golden zip:
```python
import zipfile, os
with zipfile.ZipFile("test/data/test_data_golden.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk("test/data/local_fs"):
        for f in files:
            path = os.path.join(root, f)
            z.write(path, os.path.relpath(path, "test/data"))
    for root, dirs, files in os.walk("test/data/webdav_fs"):
        for f in files:
            path = os.path.join(root, f)
            z.write(path, os.path.relpath(path, "test/data"))
```

## Configuration

All settings come from `.env` (via pydantic-settings):

| Variable | Description | Default |
|----------|-------------|---------|
| `SHAREPOINT_SITE_URL` | SharePoint site URL | *(empty, disables SharePoint root)* |
| `SHAREPOINT_DOC_LIBRARY` | Document library name | `Freigegebene Dokumente` |
| `SHAREPOINT_SUBFOLDER` | Subfolder within library | `General` |
| `SHAREPOINT_DRIVE_PATH` | Mapped network drive path | *(empty)* |
| `AZURE_TENANT_ID` | Azure AD tenant GUID | *(empty)* |
| `AZURE_CLIENT_ID` | App registration client ID | *(empty)* |
| `AZURE_CLIENT_SECRET` | App registration secret | *(empty)* |
| `SKIP_SHAREPOINT` | Set to `1` to disable SharePoint root (used in tests) | *(unset)* |

## Dependencies

Core: `fsspec`, `webdav4[fsspec]`, `httpx`, `panel`, `panelini`, `playwright`, `pydantic-settings`, `msal`, `nest-asyncio`

Test: `pytest`, `pytest-playwright`, `wsgidav` (local WebDAV server for integration tests)
