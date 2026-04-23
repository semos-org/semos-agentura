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
        entry = super().register(filename, blob, mime, source)
        uri = self._vfs.make_uri(self._root, filename)
        self._vfs.put(uri, blob)
        return entry

    def delete(self, filename: str) -> bool:
        result = super().delete(filename)
        if result:
            uri = self._vfs.make_uri(self._root, filename)
            try:
                self._vfs.rm(uri)
            except Exception:
                pass
        return result
