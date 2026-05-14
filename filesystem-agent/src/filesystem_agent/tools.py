"""AgentTool definitions for filesystem-agent.

Each tool has a Pydantic input model for validation + schema generation,
and an AgentTool subclass with the async implementation.
"""

from __future__ import annotations

from typing import Any

from agentura_commons import AgentTool
from pydantic import BaseModel, Field

from ._schemas import ADD_ROOT_SCHEMA

# Input models


class ListFilesInput(BaseModel):
    uri: str = Field(
        default="",
        description="VFS URI to list. Empty string lists all available roots.",
    )


class FileInfoInput(BaseModel):
    uri: str = Field(
        default="",
        description="VFS URI of the file or folder to inspect.",
    )


class ReadFileInput(BaseModel):
    uri: str = Field(
        default="",
        description=(
            "VFS URI of the file to read. "
            "Also reads files inside archives using the ! separator, "
            "e.g. downloads://data.zip!path/to/file.csv"
        ),
    )


class FileTreeInput(BaseModel):
    uri: str = Field(
        default="",
        description="VFS URI to start the tree from.",
    )
    depth: int = Field(
        default=2,
        description="Maximum depth to traverse.",
    )


class WriteFileInput(BaseModel):
    uri: str = Field(
        default="",
        description="VFS URI to write to. Creates parent directories if needed.",
    )
    content: str = Field(
        default="",
        description="Text content to write.",
    )


class CreateFolderInput(BaseModel):
    uri: str = Field(
        default="",
        description="VFS URI of the folder to create.",
    )


class MoveFileInput(BaseModel):
    source: str = Field(
        default="",
        description="VFS URI of the file/folder to move.",
    )
    destination: str = Field(
        default="",
        description="VFS URI of the destination.",
    )


class CopyFileInput(BaseModel):
    source: str = Field(
        default="",
        description=("VFS URI of the file/folder to copy. Can extract from archives: root://data.zip!file.txt"),
    )
    destination: str = Field(
        default="",
        description="VFS URI of the destination.",
    )


class DeleteFileInput(BaseModel):
    uri: str = Field(
        default="",
        description="VFS URI of the file or folder to delete.",
    )
    recursive: bool = Field(
        default=False,
        description="Delete folders recursively.",
    )


class ListArchiveInput(BaseModel):
    uri: str = Field(
        default="",
        description="VFS URI of the archive. Use ! separator for inner paths.",
    )


class ReadArchiveFileInput(BaseModel):
    uri: str = Field(
        default="",
        description="VFS URI of the file inside the archive.",
    )


class SearchSharepointInput(BaseModel):
    query: str = Field(
        default="",
        description="Search query for SharePoint REST API.",
    )
    limit: int = Field(
        default=20,
        description="Maximum number of results.",
    )


class GrepInput(BaseModel):
    pattern: str = Field(
        description="Regex pattern to search for in file contents.",
    )
    uri: str = Field(
        default="",
        description="VFS URI to search under.",
    )
    depth: int = Field(
        default=3,
        description="Maximum directory depth to traverse.",
    )
    max_results: int = Field(
        default=100,
        description="Maximum number of matching lines to return.",
    )


class GlobInput(BaseModel):
    pattern: str = Field(
        description="Filename pattern (e.g. '*.pdf', 'report_*.xlsx').",
    )
    uri: str = Field(
        default="",
        description="VFS URI to search under.",
    )
    depth: int = Field(
        default=5,
        description="Maximum directory depth to traverse.",
    )
    max_results: int = Field(
        default=500,
        description="Maximum number of matching entries to return.",
    )


class EditFileInput(BaseModel):
    uri: str = Field(
        description="VFS URI of the file to edit.",
    )
    old_text: str = Field(
        description="Text to find in the file.",
    )
    new_text: str = Field(
        description="Replacement text.",
    )
    replace_all: bool = Field(
        default=False,
        description="Replace all occurrences (default: fail if ambiguous).",
    )


class EditItem(BaseModel):
    old: str = Field(description="Text to find.")
    new: str = Field(description="Replacement text.")


class BatchEditInput(BaseModel):
    uri: str = Field(
        description="VFS URI of the file to edit.",
    )
    edits: list[EditItem] = Field(
        description="List of replacements to apply sequentially.",
    )


class AddRootInput(BaseModel):
    name: str = Field(
        default="",
        description=(
            "Short identifier for the mount - becomes the URI scheme. "
            "Example: name='docs' creates URIs like docs://path/to/file"
        ),
    )
    protocol: str = Field(
        default="local",
        description="Storage backend to use.",
    )
    base_path: str = Field(
        default="",
        description="Subdirectory to scope the root to.",
    )
    kwargs: dict | None = Field(
        default=None,
        description="Protocol-specific connection options.",
    )


class RemoveRootInput(BaseModel):
    name: str = Field(
        default="",
        description="Name of the root to unmount.",
    )


class ListRootsInput(BaseModel):
    pass


# Tool implementations


class ListFilesTool(AgentTool):
    name: str = "list_files"
    description: str = "List files and folders at a VFS URI. Empty URI lists all available roots."
    args_schema: type[BaseModel] = ListFilesInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._list_files(**kwargs)


class FileInfoTool(AgentTool):
    name: str = "file_info"
    description: str = "Get metadata (type, size, name) for a file or folder."
    args_schema: type[BaseModel] = FileInfoInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._file_info(**kwargs)


class ReadFileTool(AgentTool):
    name: str = "read_file"
    description: str = (
        "Read the contents of a file. Returns text for text files, base64 for binary. "
        "Also reads files inside archives using the ! separator, "
        "e.g. downloads://data.zip!path/to/file.csv"
    )
    args_schema: type[BaseModel] = ReadFileInput
    read_only: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._read_file(**kwargs)


class FileTreeTool(AgentTool):
    name: str = "file_tree"
    description: str = "Get a nested directory tree starting at a URI, pre-loaded to a given depth."
    args_schema: type[BaseModel] = FileTreeInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._file_tree(**kwargs)


class WriteFileTool(AgentTool):
    name: str = "write_file"
    description: str = "Write text content to a file. Creates parent directories if needed."
    args_schema: type[BaseModel] = WriteFileInput

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._write_file(**kwargs)


class CreateFolderTool(AgentTool):
    name: str = "create_folder"
    description: str = "Create a new folder at the given URI."
    args_schema: type[BaseModel] = CreateFolderInput

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._create_folder(**kwargs)


class MoveFileTool(AgentTool):
    name: str = "move_file"
    description: str = "Move or rename a file/folder. Works across roots."
    args_schema: type[BaseModel] = MoveFileInput
    destructive: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._move_file(**kwargs)


class CopyFileTool(AgentTool):
    name: str = "copy_file"
    description: str = (
        "Copy a file/folder to a new location. Works across roots. "
        "Can extract from archives: copy_file(source='root://data.zip!file.txt', destination='session://file.txt')"
    )
    args_schema: type[BaseModel] = CopyFileInput

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._copy_file(**kwargs)


class DeleteFileTool(AgentTool):
    name: str = "delete_file"
    description: str = "Delete a file or folder (recursive for folders)."
    args_schema: type[BaseModel] = DeleteFileInput
    destructive: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._delete_file(**kwargs)


class ListArchiveTool(AgentTool):
    name: str = "list_archive"
    description: str = "List contents of an archive file (zip, tar). Use ! separator for inner paths."
    args_schema: type[BaseModel] = ListArchiveInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._list_archive(**kwargs)


class ReadArchiveFileTool(AgentTool):
    name: str = "read_archive_file"
    description: str = "Read a file from inside an archive without extracting the whole archive."
    args_schema: type[BaseModel] = ReadArchiveFileInput
    read_only: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._read_archive_file(**kwargs)


class SearchSharepointTool(AgentTool):
    name: str = "search_sharepoint"
    description: str = "Search across SharePoint sites using the REST search API."
    args_schema: type[BaseModel] = SearchSharepointInput
    read_only: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._search_sharepoint(**kwargs)


class GrepTool(AgentTool):
    name: str = "grep"
    description: str = (
        "Search file contents for a regex pattern across VFS. "
        "Walks the directory tree, reads text files, returns matching lines with URIs. "
        "Works across all roots including SharePoint and archives. "
        "For SharePoint keyword queries, prefer search_sharepoint (server-side, indexed)."
    )
    args_schema: type[BaseModel] = GrepInput
    read_only: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._grep(**kwargs)


class GlobTool(AgentTool):
    name: str = "glob"
    description: str = (
        "Find files by name pattern (e.g. '*.pdf', 'report_*.xlsx') across VFS. "
        "Walks the directory tree and matches filenames using fnmatch patterns."
    )
    args_schema: type[BaseModel] = GlobInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._glob(**kwargs)


class EditFileTool(AgentTool):
    name: str = "edit_file"
    description: str = (
        "Edit a text file by replacing a specific string. "
        "Reads the file, replaces old_text with new_text, writes back. "
        "Fails if old_text is not found or is ambiguous (multiple occurrences)."
    )
    args_schema: type[BaseModel] = EditFileInput

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._edit_file(**kwargs)


class BatchEditTool(AgentTool):
    name: str = "batch_edit"
    description: str = (
        "Apply multiple search/replace edits to a file in a single operation. "
        "More efficient than multiple edit_file calls - reduces round trips. "
        "Edits are applied sequentially (each sees the result of prior edits)."
    )
    args_schema: type[BaseModel] = BatchEditInput

    async def _arun(self, **kwargs: Any) -> str:
        # Convert Pydantic EditItem models to dicts for the service method
        edits = kwargs.get("edits", [])
        raw_edits = [e.model_dump() if hasattr(e, "model_dump") else e for e in edits]
        return await self._service._batch_edit(uri=kwargs["uri"], edits=raw_edits)


class AddRootTool(AgentTool):
    name: str = "add_root"
    description: str = (
        "Mount a new filesystem root that becomes accessible via VFS URIs (name://path). "
        "For SharePoint use protocol='sharepoint' with kwargs.site_url - "
        "auth is handled automatically. "
        "For local dirs use protocol='local' with base_path. "
        "For other backends see the protocol-specific kwargs schemas."
    )
    args_schema: type[BaseModel] = AddRootInput
    parameters_override: dict[str, Any] | None = ADD_ROOT_SCHEMA

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._add_root(**kwargs)


class RemoveRootTool(AgentTool):
    name: str = "remove_root"
    description: str = "Unmount a filesystem root by name."
    args_schema: type[BaseModel] = RemoveRootInput
    destructive: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._remove_root(**kwargs)


class ListRootsTool(AgentTool):
    name: str = "list_roots"
    description: str = "List all mounted filesystem roots with their protocols."
    args_schema: type[BaseModel] = ListRootsInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._list_roots()


def get_filesystem_tools(service: Any) -> list[AgentTool]:
    """Create all filesystem-agent tools bound to a service instance."""
    tools = [
        ListFilesTool(),
        FileInfoTool(),
        ReadFileTool(),
        FileTreeTool(),
        WriteFileTool(),
        CreateFolderTool(),
        MoveFileTool(),
        CopyFileTool(),
        DeleteFileTool(),
        ListArchiveTool(),
        ReadArchiveFileTool(),
        SearchSharepointTool(),
        GrepTool(),
        GlobTool(),
        EditFileTool(),
        BatchEditTool(),
        AddRootTool(),
        RemoveRootTool(),
        ListRootsTool(),
    ]
    for t in tools:
        t.bind_service(service)
    return tools
