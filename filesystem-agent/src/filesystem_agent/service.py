"""MCP + A2A service wrapper for filesystem-agent.

Usage:
    uvicorn filesystem_agent.service:app --port 8003
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
from typing import Any

from agentura_commons import BaseAgentService, SkillDef, create_app

from .config import Settings
from .vfs import VirtualFileSystem

logger = logging.getLogger(__name__)


def _build_vfs(settings: Settings) -> VirtualFileSystem:
    """Build the VFS with roots from settings.

    Always creates a session:// in-memory root for scratch files.
    SharePoint is optional.
    """
    vfs = VirtualFileSystem()

    # Always available: in-memory scratch space
    vfs.add_root_from_protocol("session", "memory", base_path="/")
    logger.info("Session root added (in-memory)")

    if settings.sharepoint_site_url:
        try:
            import nest_asyncio

            nest_asyncio.apply()

            from .auth import CookieAuth, extract_sharepoint_cookies

            logger.info("Authenticating to %s ...", settings.sharepoint_site_url)
            cookies, _ = extract_sharepoint_cookies(settings.sharepoint_site_url)
            if cookies.get("FedAuth"):
                from webdav4.fsspec import WebdavFileSystem

                sp_fs = WebdavFileSystem(
                    settings.webdav_base_url,
                    auth=CookieAuth(cookies),
                )
                vfs.add_root("sharepoint", sp_fs, base_path=settings.webdav_folder_path.lstrip("/"))
                logger.info("SharePoint root added: %s", settings.sharepoint_site_url)
            else:
                logger.warning("SharePoint: no FedAuth cookie (login may be needed)")
        except Exception:
            logger.exception("SharePoint root skipped")

    return vfs


class FilesystemAgentService(BaseAgentService):
    def __init__(self, vfs: VirtualFileSystem | None = None) -> None:
        self._settings = Settings()
        self._vfs = vfs

    def _ensure_vfs(self) -> VirtualFileSystem:
        if self._vfs is None:
            self._vfs = _build_vfs(self._settings)
        return self._vfs

    @property
    def agent_name(self) -> str:
        return "Filesystem Agent"

    @property
    def agent_description(self) -> str:
        return "Browse, search, read, write files across local, WebDAV, SharePoint, and remote archives."

    @property
    def agent_version(self) -> str:
        return "0.2.0"

    def get_tools(self) -> list:
        from .tools import get_filesystem_tools

        return get_filesystem_tools(self)

    def get_skills(self) -> list[SkillDef]:
        return [
            SkillDef(
                id="filesystem-operations",
                name="Filesystem Operations",
                description="Browse, search, read, write, copy, move files across local and remote filesystems.",
                tags=["filesystem", "sharepoint", "webdav", "archive"],
                examples=[
                    "List all PDF files in the SharePoint shared folder",
                    "Copy budget.csv from local to webdav shared",
                    "Search SharePoint for documents about mTLS",
                    "Show what's inside the sample.zip archive",
                ],
            ),
        ]

    async def execute_skill(self, skill_id: str, message: str, *, task_id: str | None = None) -> str:
        # If an LLM router is configured, use tool-calling dispatch
        if self.router_llm_model:
            return await self._llm_dispatch(message)

        # Keyword-based fallback
        msg = message.lower()
        if "search" in msg and "sharepoint" in msg:
            return await self._search_sharepoint(query=message)
        elif "list" in msg or "show" in msg or "browse" in msg:
            # Try to extract a URI from the message
            uri = self._extract_uri(message) or ""
            return await self._list_files(uri=uri)
        elif ("read" in msg or "open" in msg) and "://" in message:
            uri = self._extract_uri(message) or ""
            return await self._read_file(uri=uri)
        elif "write" in msg or "save" in msg or "create" in msg:
            return "Use the write_file tool with a URI like session://filename.md and the content."
        elif "tree" in msg:
            uri = self._extract_uri(message) or ""
            return await self._file_tree(uri=uri)
        else:
            return await self._list_files(uri="")

    async def _llm_dispatch(self, message: str) -> str:
        """Use litellm with tool-calling to dispatch natural language requests."""
        import litellm

        tools_schema = []
        tool_map: dict[str, Any] = {}
        for td in self.get_tools():
            schema = td.get_input_schema()
            tools_schema.append(
                {
                    "type": "function",
                    "function": {
                        "name": td.name,
                        "description": td.description,
                        "parameters": schema,
                    },
                }
            )
            tool_map[td.name] = td

        response = await litellm.acompletion(
            model=self.router_llm_model,
            api_key=self.router_llm_api_key or None,
            api_base=self.router_llm_api_base or None,
            messages=[
                {
                    "role": "system",
                    "content": "You are a filesystem agent. Use the available tools to fulfill the user's request.",
                },
                {"role": "user", "content": message},
            ],
            tools=tools_schema,
            tool_choice="auto",
        )

        msg = response.choices[0].message
        if msg.tool_calls:
            results = []
            for tc in msg.tool_calls:
                tool = tool_map.get(tc.function.name)
                if tool:
                    args = json.loads(tc.function.arguments)
                    result = await tool._arun(**args)
                    results.append(f"[{tc.function.name}]: {result}")
            return "\n\n".join(results)

        return msg.content or "No result"

    @staticmethod
    def _extract_uri(message: str) -> str | None:
        """Try to find a VFS URI in a natural language message."""
        for word in message.split():
            if "://" in word:
                return word.strip("\"'`.,;:")
        return None

    # Tool implementations

    async def _list_files(self, uri: str = "") -> str:
        vfs = self._ensure_vfs()
        entries = vfs.ls(uri, detail=True)
        return json.dumps(entries, ensure_ascii=False, indent=2, default=str)

    async def _file_info(self, uri: str = "") -> str:
        vfs = self._ensure_vfs()
        info = vfs.info(uri)
        return json.dumps(info, ensure_ascii=False, indent=2, default=str)

    async def _read_file(self, uri: str = "") -> str:
        if not uri or "://" not in uri:
            return "Error: provide a valid URI (e.g. session://file.txt)"
        vfs = self._ensure_vfs()
        data = vfs.cat(uri)
        try:
            text = data.decode("utf-8")
            # Guard against accidentally reading huge text files
            if len(text) > 500_000:
                return (
                    f"[text file, {len(text)} chars - too large for context. "
                    f"Use grep/glob to search, or pass the filename directly to tools.]"
                )
            return text
        except UnicodeDecodeError:
            import mimetypes

            mime, _ = mimetypes.guess_type(uri)
            return (
                f"[binary file: {uri.rsplit('/', 1)[-1]}, "
                f"{len(data)} bytes, {mime or 'unknown type'}. "
                f"Do NOT read binary files into context. "
                f"Just pass the filename to tools - the file middleware "
                f"resolves content automatically.]"
            )

    async def _file_tree(self, uri: str = "", depth: int = 2) -> str:
        vfs = self._ensure_vfs()
        tree = vfs.tree(uri, depth=depth)
        return json.dumps(tree, ensure_ascii=False, indent=2, default=str)

    async def _write_file(self, uri: str = "", content: str = "") -> str:
        vfs = self._ensure_vfs()
        vfs.put(uri, content.encode("utf-8"))
        return f"Written {len(content)} chars to {uri}"

    async def _create_folder(self, uri: str = "") -> str:
        vfs = self._ensure_vfs()
        vfs.mkdir(uri)
        return f"Created folder {uri}"

    async def _move_file(self, source: str = "", destination: str = "") -> str:
        vfs = self._ensure_vfs()
        vfs.mv(source, destination)
        return f"Moved {source} to {destination}"

    async def _copy_file(self, source: str = "", destination: str = "") -> str:
        vfs = self._ensure_vfs()
        vfs.cp(source, destination)
        return f"Copied {source} to {destination}"

    async def _delete_file(self, uri: str = "", recursive: bool = False) -> str:
        vfs = self._ensure_vfs()
        vfs.rm(uri, recursive=recursive)
        return f"Deleted {uri}"

    async def _list_archive(self, uri: str = "") -> str:
        vfs = self._ensure_vfs()
        entries = vfs.ls_archive(uri)
        return json.dumps(entries, ensure_ascii=False, indent=2)

    async def _read_archive_file(self, uri: str = "") -> str:
        vfs = self._ensure_vfs()
        data = vfs.cat_archive(uri)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            import base64

            return f"[binary, {len(data)} bytes, base64]: {base64.b64encode(data).decode()}"

    async def _grep(self, pattern: str, uri: str = "", depth: int = 3, max_results: int = 100) -> str:
        """Search file contents for a regex pattern across VFS."""
        vfs = self._ensure_vfs()
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            return json.dumps({"error": f"Invalid regex: {e}"})

        tree = vfs.tree(uri, depth=depth)
        file_uris = self._collect_file_uris(tree)

        matches: list[dict] = []
        sem = asyncio.Semaphore(10)

        async def _scan_file(file_uri: str) -> list[dict]:
            async with sem:
                try:
                    data = await asyncio.to_thread(vfs.cat, file_uri)
                    if len(data) > 1_000_000:
                        return []
                    text = data.decode("utf-8")
                except (UnicodeDecodeError, Exception):
                    return []
                hits = []
                for i, line in enumerate(text.splitlines(), 1):
                    if compiled.search(line):
                        hits.append({"uri": file_uri, "line_number": i, "line": line.strip()})
                        if len(hits) >= 10:
                            break
                return hits

        tasks = [_scan_file(u) for u in file_uris]
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, list):
                matches.extend(result)
                if len(matches) >= max_results:
                    matches = matches[:max_results]
                    break

        return json.dumps(matches, ensure_ascii=False, indent=2)

    async def _glob(self, pattern: str, uri: str = "", depth: int = 5, max_results: int = 500) -> str:
        """Find files by name pattern across VFS."""
        vfs = self._ensure_vfs()
        tree = vfs.tree(uri, depth=depth)
        all_entries = self._collect_entries(tree)
        matched = [e for e in all_entries if fnmatch.fnmatch(e.get("name", ""), pattern)][:max_results]
        return json.dumps(matched, ensure_ascii=False, indent=2)

    async def _edit_file(self, uri: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        """Edit a text file by replacing a specific string."""
        vfs = self._ensure_vfs()
        try:
            data = vfs.cat(uri).decode("utf-8")
        except UnicodeDecodeError:
            return json.dumps({"error": "File is binary, cannot edit as text"})
        count = data.count(old_text)
        if count == 0:
            return json.dumps({"error": "old_text not found in file"})
        if count > 1 and not replace_all:
            return json.dumps(
                {"error": f"Ambiguous: old_text occurs {count} times. Use replace_all=true or provide more context."}
            )
        if replace_all:
            new_data = data.replace(old_text, new_text)
        else:
            new_data = data.replace(old_text, new_text, 1)
        vfs.put(uri, new_data.encode("utf-8"))
        return json.dumps({"edited": uri, "replacements": count if replace_all else 1})

    async def _batch_edit(self, uri: str, edits: list[dict] | None = None) -> str:
        """Apply multiple search/replace edits to a file."""
        if not edits:
            return json.dumps({"error": "edits list is required"})
        vfs = self._ensure_vfs()
        try:
            data = vfs.cat(uri).decode("utf-8")
        except UnicodeDecodeError:
            return json.dumps({"error": "File is binary, cannot edit as text"})
        applied = 0
        for i, edit in enumerate(edits):
            old = edit.get("old", "")
            new = edit.get("new", "")
            if not old:
                return json.dumps({"error": f"Edit {i}: 'old' field is empty"})
            if old not in data:
                return json.dumps({"error": f"Edit {i}: '{old[:60]}...' not found in file"})
            data = data.replace(old, new, 1)
            applied += 1
        vfs.put(uri, data.encode("utf-8"))
        return json.dumps({"edited": uri, "edits_applied": applied})

    @staticmethod
    def _collect_file_uris(tree: list[dict]) -> list[str]:
        """Recursively collect file URIs from a tree structure."""
        uris: list[str] = []
        for entry in tree:
            if entry.get("type") == "file":
                uris.append(entry["uri"])
            for child in entry.get("children") or []:
                if child.get("type") == "file":
                    uris.append(child["uri"])
                uris.extend(FilesystemAgentService._collect_file_uris(child.get("children") or []))
        return uris

    @staticmethod
    def _collect_entries(tree: list[dict]) -> list[dict]:
        """Recursively collect all entries from a tree structure."""
        entries: list[dict] = []
        for entry in tree:
            entries.append(
                {
                    "uri": entry.get("uri", ""),
                    "name": entry.get("name", ""),
                    "type": entry.get("type", ""),
                    "size": entry.get("size", ""),
                }
            )
            for child in entry.get("children") or []:
                entries.append(
                    {
                        "uri": child.get("uri", ""),
                        "name": child.get("name", ""),
                        "type": child.get("type", ""),
                        "size": child.get("size", ""),
                    }
                )
                entries.extend(FilesystemAgentService._collect_entries(child.get("children") or []))
        return entries

    async def _search_sharepoint(self, query: str = "", limit: int = 20) -> str:
        """Search SharePoint via REST API."""
        from urllib.parse import quote

        import httpx

        from .auth import CookieAuth, extract_sharepoint_cookies

        settings = self._settings
        if not settings.sharepoint_site_url:
            return json.dumps({"error": "SHAREPOINT_SITE_URL not configured"})

        cookies, _ = extract_sharepoint_cookies(settings.sharepoint_site_url)
        site = settings.sharepoint_site_url
        props = "Title,Path,Filename,Size,LastModifiedTime,Author,FileExtension"
        url = f"{site}/_api/search/query?querytext='{quote(query)}'&rowlimit={limit}&selectproperties='{props}'"

        with httpx.Client(auth=CookieAuth(cookies), timeout=30) as client:
            r = client.get(url, headers={"Accept": "application/json;odata=verbose"}, follow_redirects=True)
            r.raise_for_status()
            data = r.json()

        rows = (
            data.get("d", {})
            .get("query", {})
            .get("PrimaryQueryResult", {})
            .get("RelevantResults", {})
            .get("Table", {})
            .get("Rows", {})
            .get("results", [])
        )
        results = []
        for row in rows:
            cells = {c["Key"]: c["Value"] for c in row.get("Cells", {}).get("results", [])}
            results.append(
                {
                    "title": cells.get("Title", ""),
                    "path": cells.get("Path", ""),
                    "filename": cells.get("Filename", ""),
                    "size": cells.get("Size", ""),
                    "modified": cells.get("LastModifiedTime", ""),
                    "author": cells.get("Author", ""),
                    "extension": cells.get("FileExtension", ""),
                }
            )
        return json.dumps(results, ensure_ascii=False, indent=2)

    async def _add_root(
        self,
        name: str = "",
        protocol: str = "local",
        base_path: str = "",
        kwargs: dict | None = None,
    ) -> str:
        """Mount a new filesystem root."""
        from ._schemas import ADD_ROOT_SCHEMA

        if not name:
            return json.dumps({"error": "name is required"})

        # Validate protocol against oneOf const values
        valid_protocols = [
            s["properties"]["protocol"]["const"]
            for s in ADD_ROOT_SCHEMA.get("oneOf", [])
            if "properties" in s and "protocol" in s["properties"]
        ]
        if valid_protocols and protocol not in valid_protocols:
            return json.dumps({"error": f"Unknown protocol '{protocol}'. Must be one of: {valid_protocols}"})

        vfs = self._ensure_vfs()
        opts = kwargs or {}

        if protocol == "sharepoint":
            return await self._add_sharepoint_root(name, base_path, opts)
        if protocol == "google_drive":
            return await self._add_google_drive_root(name, opts)

        vfs.add_root_from_protocol(name, protocol, base_path=base_path, **opts)
        return json.dumps({"mounted": name, "protocol": protocol, "base_path": base_path})

    async def _add_sharepoint_root(self, name: str, base_path: str, opts: dict) -> str:
        """Mount a SharePoint root via WebDAV with automatic cookie auth."""
        from urllib.parse import quote

        from ._sharepoint import (
            detect_doc_library,
            is_file_sharing_link,
            is_sharing_link,
            resolve_sharepoint_url,
            resolve_sharing_link_file,
            resolve_sharing_link_folder,
        )
        from .auth import CookieAuth, extract_sharepoint_cookies

        raw_url = opts.get("site_url", "")
        if not raw_url:
            return json.dumps({"error": "kwargs.site_url is required for sharepoint protocol"})

        sharing = is_sharing_link(raw_url)
        site_url, resolved_subfolder = resolve_sharepoint_url(raw_url)

        # For sharing links, authenticate via the original URL (triggers email
        # code flow for external users). For direct URLs, use the site URL.
        auth_url = raw_url if sharing else site_url
        cookies, redirect_url = extract_sharepoint_cookies(auth_url)
        if not cookies.get("FedAuth"):
            return json.dumps({"error": "SharePoint login failed - no FedAuth cookie obtained"})

        auth = CookieAuth(cookies)

        # Extract shared folder path from sharing link redirect.
        # Always resolve via HTTP (browser URL may not contain ?id= param).
        if sharing and not opts.get("subfolder", ""):
            resolved_subfolder = resolve_sharing_link_folder(raw_url, site_url, auth)

        # Auto-detect primary document library if not specified.
        # SharePoint's localized URL name (e.g. "Freigegebene Dokumente") differs
        # from the English "Shared Documents" - query the API to get the real path.
        doc_library = opts.get("doc_library", "")
        if not doc_library:
            doc_library = detect_doc_library(site_url, auth)

        subfolder = opts.get("subfolder", "") or resolved_subfolder

        webdav_url = f"{site_url}/{quote(doc_library)}"
        # DirFileSystem base_path is a filesystem path, not a URL - don't encode it.
        # "/" is the minimum non-empty value for WebDAV.
        folder_path = subfolder if subfolder else "/"

        from webdav4.fsspec import WebdavFileSystem

        wfs = WebdavFileSystem(webdav_url, auth=auth)

        # Validate connection before adding to VFS (prevents poisoning other roots)
        try:
            wfs.ls(folder_path if folder_path != "/" else "")
        except Exception as exc:
            # For file sharing links, fall back to single-file virtual mount.
            # File-only shares don't grant folder access via WebDAV.
            if sharing and is_file_sharing_link(raw_url):
                filename = resolve_sharing_link_file(raw_url, site_url, auth)
                if filename:
                    from .vfs import SingleFileWebdavFS

                    file_url = (
                        f"{webdav_url}/{quote(subfolder)}/{quote(filename)}"
                        if subfolder
                        else f"{webdav_url}/{quote(filename)}"
                    )
                    sfs = SingleFileWebdavFS(file_url, filename, auth)
                    # Validate the single file is accessible
                    try:
                        sfs.info(filename)
                    except Exception:
                        return json.dumps({"error": f"Cannot access shared file: {filename}"})
                    vfs = self._ensure_vfs()
                    vfs.add_root(name, sfs)
                    return json.dumps(
                        {
                            "mounted": name,
                            "protocol": "sharepoint",
                            "mode": "single-file",
                            "site_url": site_url,
                            "file": filename,
                        }
                    )
            return json.dumps({"error": f"Cannot access {webdav_url}: {exc}"})

        vfs = self._ensure_vfs()
        vfs.add_root(name, wfs, base_path=folder_path)
        return json.dumps(
            {
                "mounted": name,
                "protocol": "sharepoint",
                "site_url": site_url,
                "doc_library": doc_library,
                "subfolder": subfolder,
            }
        )

    async def _remove_root(self, name: str = "") -> str:
        """Unmount a filesystem root."""
        if not name:
            return json.dumps({"error": "name is required"})
        vfs = self._ensure_vfs()
        vfs.remove_root(name)
        return json.dumps({"unmounted": name})

    async def _list_roots(self) -> str:
        """List all mounted filesystem roots."""
        vfs = self._ensure_vfs()
        return json.dumps(vfs.roots_info(), ensure_ascii=False, indent=2)

    async def _add_google_drive_root(self, name: str, opts: dict) -> str:
        """Mount a Google Drive root via sharing link with browser-based auth."""
        from ._google_drive import EXPORT_MIMES, resolve_google_drive_url
        from .auth import extract_google_drive_auth
        from .vfs import SingleFileGDriveFS

        raw_url = opts.get("share_url", "")
        if not raw_url:
            return json.dumps({"error": "kwargs.share_url is required for google_drive protocol"})

        target = resolve_google_drive_url(raw_url)
        if not target.item_id:
            return json.dumps({"error": f"Could not extract Drive item ID from: {raw_url}"})

        auth = extract_google_drive_auth(raw_url)
        if not auth.token:
            return json.dumps({"error": "Google Drive login failed - no OAuth token obtained"})

        # Resolve item metadata
        import httpx

        # Virtual roots don't have file metadata
        if target.item_id == "root":
            item_name = "My Drive"
        elif target.item_id == "sharedWithMe":
            item_name = "Shared with me"
        else:
            meta_r = httpx.get(
                f"https://www.googleapis.com/drive/v3/files/{target.item_id}",
                auth=auth,
                params={"fields": "name,mimeType,size"},
                follow_redirects=True,
                timeout=15,
            )
            if meta_r.status_code != 200:
                return json.dumps({"error": f"Cannot access Drive item {target.item_id}: {meta_r.status_code}"})
            meta = meta_r.json()
            item_name = meta.get("name", target.item_id)

        if target.item_type in ("folder", "shared"):
            # Validate access
            if target.item_id == "sharedWithMe":
                q = "sharedWithMe=true and trashed=false"
            else:
                q = f"'{target.item_id}' in parents and trashed=false"
            list_r = httpx.get(
                "https://www.googleapis.com/drive/v3/files",
                auth=auth,
                params={
                    "q": q,
                    "fields": "files(id,name,mimeType,size,modifiedTime)",
                    "pageSize": 1,
                },
                follow_redirects=True,
                timeout=15,
            )
            if list_r.status_code != 200:
                return json.dumps({"error": f"Cannot list Drive folder: {list_r.status_code}"})

            from .vfs import GDriveFolderFS

            gfs = GDriveFolderFS(folder_id=target.item_id, auth=auth)
            vfs = self._ensure_vfs()
            vfs.add_root(name, gfs)
            return json.dumps(
                {
                    "mounted": name,
                    "protocol": "google_drive",
                    "folder_name": item_name,
                    "folder_id": target.item_id,
                }
            )

        # Single file or Google Doc
        filename = item_name
        if target.is_google_doc and target.item_type in EXPORT_MIMES:
            _, ext = EXPORT_MIMES[target.item_type]
            if not filename.endswith(ext):
                filename += ext

        sfs = SingleFileGDriveFS(
            file_id=target.item_id,
            filename=filename,
            auth=auth,
            item_type=target.item_type,
        )
        # Validate
        try:
            sfs.info(filename)
        except Exception as exc:
            return json.dumps({"error": f"Cannot access shared file: {exc}"})

        vfs = self._ensure_vfs()
        vfs.add_root(name, sfs)
        return json.dumps(
            {
                "mounted": name,
                "protocol": "google_drive",
                "mode": "export" if target.is_google_doc else "single-file",
                "file_id": target.item_id,
                "file": filename,
            }
        )


# App factory
_service = FilesystemAgentService()


def create_service_app(host: str | None = None, port: str | int | None = None):
    h = host or os.getenv("AGENT_HOST", "127.0.0.1")
    p = port or os.getenv("AGENT_PORT", "8003")
    return create_app(_service, base_url=f"http://{h}:{p}")


app = create_service_app()
