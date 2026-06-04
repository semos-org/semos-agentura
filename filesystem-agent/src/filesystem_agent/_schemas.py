"""Protocol schemas for the add_root tool.

Extracted to avoid circular imports between service.py and tools.py.
"""

from __future__ import annotations

from typing import Any

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
            "SharePoint Online or OneDrive for Business. Connects via WebDAV with "
            "automatic cookie-based auth (opens browser, caches session for reuse). "
            "browser triggers password, smartcard/SSO login or email verification - "
            "user enters their email, receives a code, enters it, and checks 'stay signed in'. "
            "All connection details go in kwargs - do NOT use base_path for this protocol."
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
                            "SharePoint or OneDrive URL. Accepts: "
                            "team sites (https://tenant.sharepoint.com/sites/MySite), "
                            "personal sites (https://tenant-my.sharepoint.com/personal/user_domain_com), "
                            "or OneDrive sharing links (folder or single-file)."
                        ),
                        "pattern": r"^https://[^/]+\.sharepoint\.com(/.*)?$",
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


# Optional protocols - only included if their extra dependency is installed.
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
    # Google Drive - uses OAuth2 with GOOGLE_DRIVE_CLIENT_ID from .env.
    # No extra Python packages needed (uses Drive REST API directly via httpx).
    {
        "type": "object",
        "title": "google_drive",
        "description": (
            "Google Drive. Connects via OAuth2 (opens browser for Google login on first use, "
            "then caches and auto-refreshes the token). No extra packages needed. "
            "Requires GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET in .env "
            "(create at console.cloud.google.com). "
            "Supports personal Drive, shared folders, and Google Docs/Sheets/Slides export. "
            "All connection details go in kwargs - do NOT use base_path for this protocol."
        ),
        "properties": {
            "protocol": {"const": "google_drive"},
            "kwargs": {
                "type": "object",
                "description": "Google Drive connection details. Only share_url is required.",
                "additionalProperties": False,
                "properties": {
                    "share_url": {
                        "type": "string",
                        "description": (
                            "Google Drive URL. Accepts: "
                            "folder links (https://drive.google.com/drive/folders/ID?usp=sharing), "
                            "file links (https://drive.google.com/file/d/ID/view?usp=sharing), "
                            "Google Docs/Sheets/Slides links, "
                            "or https://drive.google.com/drive/my-drive for the user's own Drive, "
                            "or https://drive.google.com/drive/shared-with-me for all shared items."
                        ),
                        "pattern": r"^https://(drive|docs)\.google\.com/.*$",
                    },
                },
                "required": ["share_url"],
            },
        },
        "required": ["protocol", "kwargs"],
    },
]:
    if _s is not None:
        _PROTOCOL_SCHEMAS.append(_s)

ADD_ROOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Short identifier for the mount - becomes the URI scheme. "
                "Example: name='docs' creates URIs like docs://path/to/file"
            ),
        },
        "protocol": {
            "type": "string",
            "description": (
                "Storage backend to use. Each protocol has its own kwargs. "
                "For SharePoint, use 'sharepoint' (not 'webdav') - it handles auth automatically."
            ),
            "enum": [s["title"] for s in _PROTOCOL_SCHEMAS],
        },
        "base_path": {
            "type": "string",
            "description": (
                "Subdirectory to scope the root to (only for local/webdav/ftp/sftp/smb). "
                "NOT used for 'sharepoint' protocol - use kwargs.subfolder instead."
            ),
            "default": "",
        },
        "kwargs": {
            "type": "object",
            "description": (
                "Protocol-specific connection options. "
                "Required fields depend on the chosen protocol - see each protocol's schema."
            ),
            "oneOf": [s["properties"].get("kwargs", {"type": "object"}) for s in _PROTOCOL_SCHEMAS],
        },
    },
    "required": ["name", "protocol"],
    "oneOf": _PROTOCOL_SCHEMAS,
}
