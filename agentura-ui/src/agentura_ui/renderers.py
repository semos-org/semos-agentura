"""File preview and download widgets for chat messages.

Renders FileEntry objects as appropriate Panel widgets based on MIME type,
per the rendering table in docs/file-handling-spec.md.

Also resolves inline file references in LLM markdown responses:
  ![alt](filename.png)  =>  ![alt](data:image/png;base64,...)
  [text](filename.pdf)  =>  [text](data:application/pdf;base64,...)
"""

from __future__ import annotations

import base64
import io
import re

import panel as pn

from .file_registry import FileEntry, FileRegistry, human_size


def render_file_entry(entry: FileEntry) -> pn.viewable.Viewable:
    """Render a FileEntry as an appropriate Panel widget."""
    mime = entry.mime.lower().split(";")[0].strip()

    if mime.startswith("image/"):
        b64 = base64.b64encode(entry.blob).decode()
        return pn.pane.HTML(
            f'<img src="data:{mime};base64,{b64}" '
            f'alt="{entry.filename}" '
            f'style="max-height:500px;max-width:100%;'
            f'width:auto;height:auto;">',
        )

    if mime == "text/html":
        html = entry.blob.decode("utf-8", errors="replace")
        # Sandboxed iframe for safety
        escaped = html.replace('"', "&quot;")
        return pn.pane.HTML(
            f'<iframe srcdoc="{escaped}" '
            f'sandbox="allow-same-origin" '
            f'style="width:100%;height:400px;border:1px solid #ccc;"></iframe>',
            sizing_mode="stretch_width",
        )

    # Default: download button
    return _download_button(entry)


def render_file_notification(
    entry: FileEntry,
    on_preview=None,
) -> pn.viewable.Viewable:
    """Compact single-row file notification for chat.

    Shows: File created: **name** (size) [Preview] [Download]
    No inline image/HTML preview - user clicks Preview to see
    it in the sidebar preview pane.
    """
    label = pn.pane.Markdown(
        f"File created: **{entry.filename}** "
        f"({human_size(entry.size)})",
        margin=(5, 5, 5, 0),
        sizing_mode="stretch_width",
    )

    buttons = []
    if on_preview:
        preview_btn = pn.widgets.Button(
            name="Preview",
            button_type="light",
            width=70, height=28,
        )
        preview_btn.on_click(
            lambda e, ent=entry: on_preview(ent),
        )
        buttons.append(preview_btn)

    download_btn = pn.widgets.FileDownload(
        callback=lambda ent=entry: io.BytesIO(ent.blob),
        filename=entry.filename,
        label="Download",
        button_type="light",
        width=80, height=28,
    )
    buttons.append(download_btn)

    return pn.Row(
        label, *buttons,
        sizing_mode="stretch_width",
    )


def _download_button(entry: FileEntry) -> pn.widgets.FileDownload:
    """Create a download button for a file."""
    return pn.widgets.FileDownload(
        callback=lambda: io.BytesIO(entry.blob),
        filename=entry.filename,
        label=f"Download {entry.filename} ({human_size(entry.size)})",
        button_type="primary",
    )


# Matches markdown image ![alt](filename) and link [text](filename)
_MD_REF_RE = re.compile(
    r"(!?\[(?P<alt>[^\]]*)\])\((?P<ref>[^)]+)\)",
)


def resolve_file_references(
    text: str,
    registry: FileRegistry,
) -> str:
    """Replace markdown file references with data URIs.

    Scans for ![alt](filename) and [text](filename) patterns.
    If filename exists in the registry, replaces with an inline
    base64 data URI so images render and files are downloadable.

    References that are already URLs (http://) or data URIs are
    left untouched.
    """

    def _replacer(match: re.Match) -> str:
        bracket = match.group(1)  # ![alt] or [text]
        ref = match.group("ref")

        # Skip URLs and data URIs
        if ref.startswith(("http://", "https://", "data:")):
            return match.group(0)

        entry = registry.get(ref)
        if entry is None:
            return match.group(0)

        b64 = base64.b64encode(entry.blob).decode()
        data_uri = f"data:{entry.mime};base64,{b64}"
        alt = match.group("alt") or entry.filename

        # For images: constrain height for chat readability
        if bracket.startswith("!"):
            return (
                f'<img src="{data_uri}" alt="{alt}" '
                f'style="max-height:500px;max-width:100%;'
                f'width:auto;height:auto;">'
            )
        return f"{bracket}({data_uri})"

    return _MD_REF_RE.sub(_replacer, text)
