"""SharePoint Online file access via WebDAV/fsspec."""

from .auth import BearerAuth, CookieAuth, acquire_sharepoint_token, extract_sharepoint_cookies
from .config import Settings
from .vfs import Root, VirtualFileSystem

__all__ = [
    "BearerAuth",
    "CookieAuth",
    "Root",
    "Settings",
    "VirtualFileSystem",
    "acquire_sharepoint_token",
    "extract_sharepoint_cookies",
]
