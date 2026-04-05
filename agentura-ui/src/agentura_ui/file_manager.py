"""Sidebar file manager widget.

Shows all files in the FileRegistry (user uploads + tool outputs)
with per-file actions: preview, download, re-use, delete.
"""

from __future__ import annotations

import io
import logging
from typing import Callable

import panel as pn

from .file_registry import FileEntry, FileRegistry, human_size

logger = logging.getLogger(__name__)

# Icons for file sources
_ICON_UPLOAD = "\U0001f4ce"   # paperclip
_ICON_TOOL = "\U0001f4c4"     # page


class FileManager:
    """Sidebar file manager backed by a FileRegistry.

    Parameters
    ----------
    registry:
        Shared FileRegistry instance.
    pending_uploads:
        Mutable list - filenames appended here are prepended
        to the user's next chat message so the LLM knows about
        them.
    on_preview:
        Optional callback(entry) to show a preview (e.g. in
        panelini's preview pane).
    on_chat_notify:
        Optional callback(message) to post a system message
        in the chat interface.
    """

    def __init__(
        self,
        registry: FileRegistry,
        pending_uploads: list[str],
        *,
        on_preview: Callable[[FileEntry], None] | None = None,
        on_chat_notify: Callable[[str], None] | None = None,
    ) -> None:
        self._registry = registry
        self._pending = pending_uploads
        self._on_preview = on_preview
        self._on_chat_notify = on_chat_notify

        self._file_input = pn.widgets.FileInput(
            accept=(
                ".pdf,.docx,.pptx,.png,.jpg,"
                ".jpeg,.html,.svg,.csv,.xlsx"
            ),
            multiple=False,
        )
        self._file_input.param.watch(
            self._on_file_selected, "value",
        )

        self._list_col = pn.Column(sizing_mode="stretch_width")
        self.refresh()

        self.panel = pn.Card(
            self._file_input,
            self._list_col,
            title="Files",
            collapsed=False,
        )

    # Public API

    def refresh(self) -> None:
        """Rebuild the file list from the registry."""
        if not self._registry.files:
            self._list_col.objects = [
                pn.pane.Markdown(
                    "*No files*",
                    styles={"color": "#888"},
                ),
            ]
            return
        self._list_col.objects = [
            self._make_row(e)
            for e in self._registry.files.values()
        ]

    def register_and_refresh(
        self, filename: str, blob: bytes, mime: str,
        source: str,
    ) -> FileEntry:
        """Register a file and refresh the list."""
        entry = self._registry.register(
            filename, blob, mime, source,
        )
        self.refresh()
        return entry

    # Row builder

    def _make_row(self, entry: FileEntry) -> pn.Column:
        icon = (
            _ICON_UPLOAD if entry.source == "upload"
            else _ICON_TOOL
        )
        mime_short = entry.mime.split(";")[0].split("/")[-1]
        label = pn.pane.HTML(
            f"<div title='{entry.filename}' style='"
            f"overflow:hidden;text-overflow:ellipsis;"
            f"white-space:nowrap;font-size:0.85em;'>"
            f"{icon} <b>{entry.filename}</b><br>"
            f"<span style='color:#888;'>"
            f"{human_size(entry.size)} &middot; {mime_short}"
            f"</span></div>",
            sizing_mode="stretch_width",
            margin=(2, 0),
        )

        fn = entry.filename

        preview_btn = pn.widgets.Button(
            name="\U0001f441",  # eye
            button_type="light",
            width=36, height=28,
            description="Preview",
        )
        preview_btn.on_click(
            lambda e, f=fn: self._action_preview(f),
        )

        download_btn = pn.widgets.FileDownload(
            callback=lambda f=fn: io.BytesIO(
                self._registry.get(f).blob,
            ),
            filename=entry.filename,
            label="\u2b07",  # down arrow
            button_type="light",
            width=36, height=28,
        )

        reuse_btn = pn.widgets.Button(
            name="\u21ba",  # cycle arrow
            button_type="light",
            width=36, height=28,
            description="Re-use in chat",
        )
        reuse_btn.on_click(
            lambda e, f=fn: self._action_reuse(f),
        )

        delete_btn = pn.widgets.Button(
            name="\u2715",  # cross
            button_type="danger",
            width=36, height=28,
            description="Delete",
        )
        delete_btn.on_click(
            lambda e, f=fn: self._action_delete(f),
        )

        toolbar = pn.Row(
            preview_btn, download_btn, reuse_btn, delete_btn,
            sizing_mode="stretch_width",
            margin=(2, 0),
        )

        return pn.Column(
            label,
            toolbar,
            sizing_mode="stretch_width",
            styles={
                "border-bottom": "1px solid #eee",
                "padding-bottom": "6px",
                "margin-bottom": "6px",
            },
        )

    # Actions

    def _action_preview(self, filename: str) -> None:
        entry = self._registry.get(filename)
        if entry and self._on_preview:
            self._on_preview(entry)

    def _action_reuse(self, filename: str) -> None:
        """Make the file available for the next tool call."""
        entry = self._registry.get(filename)
        if not entry:
            return
        note = f"{entry.filename} ({human_size(entry.size)})"
        self._pending.append(note)
        if self._on_chat_notify:
            self._on_chat_notify(
                f"File re-added for tool use: **{note}**",
            )
        logger.info("File re-queued: %s", filename)

    def _action_delete(self, filename: str) -> None:
        self._registry.delete(filename)
        logger.info("File deleted: %s", filename)
        self.refresh()

    # Upload handler

    def _on_file_selected(self, event) -> None:
        if event.new is None:
            return
        blob = event.new
        filename = self._file_input.filename or "upload"
        mime = (
            self._file_input.mime_type
            or "application/octet-stream"
        )
        entry = self.register_and_refresh(
            filename, bytes(blob), mime, "upload",
        )
        note = f"{entry.filename} ({human_size(entry.size)})"
        self._pending.append(note)
        logger.info("File uploaded: %s", note)

        if self._on_chat_notify:
            self._on_chat_notify(
                f"File received: **{note}**. "
                f"You can now ask me to process it.",
            )
