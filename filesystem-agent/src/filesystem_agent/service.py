"""MCP + A2A service wrapper for filesystem-agent.

Usage:
    uvicorn filesystem_agent.service:app --port 8003
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from agentura_commons import BaseAgentService, SkillDef, ToolDef, create_app

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
            cookies = extract_sharepoint_cookies(settings.sharepoint_site_url)
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

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="list_files",
                description="List files and folders at a VFS URI. Empty URI lists all available roots.",
                fn=self._list_files,
                read_only=True,
                idempotent=True,
            ),
            ToolDef(
                name="file_info",
                description="Get metadata (type, size, name) for a file or folder.",
                fn=self._file_info,
                read_only=True,
                idempotent=True,
            ),
            ToolDef(
                name="read_file",
                description=(
                    "Read the contents of a file. Returns text for text files, base64 for binary. "
                    "Also reads files inside archives using the ! separator, "
                    "e.g. downloads://data.zip!path/to/file.csv"
                ),
                fn=self._read_file,
                read_only=True,
            ),
            ToolDef(
                name="file_tree",
                description="Get a nested directory tree starting at a URI, pre-loaded to a given depth.",
                fn=self._file_tree,
                read_only=True,
                idempotent=True,
            ),
            ToolDef(
                name="write_file",
                description="Write text content to a file. Creates parent directories if needed.",
                fn=self._write_file,
            ),
            ToolDef(
                name="create_folder",
                description="Create a new folder at the given URI.",
                fn=self._create_folder,
            ),
            ToolDef(
                name="move_file",
                description="Move or rename a file/folder. Works across roots.",
                fn=self._move_file,
                destructive=True,
            ),
            ToolDef(
                name="copy_file",
                description=(
                    "Copy a file/folder to a new location. Works across roots. "
                    "Can extract from archives: copy_file(source='root://data.zip!file.txt', destination='session://file.txt')"
                ),
                fn=self._copy_file,
            ),
            ToolDef(
                name="delete_file",
                description="Delete a file or folder (recursive for folders).",
                fn=self._delete_file,
                destructive=True,
            ),
            ToolDef(
                name="list_archive",
                description="List contents of an archive file (zip, tar). Use ! separator for inner paths.",
                fn=self._list_archive,
                read_only=True,
                idempotent=True,
            ),
            ToolDef(
                name="read_archive_file",
                description="Read a file from inside an archive without extracting the whole archive.",
                fn=self._read_archive_file,
                read_only=True,
            ),
            ToolDef(
                name="search_sharepoint",
                description="Search across SharePoint sites using the REST search API.",
                fn=self._search_sharepoint,
                read_only=True,
                idempotent=True,
            ),
            ToolDef(
                name="add_root",
                description=(
                    "Mount a new filesystem root. Supports any fsspec protocol: "
                    "local/file, webdav, sftp, ssh, smb, s3, gcs, az, http, ftp, memory, etc. "
                    "Pass protocol-specific options as JSON kwargs."
                ),
                fn=self._add_root,
            ),
            ToolDef(
                name="remove_root",
                description="Unmount a filesystem root by name.",
                fn=self._remove_root,
                destructive=True,
            ),
            ToolDef(
                name="list_roots",
                description="List all mounted filesystem roots with their protocols.",
                fn=self._list_roots,
                read_only=True,
                idempotent=True,
            ),
        ]

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
            # Build JSON schema from function signature
            import inspect

            sig = inspect.signature(td.fn)
            props: dict[str, Any] = {}
            required = []
            for pname, param in sig.parameters.items():
                if pname == "self":
                    continue
                ptype = "string"
                if param.annotation in (int, "int"):
                    ptype = "integer"
                elif param.annotation in (bool, "bool"):
                    ptype = "boolean"
                props[pname] = {"type": ptype, "description": ""}
                if param.default is inspect.Parameter.empty:
                    required.append(pname)

            tools_schema.append(
                {
                    "type": "function",
                    "function": {
                        "name": td.name,
                        "description": td.description,
                        "parameters": {
                            "type": "object",
                            "properties": props,
                            "required": required,
                        },
                    },
                }
            )
            tool_map[td.name] = td.fn

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
                fn = tool_map.get(tc.function.name)
                if fn:
                    args = json.loads(tc.function.arguments)
                    result = await fn(**args)
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
        return json.dumps(entries, ensure_ascii=False, indent=2)

    async def _file_info(self, uri: str = "") -> str:
        vfs = self._ensure_vfs()
        info = vfs.info(uri)
        return json.dumps(info, ensure_ascii=False, indent=2)

    async def _read_file(self, uri: str = "") -> str:
        if not uri or "://" not in uri:
            return "Error: provide a valid URI (e.g. session://file.txt)"
        vfs = self._ensure_vfs()
        data = vfs.cat(uri)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            import base64

            return f"[binary, {len(data)} bytes, base64]: {base64.b64encode(data).decode()}"

    async def _file_tree(self, uri: str = "", depth: int = 2) -> str:
        vfs = self._ensure_vfs()
        tree = vfs.tree(uri, depth=depth)
        return json.dumps(tree, ensure_ascii=False, indent=2)

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

    async def _search_sharepoint(self, query: str = "", limit: int = 20) -> str:
        """Search SharePoint via REST API."""
        from urllib.parse import quote

        import httpx

        from .auth import CookieAuth, extract_sharepoint_cookies

        settings = self._settings
        if not settings.sharepoint_site_url:
            return json.dumps({"error": "SHAREPOINT_SITE_URL not configured"})

        cookies = extract_sharepoint_cookies(settings.sharepoint_site_url)
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
        kwargs: str = "{}",
    ) -> str:
        """Mount a new filesystem root.

        Args:
            name: Root name used in URIs (e.g. "myserver" for myserver://path).
            protocol: fsspec protocol (local, webdav, sftp, smb, s3, gcs, az, http, ftp, memory, ...).
            base_path: Subdirectory within the filesystem to scope to.
            kwargs: JSON string of protocol-specific options (host, username, password, etc.).
        """
        if not name:
            return json.dumps({"error": "name is required"})
        vfs = self._ensure_vfs()
        opts = json.loads(kwargs) if kwargs and kwargs != "{}" else {}
        vfs.add_root_from_protocol(name, protocol, base_path=base_path, **opts)
        return json.dumps({"mounted": name, "protocol": protocol, "base_path": base_path})

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


# App factory
_service = FilesystemAgentService()


def create_service_app(host: str | None = None, port: str | int | None = None):
    h = host or os.getenv("AGENT_HOST", "127.0.0.1")
    p = port or os.getenv("AGENT_PORT", "8003")
    return create_app(_service, base_url=f"http://{h}:{p}")


app = create_service_app()
