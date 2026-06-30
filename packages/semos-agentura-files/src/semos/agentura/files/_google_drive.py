"""Google Drive URL parsing and sharing link resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Google Drive/Docs sharing link patterns
_FOLDER_RE = re.compile(r"/drive/(?:u/\d+/)?folders/([a-zA-Z0-9_-]+)")
_FILE_RE = re.compile(r"/file/d/([a-zA-Z0-9_-]+)")
_DOC_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")
_SHEET_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_SLIDES_RE = re.compile(r"/presentation/d/([a-zA-Z0-9_-]+)")

# Google Workspace MIME types for export
# Google Workspace MIME types and their export formats
# Key = suffix after "application/vnd.google-apps." in the MIME type
EXPORT_MIMES = {
    "document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "drawing": (
        "image/svg+xml",
        ".svg",
    ),
}


@dataclass
class GDriveTarget:
    """Parsed result of a Google Drive sharing link."""

    item_id: str = ""
    item_type: str = ""  # folder, file, document, spreadsheet, presentation
    is_google_doc: bool = False


def is_google_drive_url(url: str) -> bool:
    """Check if a URL is a Google Drive or Google Docs sharing link."""
    return bool(re.match(r"^https://(drive|docs)\.google\.com/", url))


def resolve_google_drive_url(url: str) -> GDriveTarget:
    """Parse a Google Drive sharing link and extract item ID and type.

    Supports:
    - Folder: https://drive.google.com/drive/folders/FOLDER_ID?usp=sharing
    - File: https://drive.google.com/file/d/FILE_ID/view?usp=sharing
    - Doc: https://docs.google.com/document/d/DOC_ID/edit?usp=sharing
    - Sheet: https://docs.google.com/spreadsheets/d/SHEET_ID/edit
    - Slides: https://docs.google.com/presentation/d/SLIDES_ID/edit
    """
    # Special URLs for Drive roots
    if "/drive/my-drive" in url or "/drive/u/" in url and "/my-drive" in url:
        return GDriveTarget(item_id="root", item_type="folder")
    if "/drive/shared-with-me" in url:
        return GDriveTarget(item_id="sharedWithMe", item_type="shared")

    for pattern, item_type, is_doc in [
        (_FOLDER_RE, "folder", False),
        (_FILE_RE, "file", False),
        (_DOC_RE, "document", True),
        (_SHEET_RE, "spreadsheet", True),
        (_SLIDES_RE, "presentation", True),
    ]:
        m = pattern.search(url)
        if m:
            return GDriveTarget(
                item_id=m.group(1),
                item_type=item_type,
                is_google_doc=is_doc,
            )
    return GDriveTarget()
