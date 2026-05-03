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


# Protocol-specific option schemas for add_root.
# Each entry in oneOf describes one fsspec protocol and its kwargs.
# Only protocols whose dependencies are installed are listed.
# Installed: fsspec (local, memory, http, ftp), webdav4[fsspec] (webdav).
# Optional extras (need separate pip install): sftp (paramiko), smb (smbprotocol),
# s3 (s3fs), gcs (gcsfs), az (adlfs).
_PROTOCOL_SCHEMAS = [
    {
        "type": "object",
        "title": "local",
        "description": "Local filesystem",
        "properties": {
            "protocol": {"const": "local"},
        },
        "required": ["protocol"],
    },
    {
        "type": "object",
        "title": "memory",
        "description": "In-memory filesystem (ephemeral, lost on restart)",
        "properties": {
            "protocol": {"const": "memory"},
        },
        "required": ["protocol"],
    },
    {
        "type": "object",
        "title": "webdav",
        "description": "WebDAV server (SharePoint, Nextcloud, etc.)",
        "properties": {
            "protocol": {"const": "webdav"},
            "kwargs": {
                "type": "object",
                "properties": {
                    "base_url": {"type": "string", "description": "WebDAV endpoint URL"},
                    "auth": {
                        "type": "object",
                        "description": "Auth object (CookieAuth or BearerAuth)",
                        "properties": {
                            "type": {"type": "string", "enum": ["cookie", "bearer"]},
                            "token": {"type": "string", "description": "Token or cookie value"},
                        },
                        "required": ["type", "token"],
                    },
                },
                "required": ["base_url"],
            },
        },
        "required": ["protocol", "kwargs"],
    },
    {
        "type": "object",
        "title": "sharepoint",
        "description": (
            "SharePoint Online. Connects via WebDAV with automatic cookie-based auth "
            "(opens browser for smartcard/SSO if needed, caches session for reuse). "
            "All connection details go in kwargs  - do NOT use base_path for this protocol."
        ),
        "properties": {
            "protocol": {"const": "sharepoint"},
            "kwargs": {
                "type": "object",
                "description": "SharePoint connection details. Only site_url is required; doc_library and subfolder have sensible defaults.",
                "properties": {
                    "site_url": {
                        "type": "string",
                        "description": (
                            "SharePoint SITE URL only  - must end at the site name, "
                            "do NOT append the document library path. "
                            "Correct: https://contoso.sharepoint.com/sites/MySite  "
                            "Wrong:   https://contoso.sharepoint.com/sites/MySite/Shared%20Documents"
                        ),
                        "pattern": r"^https://[^/]+/sites/[^/]+$",
                    },
                    "doc_library": {
                        "type": "string",
                        "description": (
                            "Document library URL folder name. Auto-detected from SharePoint if omitted. "
                            "Only set this if the site has multiple document libraries and you want a specific one. "
                            "Example: 'Freigegebene Dokumente' or 'Shared Documents'."
                        ),
                    },
                    "subfolder": {
                        "type": "string",
                        "description": (
                            "Subfolder path within the document library to scope to. "
                            "Example: 'General' or 'Projects/2025'. Empty string mounts the library root."
                        ),
                        "default": "",
                    },
                },
                "required": ["site_url"],
            },
        },
        "required": ["protocol", "kwargs"],
    },
    {
        "type": "object",
        "title": "http",
        "description": "Read-only HTTP/HTTPS file access",
        "properties": {
            "protocol": {"const": "http"},
            "kwargs": {
                "type": "object",
                "properties": {
                    "headers": {
                        "type": "object",
                        "description": "Extra HTTP headers",
                        "additionalProperties": {"type": "string"},
                    },
                },
            },
        },
        "required": ["protocol"],
    },
    {
        "type": "object",
        "title": "ftp",
        "description": "FTP file access",
        "properties": {
            "protocol": {"const": "ftp"},
            "kwargs": {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer", "default": 21},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                },
                "required": ["host"],
            },
        },
        "required": ["protocol", "kwargs"],
    },
]


# Optional protocols  - only included if their extra dependency is installed.
def _optional_schema(title: str, description: str, check_import: str, kwargs_schema: dict) -> dict | None:
    try:
        __import__(check_import)
    except ImportError:
        return None
    return {
        "type": "object",
        "title": title,
        "description": description,
        "properties": {
            "protocol": {"const": title},
            "kwargs": kwargs_schema,
        },
        "required": ["protocol", "kwargs"],
    }


for _s in [
    _optional_schema(
        "sftp",
        "SFTP/SSH file access (requires paramiko)",
        "paramiko",
        {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "SSH hostname"},
                "port": {"type": "integer", "description": "SSH port", "default": 22},
                "username": {"type": "string"},
                "password": {"type": "string"},
                "key_filename": {"type": "string", "description": "Path to SSH private key"},
            },
            "required": ["host", "username"],
        },
    ),
    _optional_schema(
        "smb",
        "SMB/CIFS network share (requires smbprotocol)",
        "smbprotocol",
        {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Server hostname or IP"},
                "port": {"type": "integer", "default": 445},
                "username": {"type": "string"},
                "password": {"type": "string"},
            },
            "required": ["host"],
        },
    ),
    _optional_schema(
        "s3",
        "Amazon S3 or S3-compatible storage (requires s3fs)",
        "s3fs",
        {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "AWS access key ID"},
                "secret": {"type": "string", "description": "AWS secret access key"},
                "endpoint_url": {"type": "string", "description": "S3-compatible endpoint (MinIO, etc.)"},
                "region_name": {"type": "string", "description": "AWS region"},
                "anon": {"type": "boolean", "description": "Anonymous access (public buckets)", "default": False},
            },
        },
    ),
    _optional_schema(
        "gcs",
        "Google Cloud Storage (requires gcsfs)",
        "gcsfs",
        {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "GCP project ID"},
                "token": {"type": "string", "description": "Path to service account JSON or 'anon'"},
            },
        },
    ),
    _optional_schema(
        "az",
        "Azure Blob Storage (requires adlfs)",
        "adlfs",
        {
            "type": "object",
            "properties": {
                "account_name": {"type": "string", "description": "Azure storage account name"},
                "account_key": {"type": "string", "description": "Azure storage account key"},
                "connection_string": {
                    "type": "string",
                    "description": "Full connection string (alternative to account_name+key)",
                },
                "sas_token": {"type": "string", "description": "Shared Access Signature token"},
            },
            "required": ["account_name"],
        },
    ),
]:
    if _s is not None:
        _PROTOCOL_SCHEMAS.append(_s)

_ADD_ROOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Short identifier for the mount  - becomes the URI scheme. "
                "Example: name='docs' creates URIs like docs://path/to/file"
            ),
        },
        "protocol": {
            "type": "string",
            "description": (
                "Storage backend to use. Each protocol has its own kwargs. "
                "For SharePoint, use 'sharepoint' (not 'webdav')  - it handles auth automatically."
            ),
            "enum": [s["title"] for s in _PROTOCOL_SCHEMAS],
        },
        "base_path": {
            "type": "string",
            "description": (
                "Subdirectory to scope the root to (only for local/webdav/ftp/sftp/smb). "
                "NOT used for 'sharepoint' protocol  - use kwargs.subfolder instead."
            ),
            "default": "",
        },
        "kwargs": {
            "type": "object",
            "description": (
                "Protocol-specific connection options. "
                "Required fields depend on the chosen protocol  - see each protocol's schema."
            ),
            "oneOf": [s["properties"].get("kwargs", {"type": "object"}) for s in _PROTOCOL_SCHEMAS],
        },
    },
    "required": ["name", "protocol"],
    "oneOf": _PROTOCOL_SCHEMAS,
}


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
                name="grep",
                description=(
                    "Search file contents for a regex pattern across VFS. "
                    "Walks the directory tree, reads text files, returns matching lines with URIs. "
                    "Works across all roots including SharePoint and archives. "
                    "For SharePoint keyword queries, prefer search_sharepoint (server-side, indexed)."
                ),
                fn=self._grep,
                read_only=True,
            ),
            ToolDef(
                name="glob",
                description=(
                    "Find files by name pattern (e.g. '*.pdf', 'report_*.xlsx') across VFS. "
                    "Walks the directory tree and matches filenames using fnmatch patterns."
                ),
                fn=self._glob,
                read_only=True,
                idempotent=True,
            ),
            ToolDef(
                name="edit_file",
                description=(
                    "Edit a text file by replacing a specific string. "
                    "Reads the file, replaces old_text with new_text, writes back. "
                    "Fails if old_text is not found or is ambiguous (multiple occurrences)."
                ),
                fn=self._edit_file,
            ),
            ToolDef(
                name="batch_edit",
                description=(
                    "Apply multiple search/replace edits to a file in a single operation. "
                    "More efficient than multiple edit_file calls  - reduces round trips. "
                    "Edits are applied sequentially (each sees the result of prior edits)."
                ),
                fn=self._batch_edit,
                parameters={
                    "type": "object",
                    "properties": {
                        "uri": {"type": "string", "description": "VFS URI of the file to edit"},
                        "edits": {
                            "type": "array",
                            "description": "List of replacements to apply sequentially",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old": {"type": "string", "description": "Text to find"},
                                    "new": {"type": "string", "description": "Replacement text"},
                                },
                                "required": ["old", "new"],
                            },
                        },
                    },
                    "required": ["uri", "edits"],
                },
            ),
            ToolDef(
                name="add_root",
                description=(
                    "Mount a new filesystem root that becomes accessible via VFS URIs (name://path). "
                    "For SharePoint use protocol='sharepoint' with kwargs.site_url  - "
                    "auth is handled automatically. "
                    "For local dirs use protocol='local' with base_path. "
                    "For other backends see the protocol-specific kwargs schemas."
                ),
                fn=self._add_root,
                parameters=_ADD_ROOT_SCHEMA,
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
        kwargs: dict | None = None,
    ) -> str:
        """Mount a new filesystem root."""
        if not name:
            return json.dumps({"error": "name is required"})
        vfs = self._ensure_vfs()
        opts = kwargs or {}

        if protocol == "sharepoint":
            return await self._add_sharepoint_root(name, base_path, opts)

        vfs.add_root_from_protocol(name, protocol, base_path=base_path, **opts)
        return json.dumps({"mounted": name, "protocol": protocol, "base_path": base_path})

    async def _add_sharepoint_root(self, name: str, base_path: str, opts: dict) -> str:
        """Mount a SharePoint root via WebDAV with automatic cookie auth."""
        from urllib.parse import quote, urlparse

        from .auth import CookieAuth, extract_sharepoint_cookies

        site_url = opts.get("site_url", "")
        if not site_url:
            return json.dumps({"error": "kwargs.site_url is required for sharepoint protocol"})

        # Normalize: strip trailing slash and any doc library path the LLM may have appended
        parsed = urlparse(site_url.rstrip("/"))
        parts = parsed.path.split("/")
        if "/sites/" in parsed.path:
            idx = parts.index("sites")
            site_url = f"{parsed.scheme}://{parsed.netloc}{'/'.join(parts[: idx + 2])}"

        cookies = extract_sharepoint_cookies(site_url)
        if not cookies.get("FedAuth"):
            return json.dumps({"error": "SharePoint login failed  - no FedAuth cookie obtained"})

        auth = CookieAuth(cookies)

        # Auto-detect primary document library if not specified.
        # SharePoint's localized URL name (e.g. "Freigegebene Dokumente") differs
        # from the English "Shared Documents"  - query the API to get the real path.
        doc_library = opts.get("doc_library", "")
        if not doc_library:
            doc_library = self._detect_doc_library(site_url, auth)

        subfolder = opts.get("subfolder", "")

        webdav_url = f"{site_url}/{quote(doc_library)}"
        # DirFileSystem needs a non-empty base_path for WebDAV  - "/" is the minimum
        folder_path = quote(subfolder) if subfolder else "/"

        from webdav4.fsspec import WebdavFileSystem

        wfs = WebdavFileSystem(webdav_url, auth=auth)
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

    @staticmethod
    def _detect_doc_library(site_url: str, auth: Any) -> str:
        """Auto-detect the primary document library name from SharePoint REST API.

        Queries for document libraries (BaseTemplate=101) and returns the
        server-relative folder name of the first one (usually the main library).
        Falls back to 'Shared Documents' if detection fails.
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
                    # Use the server-relative URL's last segment as the library name
                    rel_url = libs[0]["RootFolder"]["ServerRelativeUrl"]
                    return rel_url.rsplit("/", 1)[-1]
        except Exception:
            pass
        return "Shared Documents"

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
