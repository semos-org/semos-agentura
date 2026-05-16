"""File registry backed by VFS for agentura-ui.

Re-exports protocol-level middleware from agentura-commons.
Adds VFSFileRegistry that syncs registered files to a VFS root
so the VFSTreeBrowser sidebar shows them in real time.
"""

from agentura_commons.file_middleware import (  # noqa: F401
    FileEntry,
    FileRegistry,
    _fetch_and_register,
    _has_file_attachment_schema,
    _identify_file_params,
    _make_file_attachment,
    _resolve_embedded_refs,
    human_size,
    post_process_tool_result,
    pre_process_tool_call,
)
from agentura_commons.mcp_client import AgentConnection  # noqa: F401


class VFSFileRegistry(FileRegistry):
    """FileRegistry that also writes files to a VFS root.

    All registered files are mirrored in the VFS so the
    VFSTreeBrowser shows them. The blob stays in the in-memory
    _files dict (for fast access by middleware) AND in the VFS
    (for the tree UI).
    """

    def __init__(self, vfs, root: str = "session") -> None:
        super().__init__()
        self._vfs = vfs
        self._root = root

    def register(
        self,
        filename: str,
        blob: bytes,
        mime: str,
        source: str,
    ) -> FileEntry:
        # Prefix with VFS root so the LLM sees full paths
        # (e.g. "session://report.pdf" not just "report.pdf").
        # Skip if already prefixed.
        if "://" not in filename:
            filename = self._vfs.make_uri(self._root, filename)
        entry = super().register(filename, blob, mime, source)
        # Create parent directories if path has components
        uri = filename
        rel = uri.split("://", 1)[1] if "://" in uri else uri
        if "/" in rel:
            parent = uri.rsplit("/", 1)[0]
            try:
                self._vfs.mkdir(parent)
            except Exception:
                pass  # already exists or memory fs ignores
        self._vfs.put(uri, blob)
        return entry

    def get(self, filename: str) -> FileEntry | None:
        """Look up file by name, path, or VFS URI.

        Lookup chain:
        1. In-memory registry (exact + fuzzy suffix match)
        2. Full VFS URI (e.g., 'local://Downloads/test.png')
        3. session:// root (e.g., 'test.png' -> 'session://test.png')
        4. All mounted VFS roots with path (e.g., 'Downloads/test.png'
           -> 'local://Downloads/test.png')

        VFS files are NOT cached in memory - always read fresh so edits
        are picked up. Only upload/tool-produced files are cached.
        """
        import mimetypes as _mt

        entry = super().get(filename)
        if entry:
            return entry

        def _make_entry(blob: bytes, display_name: str, source: str) -> FileEntry:
            mime, _ = _mt.guess_type(display_name)
            return FileEntry(
                filename=display_name,
                blob=blob,
                mime=mime or "application/octet-stream",
                size=len(blob),
                source=source,
            )

        # Full VFS URI: local://Downloads/test.png
        if "://" in filename and not filename.startswith("data:"):
            try:
                blob = self._vfs.cat(filename)
                if blob:
                    display = filename.rsplit("/", 1)[-1]
                    return _make_entry(blob, display, f"vfs:{filename}")
            except Exception:
                pass

        # session:// root
        try:
            uri = self._vfs.make_uri(self._root, filename)
            blob = self._vfs.cat(uri)
            if blob:
                display = filename.rsplit("/", 1)[-1]
                return _make_entry(blob, display, "vfs:session")
        except Exception:
            pass

        # All other mounted roots
        for root_name in self._vfs.roots:
            if root_name == self._root:
                continue
            try:
                uri = self._vfs.make_uri(root_name, filename)
                blob = self._vfs.cat(uri)
                if blob:
                    display = filename.rsplit("/", 1)[-1]
                    return _make_entry(blob, display, f"vfs:{root_name}")
            except Exception:
                continue

        return None

    def delete(self, filename: str) -> bool:
        result = super().delete(filename)
        if result:
            uri = self._vfs.make_uri(self._root, filename)
            try:
                self._vfs.rm(uri)
            except Exception:
                pass
        return result
