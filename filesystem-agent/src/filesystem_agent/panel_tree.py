"""Reusable Panel tree browser for the VirtualFileSystem.

Provides ``VFSTreeBrowser`` which wires a Wunderbaum tree widget to a VFS
instance, with DnD, context menu, inline rename, archive browsing, and
remote URL archive support.

Usage::

    from filesystem_agent.panel_tree import VFSTreeBrowser

    browser = VFSTreeBrowser(vfs, remote_archives=[...])
    template = browser.create_template(title="My Browser")
    template.servable()
"""

from __future__ import annotations

from typing import Any

import panel as pn
from panelini.panels.wunderbaum import Wunderbaum

from .vfs import ARCHIVE_SEPARATOR, VirtualFileSystem

# Helpers


def _icon_for_file(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    icons = {
        "txt": "bi bi-file-earmark-text",
        "md": "bi bi-file-earmark-richtext",
        "csv": "bi bi-file-earmark-spreadsheet",
        "py": "bi bi-file-earmark-code",
        "jpg": "bi bi-file-earmark-image",
        "png": "bi bi-file-earmark-image",
        "pdf": "bi bi-file-earmark-pdf",
    }
    return icons.get(ext, "bi bi-file-earmark")


def _format_size(size: Any) -> str:
    if not size:
        return ""
    size = int(size)
    if size > 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size > 1_000:
        return f"{size / 1_000:.1f} KB"
    return f"{size} B"


def _format_modified(ts: Any) -> str:
    """Format a timestamp for display."""
    from datetime import datetime

    if isinstance(ts, (int, float)):
        try:
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%Y-%m-%d %H:%M")
        except (OSError, ValueError):
            return ""
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    if isinstance(ts, str):
        return ts[:16]  # trim to YYYY-MM-DD HH:MM
    return ""


def _basename(uri: str) -> str:
    """Extract the filename from a URI, handling archive inner paths."""
    if ARCHIVE_SEPARATOR in uri:
        _, inner = uri.split(ARCHIVE_SEPARATOR, 1)
        return inner.rstrip("/").rsplit("/", 1)[-1]
    return uri.rstrip("/").rsplit("/", 1)[-1]


class VFSTreeBrowser:
    """Interactive tree browser backed by a VirtualFileSystem.

    Parameters
    ----------
    vfs : VirtualFileSystem
        The VFS instance to browse.
    remote_archives : list[dict] | None
        Optional list of remote archives to show as top-level lazy nodes.
        Each dict has keys: title, url, icon (optional), range (optional bool).
    preload_depth : int
        How many directory levels to preload on startup.
    archive_prompt_threshold : int
        Archive modifications above this size (bytes) show a confirmation popup.
        Default 0 means always prompt.
    """

    def __init__(
        self,
        vfs: VirtualFileSystem,
        *,
        remote_archives: list[dict] | None = None,
        preload_depth: int = 5,
        archive_prompt_threshold: int = 0,
    ):
        self.vfs = vfs
        self.remote_archives = remote_archives or []
        self.preload_depth = preload_depth
        self.archive_prompt_threshold = archive_prompt_threshold

        # Optional callbacks for UI-specific context menu actions.
        # Set by the host (e.g. __main__.py) after construction.
        # on_preview(uri): show file preview (no size limit)
        # on_download(uri): trigger browser download
        # on_add_to_chat(uri): register file in chat context
        self.on_preview: Any | None = None
        self.on_download: Any | None = None
        self.on_add_to_chat: Any | None = None

        # Auto-refresh tree when roots are added/removed
        self.vfs._on_roots_changed = self._on_roots_changed

        self._remote_cache: dict[str, Any] = {}
        self._clipboard: dict[str, str | None] = {"uri": None}
        self._counter = {"value": 0}
        self._pending_archive_op: dict[str, Any] = {}

        # UI widgets
        self.uri_display = pn.widgets.TextInput(
            name="Symbolic URI",
            value="",
            placeholder="Select a file/folder",
            disabled=True,
            sizing_mode="stretch_width",
        )
        self.info_display = pn.pane.JSON({}, name="File Info", depth=2, sizing_mode="stretch_width")
        self.status = pn.pane.Markdown("Right-click for context menu. F2 to rename. Drag to move.")
        self.folder_name_input = pn.widgets.TextInput(
            name="New folder name",
            placeholder="my_folder",
            sizing_mode="stretch_width",
        )
        self.file_input = pn.widgets.FileInput(name="Upload file", sizing_mode="stretch_width")
        self.file_input.param.watch(self._on_file_upload, "value")

        # Archive confirmation dialog
        self._dialog_text = pn.pane.Markdown("", sizing_mode="stretch_width")
        self._btn_confirm = pn.widgets.Button(
            name="Confirm rebuild",
            button_type="warning",
            sizing_mode="stretch_width",
        )
        self._btn_cancel = pn.widgets.Button(
            name="Cancel",
            button_type="light",
            sizing_mode="stretch_width",
        )
        self._btn_confirm.on_click(self._do_archive_confirm)
        self._btn_cancel.on_click(self._do_archive_cancel)
        self.archive_dialog = pn.Column(
            self._dialog_text,
            pn.Row(self._btn_confirm, self._btn_cancel, sizing_mode="stretch_width"),
            sizing_mode="stretch_width",
            visible=False,
            styles={
                "background": "#fff3cd",
                "border": "1px solid #ffc107",
                "border-radius": "8px",
                "padding": "12px",
                "position": "absolute",
                "top": "10px",
                "left": "10px",
                "right": "10px",
                "z-index": "1000",
                "box-shadow": "0 4px 12px rgba(0,0,0,0.3)",
            },
        )

        # Refresh button
        self._btn_refresh = pn.widgets.Button(
            name="Refresh",
            button_type="light",
            sizing_mode="stretch_width",
        )
        self._btn_refresh.on_click(self._do_refresh)

        # Tree widget
        self.tree = Wunderbaum(
            source=self.build_source(),
            columns=[
                {"id": "*", "title": "Name", "width": "250px", "sortable": True},
                {"id": "size", "title": "Size", "width": "80px", "sortable": True},
                {
                    "id": "modified",
                    "title": "Modified",
                    "width": "130px",
                    "sortable": True,
                    "sortOrder": "desc",
                },
            ],
            context_menu_items=[
                {"id": "preview", "label": "Preview", "icon": "bi bi-eye"},
                {"id": "download", "label": "Download", "icon": "bi bi-download"},
                {"id": "add_to_chat", "label": "Add to Chat", "icon": "bi bi-chat-left-text"},
                {"id": "---"},
                {"id": "new_folder", "label": "New Folder", "icon": "bi bi-folder-plus"},
                {"id": "new_file", "label": "New File", "icon": "bi bi-file-earmark-plus"},
                {"id": "delete", "label": "Delete", "icon": "bi bi-trash"},
                {"id": "copy", "label": "Copy", "icon": "bi bi-clipboard"},
                {"id": "paste", "label": "Paste", "icon": "bi bi-clipboard-check"},
            ],
            options={
                "checkbox": False,
                "dnd": True,
                "edit": {"trigger": ["clickActive", "F2"]},
                "columnsSortable": True,
            },
            tree_event_callback=self.on_tree_event,
            lazy_load_callback=self.on_lazy_load,
            file_drop_callback=self.on_file_drop,
        )

    # Source building

    def build_source(self, depth: int | None = None) -> list[dict]:
        """Convert VFS tree to Wunderbaum source format."""
        if depth is None:
            depth = self.preload_depth

        def _to_wb(node: dict, is_root: bool = False) -> dict:
            is_dir = node["type"] == "directory"
            result: dict = {"title": node["name"], "key": node["uri"]}
            is_archive = not is_dir and self.vfs.is_archive(node["uri"])

            if is_root:
                result["icon"] = "bi bi-hdd"
                result["expanded"] = True
            elif is_dir:
                result["icon"] = "bi bi-folder-fill"
            elif is_archive:
                result["icon"] = "bi bi-file-earmark-zip"
            else:
                result["icon"] = _icon_for_file(node["name"])
                result["size"] = _format_size(node.get("size", 0))

            # Timestamp (if available from filesystem)
            mod = node.get("modified")
            if mod:
                result["modified"] = _format_modified(mod)

            if is_dir:
                raw = node.get("children")
                if isinstance(raw, list) and raw:
                    # Sort by modified, newest first
                    sorted_children = sorted(
                        raw,
                        key=lambda c: -(c.get("modified") or 0) if isinstance(c.get("modified"), (int, float)) else 0,
                    )
                    result["children"] = [_to_wb(c) for c in sorted_children]
                elif isinstance(raw, list) and not raw:
                    # Empty list = depth boundary, enable lazy load
                    result["lazy"] = True
                else:
                    # No children key at all - also lazy
                    result["lazy"] = True
            elif is_archive:
                result["lazy"] = True
                result["size"] = _format_size(node.get("size", 0))

            return result

        root_nodes = self.vfs.tree("", depth=depth)
        source = [_to_wb(n, is_root=True) for n in root_nodes]

        for ra in self.remote_archives:
            source.append(
                {
                    "title": ra["title"],
                    "key": f"url://{ra['url']}",
                    "icon": ra.get("icon", "bi bi-cloud-arrow-down"),
                    "lazy": True,
                }
            )
        return source

    def _tree_add_node(self, parent_key: str, name: str, uri: str, size: int = 0):
        data: dict[str, str] = {"size": _format_size(size)}
        try:
            info = self.vfs.info(uri)
            mod = info.get("modified")
            if mod:
                data["modified"] = _format_modified(mod)
        except Exception:
            pass

        if self.vfs.is_archive(uri):
            self.tree.add_node(
                parent_key,
                {
                    "title": name,
                    "key": uri,
                    "icon": "bi bi-file-earmark-zip",
                    "lazy": True,
                    **data,
                },
            )
        else:
            data["icon"] = _icon_for_file(name)
            self.tree.add_file(parent_key, name, data=data, key=uri)

    # Event handling

    def on_tree_event(self, event_name: str, event_params: dict) -> None:
        print(f"[tree] {event_name}: {list(event_params.keys())}", flush=True)

        if event_name == "activate":
            self._handle_activate(event_params)
        elif event_name == "contextmenu":
            self._handle_contextmenu(event_params)
        elif event_name == "drop":
            self._handle_drop(event_params)
        elif event_name == "edit.apply":
            self._handle_rename(event_params)

    def _handle_activate(self, params: dict):
        key = params.get("key", "")
        self.uri_display.value = key
        try:
            self.info_display.object = self.vfs.info(key)
        except Exception:
            self.info_display.object = {}

    def _handle_contextmenu(self, params: dict):
        action = params.get("action", "")
        key = params.get("key", "")
        title = params.get("title", "")

        if action == "preview":
            if self.on_preview and key:
                self.on_preview(key)
            return

        if action == "download":
            if self.on_download and key:
                self.on_download(key)
            return

        if action == "add_to_chat":
            if self.on_add_to_chat and key:
                self.on_add_to_chat(key)
            return

        if action == "new_folder":
            self._counter["value"] += 1
            name = self.folder_name_input.value.strip() or f"new_folder_{self._counter['value']}"
            new_uri = f"{key.rstrip('/')}/{name}" if not key.endswith("://") else f"{key}{name}"
            try:
                self.vfs.mkdir(new_uri)
                self.tree.add_folder(key, name, key=new_uri)
                self.tree.expand_node(key, True)
                self.status.object = f"**Created** `{new_uri}`"
                self.folder_name_input.value = ""
            except Exception as e:
                self.status.object = f"**Error:** {e}"

        elif action == "new_file":
            self._counter["value"] += 1
            name = f"new_file_{self._counter['value']}.txt"
            new_uri = f"{key.rstrip('/')}/{name}" if not key.endswith("://") else f"{key}{name}"
            try:
                self.vfs.put(new_uri, b"")
                self.tree.add_file(key, name, data={"size": "0 B"}, key=new_uri)
                self.tree.expand_node(key, True)
                self.status.object = f"**Created** `{new_uri}`"
            except Exception as e:
                self.status.object = f"**Error:** {e}"

        elif action == "delete":
            try:
                if ARCHIVE_SEPARATOR in key:
                    self._request_archive_write("delete_from", key, key, key, title)
                else:
                    try:
                        is_dir = self.vfs.isdir(key)
                    except Exception:
                        is_dir = False
                    self.vfs.rm(key, recursive=is_dir)
                    self.tree.remove_node(key)
                    self.status.object = f"**Deleted** `{title}` (`{key}`)"
            except Exception as e:
                self.status.object = f"**Error:** {e}"

        elif action == "copy":
            self._clipboard["uri"] = key
            self.status.object = f"**Copied** `{key}` - right-click target folder, then Paste"

        elif action == "paste":
            self._handle_paste(key)

    def _handle_paste(self, key: str):
        src = self._clipboard.get("uri")
        if not src:
            self.status.object = "**Nothing in clipboard** - Copy first"
            return
        basename = _basename(src)
        key_is_archive = ARCHIVE_SEPARATOR in key or self.vfs.is_archive(key)
        if key_is_archive and ARCHIVE_SEPARATOR not in key:
            dst = f"{key}{ARCHIVE_SEPARATOR}{basename}"
        elif key_is_archive:
            dst = f"{key.rstrip('/')}/{basename}"
        else:
            dst = f"{key.rstrip('/')}/{basename}" if not key.endswith("://") else f"{key}{basename}"
        try:
            if key_is_archive:
                self._request_archive_write("paste_into", src, key, dst, basename)
            elif src.startswith("url://") or ARCHIVE_SEPARATOR in src:
                data = self._read_any(src)
                self.vfs.put(dst, data)
                self._tree_add_node(key, basename, dst, len(data))
                self.tree.expand_node(key, True)
                self._clipboard["uri"] = None
                self.status.object = f"**Pasted (extracted)** `{basename}` to `{dst}`"
            else:
                self.vfs.cp(src, dst)
                is_dir = self.vfs.isdir(dst)
                if is_dir:
                    self.tree.add_folder(key, basename, key=dst)
                else:
                    self._tree_add_node(key, basename, dst, self.vfs.info(dst).get("size", 0))
                self.tree.expand_node(key, True)
                self._clipboard["uri"] = None
                self.status.object = f"**Pasted** `{src}` to `{dst}`"
        except Exception as e:
            self.status.object = f"**Error:** {e}"

    def _handle_drop(self, params: dict):
        src_key = params.get("sourceKey", "")
        tgt_key = params.get("targetKey", "")
        orig_tgt_key = tgt_key
        region = params.get("region", "over")
        is_copy = params.get("copy", False) or "copiedNodeId" in params
        if not (src_key and tgt_key and src_key != tgt_key):
            return

        basename = _basename(src_key)
        src_is_archive = ARCHIVE_SEPARATOR in src_key

        # Resolve target to a folder
        if region in ("before", "after"):
            tgt_key = tgt_key.rstrip("/").rsplit("/", 1)[0]
            if "://" not in tgt_key:
                tgt_key = src_key.split("://")[0] + "://"

        tgt_is_archive = ARCHIVE_SEPARATOR in tgt_key or self.vfs.is_archive(tgt_key)
        if not tgt_is_archive and not tgt_key.endswith("://"):
            try:
                if not self.vfs.isdir(tgt_key):
                    tgt_key = tgt_key.rstrip("/").rsplit("/", 1)[0]
                    if "://" not in tgt_key:
                        tgt_key = src_key.split("://")[0] + "://"
            except Exception:
                pass

        tgt_redirected = tgt_key != orig_tgt_key
        tgt_is_archive = ARCHIVE_SEPARATOR in tgt_key or self.vfs.is_archive(tgt_key)

        # Build destination URI
        if tgt_is_archive and ARCHIVE_SEPARATOR not in tgt_key:
            dst = f"{tgt_key}{ARCHIVE_SEPARATOR}{basename}"
        elif tgt_is_archive:
            dst = f"{tgt_key.rstrip('/')}/{basename}"
        else:
            dst = f"{tgt_key.rstrip('/')}/{basename}" if not tgt_key.endswith("://") else f"{tgt_key}{basename}"

        src_parent = src_key.rstrip("/").rsplit("/", 1)[0]
        if "://" not in src_parent:
            src_parent = src_key.split("://")[0] + "://"

        if src_key == dst:
            if not is_copy and tgt_redirected:
                self.tree.move_node(src_key, src_parent, "child")
            return

        op = "copy" if is_copy else "move"
        modifies_archive = tgt_is_archive or (src_is_archive and not is_copy)
        if modifies_archive:
            archive_to_check = self._archive_uri_to_outer(tgt_key if tgt_is_archive else src_key)
            if self._needs_archive_prompt(archive_to_check):
                self._request_archive_write(
                    f"{op}_{'into' if tgt_is_archive else 'from'}",
                    src_key,
                    tgt_key,
                    dst,
                    basename,
                )
                return

        try:
            if is_copy:
                data = self._read_any(src_key)
                if tgt_is_archive:
                    self.vfs.put_archive(dst, data)
                else:
                    self.vfs.put(dst, data)
                self._tree_add_node(tgt_key, basename, dst, len(data))
                self.tree.expand_node(tgt_key, True)
                self.status.object = f"**Copied** `{basename}` to `{dst}`"
            else:
                data = self._read_any(src_key)
                if tgt_is_archive:
                    self.vfs.put_archive(dst, data)
                else:
                    self.vfs.put(dst, data)
                if src_is_archive:
                    self.vfs.rm_archive(src_key)
                else:
                    self.vfs.rm(src_key)
                # update_node can't change keys in Wunderbaum JS, so
                # remove + re-add to get the correct URI on the node.
                self.tree.remove_node(src_key)
                self._tree_add_node(tgt_key, basename, dst, len(data))
                self.tree.expand_node(tgt_key, True)
                self.status.object = f"**Moved** `{basename}` to `{dst}`"
        except Exception as e:
            self.status.object = f"**Error:** {e}"
            if not is_copy:
                try:
                    self.tree.move_node(src_key, src_parent, "child")
                except Exception:
                    pass

    def _handle_rename(self, params: dict):
        key = params.get("key", "")
        new_title = params.get("newValue", "")
        if key and new_title:
            parent = key.rstrip("/").rsplit("/", 1)[0]
            if "://" not in parent:
                parent = key.split("://")[0] + "://"
            new_uri = f"{parent}/{new_title}" if not parent.endswith("://") else f"{parent}{new_title}"
            try:
                is_dir = self.vfs.isdir(key)
                self.vfs.mv(key, new_uri)
                # update_node can't change keys in Wunderbaum JS, so
                # remove + re-add to get the correct URI on the node.
                self.tree.remove_node(key)
                if is_dir:
                    self.tree.add_folder(parent, new_title, key=new_uri)
                else:
                    size = self.vfs.info(new_uri).get("size", 0)
                    self._tree_add_node(parent, new_title, new_uri, size)
                self.status.object = f"**Renamed** `{key}` to `{new_uri}`"
            except Exception as e:
                self.status.object = f"**Rename error:** {e}"

    # Read from any source

    def _read_any(self, key: str) -> bytes:
        if key.startswith("url://"):
            return self._read_url_archive_file(key)
        if ARCHIVE_SEPARATOR in key:
            return self.vfs.cat_archive(key)
        return self.vfs.cat(key)

    def _read_url_archive_file(self, key: str) -> bytes:
        import io
        import zipfile

        raw = key[len("url://") :]
        url, inner = raw.split(ARCHIVE_SEPARATOR, 1)
        inner = inner.strip("/")

        if url not in self._remote_cache:
            raise FileNotFoundError(f"Remote archive not cached: {url} (expand it first)")

        cached = self._remote_cache[url]
        if isinstance(cached, bytes):
            zf = zipfile.ZipFile(io.BytesIO(cached))
        else:
            cached.seek(0)
            zf = zipfile.ZipFile(cached)
        return zf.read(inner)

    # Lazy loading

    def on_lazy_load(self, key: str, request_data: dict) -> list[dict]:
        try:
            if key.startswith("url://"):
                return self._list_url_archive(key)

            # Regular VFS directory (depth boundary)
            if ARCHIVE_SEPARATOR not in key and "://" in key:
                try:
                    if self.vfs.isdir(key):
                        return self._list_vfs_dir(key)
                except Exception:
                    pass

            # Archive contents
            entries = self.vfs.ls_archive(key)
            children = []
            for e in entries:
                is_dir = e["type"] == "directory"
                node: dict = {"title": e["name"], "key": e["uri"]}
                if is_dir:
                    node["icon"] = "bi bi-folder-fill"
                    node["lazy"] = True
                else:
                    node["icon"] = _icon_for_file(e["name"])
                    node["size"] = _format_size(e.get("size", 0))
                children.append(node)
            return children
        except Exception as exc:
            print(f"[lazy_load] Error loading {key}: {exc}", flush=True)  # noqa: T201
            return [
                {
                    "title": f"Error: {exc}",
                    "key": f"{key}{ARCHIVE_SEPARATOR}__error__",
                    "icon": "bi bi-exclamation-triangle",
                }
            ]

    def _list_vfs_dir(self, uri: str) -> list[dict]:
        """List a VFS directory for lazy loading."""
        entries = self.vfs.ls(uri)
        # Sort by modified, newest first (same as build_source)
        entries.sort(
            key=lambda e: -(e.get("modified") or 0) if isinstance(e.get("modified"), (int, float)) else 0,
        )
        children = []
        for e in entries:
            is_dir = e["type"] == "directory"
            is_archive = not is_dir and self.vfs.is_archive(e["uri"])
            node: dict = {"title": e["name"], "key": e["uri"]}
            if is_dir:
                node["icon"] = "bi bi-folder-fill"
                node["lazy"] = True
            elif is_archive:
                node["icon"] = "bi bi-file-earmark-zip"
                node["lazy"] = True
                node["size"] = _format_size(e.get("size", 0))
            else:
                node["icon"] = _icon_for_file(e["name"])
                node["size"] = _format_size(e.get("size", 0))
            mod = e.get("modified")
            if mod:
                node["modified"] = _format_modified(mod)
            children.append(node)
        return children

    def _list_url_archive(self, key: str) -> list[dict]:
        import io
        import zipfile

        raw = key[len("url://") :]
        if ARCHIVE_SEPARATOR in raw:
            url, inner = raw.split(ARCHIVE_SEPARATOR, 1)
            inner = inner.strip("/")
        else:
            url = raw
            inner = ""

        is_range = any(ra.get("range") and ra["url"] == url for ra in self.remote_archives)

        if url not in self._remote_cache:
            if is_range:
                from .vfs import _HttpRangeFile

                print(f"[lazy_load] Opening {url} via range requests ...", flush=True)
                self._remote_cache[url] = _HttpRangeFile(url)
                print(f"[lazy_load] Remote size: {self._remote_cache[url]._size} bytes", flush=True)
            else:
                import httpx

                print(f"[lazy_load] Downloading {url} ...", flush=True)
                r = httpx.get(url, follow_redirects=True, timeout=120)
                r.raise_for_status()
                self._remote_cache[url] = r.content
                print(f"[lazy_load] Cached {len(r.content)} bytes", flush=True)

        cached = self._remote_cache[url]
        if isinstance(cached, bytes):
            zf = zipfile.ZipFile(io.BytesIO(cached))
        else:
            cached.seek(0)
            zf = zipfile.ZipFile(cached)

        prefix = (inner + "/") if inner else ""
        seen_dirs: set[str] = set()
        children: list[dict] = []

        for info in zf.infolist():
            name = info.filename
            if not name.startswith(prefix):
                continue
            remainder = name[len(prefix) :]
            if not remainder or remainder == "/":
                continue

            parts = remainder.split("/")
            if len(parts) == 1 or (len(parts) == 2 and parts[1] == ""):
                entry_name = parts[0]
                is_dir = name.endswith("/")
                if is_dir:
                    if entry_name in seen_dirs:
                        continue
                    seen_dirs.add(entry_name)
                inner_path = f"{inner}/{entry_name}".strip("/")
                child_key = f"url://{url}{ARCHIVE_SEPARATOR}{inner_path}"
                node: dict = {"title": entry_name, "key": child_key}
                if is_dir:
                    node["icon"] = "bi bi-folder-fill"
                    node["lazy"] = True
                else:
                    node["icon"] = _icon_for_file(entry_name)
                    node["size"] = _format_size(info.file_size)
                children.append(node)
            else:
                dir_name = parts[0]
                if dir_name not in seen_dirs:
                    seen_dirs.add(dir_name)
                    inner_path = f"{inner}/{dir_name}".strip("/")
                    child_key = f"url://{url}{ARCHIVE_SEPARATOR}{inner_path}"
                    children.append(
                        {
                            "title": dir_name,
                            "key": child_key,
                            "icon": "bi bi-folder-fill",
                            "lazy": True,
                        }
                    )
        return children

    # File drop / upload

    def on_file_drop(self, event_params: dict) -> None:
        files = event_params.get("files", [])
        target_key = event_params.get("targetKey", "")
        if not target_key:
            self.status.object = "**Drop target unknown**"
            return
        names = []
        for f in files:
            name = f.get("name", "dropped_file")
            uri = f"{target_key.rstrip('/')}/{name}" if not target_key.endswith("://") else f"{target_key}{name}"
            try:
                self.vfs.put(uri, b"")
                self._tree_add_node(target_key, name, uri)
                names.append(name)
            except Exception as e:
                self.status.object = f"**Drop error:** {e}"
                return
        self.status.object = f"**Dropped** {', '.join(names)} into `{target_key}`"

    def _on_file_upload(self, event):
        if not event.new:
            return
        active_uri = self.uri_display.value
        if not active_uri:
            self.status.object = "**Select a target folder first**"
            return
        filename = self.file_input.filename
        target = f"{active_uri.rstrip('/')}/{filename}" if not active_uri.endswith("://") else f"{active_uri}{filename}"
        try:
            self.vfs.put(target, event.new)
            self._tree_add_node(active_uri, filename, target, len(event.new))
            self.tree.expand_node(active_uri, True)
            self.status.object = f"**Uploaded** `{filename}` to `{target}`"
        except Exception as e:
            self.status.object = f"**Upload error:** {e}"

    # Archive write confirmation

    def _archive_uri_to_outer(self, uri: str) -> str:
        return uri.split(ARCHIVE_SEPARATOR, 1)[0]

    def _needs_archive_prompt(self, archive_uri: str) -> bool:
        try:
            size = self.vfs.archive_size(archive_uri)
        except Exception:
            size = 0
        return size >= self.archive_prompt_threshold

    def _request_archive_write(self, op: str, src: str, tgt: str, dst: str, basename: str):
        archive_outer = self._archive_uri_to_outer(tgt if "into" in op else src)
        try:
            size = self.vfs.archive_size(archive_outer)
        except Exception:
            size = 0
        size_str = _format_size(size) if size else "unknown size"

        src_parent = src.rstrip("/").rsplit("/", 1)[0]
        if "://" not in src_parent:
            src_parent = src.split("://")[0] + "://"

        self._pending_archive_op.clear()
        self._pending_archive_op.update(
            {
                "op": op,
                "src": src,
                "tgt": tgt,
                "dst": dst,
                "basename": basename,
                "archive": archive_outer,
                "src_parent": src_parent,
            }
        )

        archive_name = _basename(archive_outer)
        actions = {
            "move_into": f"Move **{basename}** into **{archive_name}**",
            "copy_into": f"Copy **{basename}** into **{archive_name}**",
            "move_from": f"Move **{basename}** out of **{archive_name}**",
            "copy_from": f"Copy **{basename}** from **{archive_name}**",
            "delete_from": f"Delete **{basename}** from **{archive_name}**",
            "paste_into": f"Paste **{basename}** into **{archive_name}**",
        }
        desc = actions.get(op, f"Modify **{archive_name}**")
        self._dialog_text.object = f"{desc}\n\nThis requires rebuilding `{archive_name}` ({size_str})."
        self.archive_dialog.visible = True

    def _do_archive_confirm(self, event):
        op = self._pending_archive_op.get("op")
        if not op:
            return
        src = self._pending_archive_op["src"]
        tgt = self._pending_archive_op["tgt"]
        dst = self._pending_archive_op["dst"]
        basename = self._pending_archive_op["basename"]
        archive = self._pending_archive_op["archive"]

        self.archive_dialog.visible = False
        src_is_archive = ARCHIVE_SEPARATOR in src
        tgt_is_archive = ARCHIVE_SEPARATOR in tgt or self.vfs.is_archive(tgt)
        is_move = op.startswith("move") or op == "delete_from"

        try:
            if op == "delete_from":
                self.vfs.rm_archive(src)
                self.tree.remove_node(src)
            else:
                data = self._read_any(src)
                if tgt_is_archive:
                    self.vfs.put_archive(dst, data)
                else:
                    self.vfs.put(dst, data)
                if is_move:
                    if src_is_archive:
                        self.vfs.rm_archive(src)
                    else:
                        self.vfs.rm(src)
                    self.tree.remove_node(src)
                if op == "paste_into":
                    self._clipboard["uri"] = None
                self._tree_add_node(tgt, basename, dst, len(data))
                self.tree.expand_node(tgt, True)

            try:
                new_size = self.vfs.archive_size(archive)
                self.tree.update_node(archive, {"size": _format_size(new_size)})
            except Exception:
                pass

            verb = "Moved" if is_move else ("Deleted" if op == "delete_from" else "Copied")
            self.status.object = f"**{verb}** `{basename}` (archive rebuilt)"
        except Exception as e:
            self.status.object = f"**Archive write error:** {e}"
        finally:
            self._pending_archive_op.clear()

    def _do_archive_cancel(self, event):
        op = self._pending_archive_op.get("op", "")
        src = self._pending_archive_op.get("src", "")
        src_parent = self._pending_archive_op.get("src_parent", "")
        self.archive_dialog.visible = False
        if op.startswith("move") and src and src_parent:
            self.tree.move_node(src, src_parent, "child")
        self._pending_archive_op.clear()
        self.status.object = "**Cancelled**"

    # Refresh

    def _on_roots_changed(self):
        """Called by VFS when roots are added or removed."""
        try:
            self.tree.set_source(self.build_source())
        except Exception:
            pass  # broken root should not poison other VFS operations
        self.status.object = "**Roots updated**"

    def _do_refresh(self, event):
        self.tree.set_source(self.build_source())
        self.status.object = "**Refreshed**"

    # Template factory

    def create_template(
        self,
        *,
        title: str = "Virtual Filesystem Browser",
        site: str = "Filesystem Agent",
    ) -> pn.template.FastListTemplate:
        sidebar = pn.Column(
            pn.pane.Markdown("## Selected"),
            self.uri_display,
            self.info_display,
            pn.layout.Divider(),
            pn.pane.Markdown("## New Folder"),
            self.folder_name_input,
            pn.pane.Markdown(
                "*(Right-click a folder, then New Folder)*", styles={"color": "#888", "font-size": "12px"}
            ),
            pn.layout.Divider(),
            pn.pane.Markdown("## Upload"),
            self.file_input,
            pn.layout.Divider(),
            self._btn_refresh,
            self.status,
            sizing_mode="stretch_width",
        )
        main_area = pn.Column(
            self.archive_dialog,
            self.tree,
            sizing_mode="stretch_both",
            styles={"position": "relative"},
        )
        return pn.template.FastListTemplate(
            site=site,
            title=title,
            main=[main_area],
            sidebar=[sidebar],
        )
