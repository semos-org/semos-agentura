"""Shared utility functions."""

from __future__ import annotations

import base64
import os
import re
import shutil
import tempfile
from pathlib import Path

from .exceptions import ToolNotFoundError


def _project_root() -> Path:
    """Return the project root (where pyproject.toml lives)."""
    here = Path(__file__).resolve().parent
    for parent in (here, here.parent, here.parent.parent, here.parent.parent.parent):
        if (parent / "pyproject.toml").exists():
            return parent
    return here


def find_tool(name: str, env_override: str | None = None) -> Path | None:
    """Find a CLI tool: explicit path > tools/node_modules/.bin/ > system PATH."""
    if env_override:
        p = Path(env_override)
        if p.is_file():
            return p
    # Check local tools/node_modules/.bin/ (prefer .cmd on Windows)
    local_bin = _project_root() / "tools" / "node_modules" / ".bin" / name
    if os.name == "nt":
        candidates = (local_bin.with_suffix(".cmd"), local_bin.with_suffix(".exe"), local_bin)
    else:
        candidates = (local_bin,)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which(name)
    return Path(found) if found else None


def require_tool(name: str, env_override: str | None = None) -> Path:
    """Find a CLI tool or raise ToolNotFoundError."""
    path = find_tool(name, env_override)
    if path is None:
        raise ToolNotFoundError(f"'{name}' not found on PATH. Install it or set the path in settings/environment.")
    return path


def encode_image_base64(image_bytes: bytes, mime_type: str) -> str:
    """Encode image bytes as a data URI string."""
    b64 = base64.b64encode(image_bytes).decode()
    return f"data:{mime_type};base64,{b64}"


def decode_image_base64(data_uri: str) -> tuple[bytes, str]:
    """Decode a data URI into raw bytes and MIME type."""
    match = re.match(r"data:([^;]+);base64,(.+)", data_uri, re.DOTALL)
    if not match:
        raise ValueError(f"Invalid data URI: {data_uri[:80]}...")
    mime_type = match.group(1)
    raw_bytes = base64.b64decode(match.group(2))
    return raw_bytes, mime_type


def resolve_source(source: Path | str | bytes, filename: str | None = None) -> tuple[Path, bool]:
    """Resolve a source input to a file path.

    Returns (file_path, is_temp) where is_temp indicates a temporary file was created.
    """
    if isinstance(source, Path):
        return source, False
    if isinstance(source, bytes):
        suffix = Path(filename).suffix if filename else ".bin"
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(source)
        tmp.close()
        return Path(tmp.name), True
    # String: could be a file path or base64 content
    p = Path(source)
    if p.exists():
        return p, False
    # Assume base64-encoded content
    if source.startswith("data:"):
        raw_bytes, _ = decode_image_base64(source)
    else:
        raw_bytes = base64.b64decode(source)
    suffix = Path(filename).suffix if filename else ".bin"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(raw_bytes)
    tmp.close()
    return Path(tmp.name), True
