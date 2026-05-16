"""SharePoint/OneDrive URL resolution and sharing link handling."""

from __future__ import annotations

import logging
import re
from posixpath import dirname
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)


def is_sharing_link(url: str) -> bool:
    """Check if URL is a SharePoint/OneDrive sharing link."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    return len(parts) >= 4 and parts[0].startswith(":") and parts[0].endswith(":")


def is_file_sharing_link(url: str) -> bool:
    """Check if sharing link points to a single file (not a folder)."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    # :f: = folder, :w: = Word, :x: = Excel, :p: = PPT, :o: = other
    return len(parts) >= 4 and parts[0] in (":w:", ":x:", ":p:", ":o:")


def resolve_sharepoint_url(url: str) -> tuple[str, str]:
    """Resolve a SharePoint/OneDrive URL to (site_url, subfolder).

    Handles team sites (/sites/X), personal sites (/personal/X),
    and OneDrive sharing links (/:f:/g/personal/X/hash).
    """
    parsed = urlparse(url.rstrip("/"))
    parts = [p for p in parsed.path.split("/") if p]

    # Sharing link: /:f:/g/personal/username/hash or /:x:/g/personal/...
    if len(parts) >= 4 and parts[0].startswith(":") and parts[0].endswith(":"):
        scope = parts[2]  # "personal" or "sites"
        if scope in ("personal", "sites"):
            site_url = f"{parsed.scheme}://{parsed.netloc}/{scope}/{parts[3]}"
            return site_url, ""

    # Direct personal site URL: /personal/username[/...]
    if "personal" in parts:
        idx = parts.index("personal")
        if idx + 1 < len(parts):
            site_url = f"{parsed.scheme}://{parsed.netloc}/personal/{parts[idx + 1]}"
            return site_url, ""

    # Team site URL: /sites/SiteName[/...]
    if "sites" in parts:
        idx = parts.index("sites")
        if idx + 1 < len(parts):
            site_url = f"{parsed.scheme}://{parsed.netloc}/sites/{parts[idx + 1]}"
            return site_url, ""

    return url.rstrip("/"), ""


def extract_shared_folder(redirect_url: str, site_url: str) -> str:
    """Extract the shared folder path from a SharePoint redirect URL.

    After navigating to a sharing link, SharePoint redirects to a URL like:
    .../onedrive.aspx?id=/personal/user/Documents/Folder&ga=1
    This extracts the subfolder relative to the doc library.
    For single-file links, returns the parent folder so the file is accessible.
    """
    parsed = urlparse(redirect_url)
    qs = parse_qs(parsed.query)
    server_path = qs.get("id", [""])[0]
    if not server_path:
        return ""

    # server_path is like /personal/user/Documents/Folder/Sub
    # We need to strip the site path and doc library prefix
    site_path = urlparse(site_url).path.rstrip("/")
    if server_path.startswith(site_path):
        # Strip site path -> /Documents/Folder/Sub
        remainder = server_path[len(site_path) :].strip("/")
        # Strip doc library name (first segment) -> Folder/Sub
        parts = remainder.split("/", 1)
        if len(parts) > 1:
            rel_path = parts[1]
            # Single-file link: use parent folder so file is browsable
            if "." in rel_path.rsplit("/", 1)[-1]:
                rel_path = dirname(rel_path)
            return rel_path
    return ""


def resolve_sharing_link_folder(sharing_url: str, site_url: str, auth: Any) -> str:
    """Resolve a sharing link to the shared folder path.

    Strategy 1: Follow the redirect - folder links (/:f:/) redirect to
    onedrive.aspx?id=/path, even if the HTML page returns 403.
    Strategy 2: Parse HTML metadata - file links (/:w:/, /:x:/) open an
    editor and don't redirect, so we parse the page for file paths.
    """
    import httpx

    try:
        # Try redirect first (works for folder links)
        r = httpx.get(sharing_url, auth=auth, follow_redirects=True, timeout=15)
        final_url = str(r.url)
        logger.info("Sharing link resolved to: %s", final_url)
        folder = extract_shared_folder(final_url, site_url)
        if folder:
            logger.info("Extracted shared folder: %s", folder)
            return folder

        # Redirect didn't contain ?id= (e.g. :w: opens Word editor).
        # Try parsing the HTML response for the file's server-relative path.
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("text/html"):
            folder = _resolve_via_html_metadata(r.text, site_url)
        if folder:
            return folder
    except Exception:
        logger.debug("Failed to resolve sharing link folder", exc_info=True)
    return ""


def _resolve_via_html_metadata(html: str, site_url: str) -> str:
    """Extract file path from a SharePoint/Word Online HTML response.

    The editor page embeds the file's server-relative URL in various places
    (e.g. WopiSrc parameter, data attributes, or script config).
    """
    site_path = urlparse(site_url).path.rstrip("/")

    # Look for server-relative paths in the HTML
    # Pattern: the site path followed by /Documents/... in a quoted string
    pattern = re.escape(site_path) + r"/Documents/[^\"'&<>\\]+"
    matches = re.findall(pattern, html)
    if not matches:
        return ""

    # Pick the longest match (most specific path)
    server_path = unquote(max(matches, key=len))

    # Strip site path and doc library
    if server_path.startswith(site_path):
        remainder = server_path[len(site_path) :].strip("/")
        parts = remainder.split("/", 1)
        if len(parts) > 1:
            rel_path = parts[1]
            # For files, return parent folder
            if "." in rel_path.rsplit("/", 1)[-1]:
                rel_path = dirname(rel_path)
            if rel_path:
                logger.info("HTML metadata resolved to: %s", rel_path)
                return rel_path
    return ""


def detect_doc_library(site_url: str, auth: Any) -> str:
    """Auto-detect the primary document library name from SharePoint REST API.

    Queries for document libraries (BaseTemplate=101) and returns the
    server-relative folder name of the first one (usually the main library).
    Falls back to 'Shared Documents' (or 'Documents' for OneDrive personal sites).
    """
    import httpx

    try:
        r = httpx.get(
            f"{site_url}/_api/web/lists"
            "?$filter=BaseTemplate eq 101"
            "&$select=Title,RootFolder/ServerRelativeUrl"
            "&$expand=RootFolder",
            auth=auth,
            headers={"Accept": "application/json;odata=verbose"},
            follow_redirects=True,
            timeout=10,
        )
        if r.status_code == 200:
            libs = r.json().get("d", {}).get("results", [])
            if libs:
                rel_url = libs[0]["RootFolder"]["ServerRelativeUrl"]
                return rel_url.rsplit("/", 1)[-1]
    except Exception:
        logger.debug("Doc library detection failed for %s", site_url, exc_info=True)

    # OneDrive personal sites use "Documents", team sites use "Shared Documents"
    if "/personal/" in site_url:
        return "Documents"
    return "Shared Documents"
