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
# Optional protocols - only included if their extra dependency is installed.
def _optional_entry(
    title: str, description: str, check_import: str, kwargs_props: dict, kwargs_required: list | None = None
) -> dict | None:
    try:
        __import__(check_import)
    except ImportError:
        return None
    kwargs: dict[str, Any] = {"type": "object", "properties": kwargs_props}
    if kwargs_required:
        kwargs["required"] = kwargs_required
    return {
        "title": title,
        "description": description,
        "properties": {
            "name": _NAME_PROP,
            "protocol": {"const": title},
            "base_path": {"type": "string", "default": ""},
            "kwargs": kwargs,
        },
        "required": ["name", "protocol", "kwargs"],
    }


_OPTIONAL_SCHEMAS = [
    _optional_entry(
        "sftp",
        "SFTP/SSH file access",
        "paramiko",
        {
            "host": {"type": "string", "description": "SSH hostname"},
            "port": {"type": "integer", "description": "SSH port", "default": 22},
            "username": {"type": "string"},
            "password": {"type": "string"},
            "key_filename": {"type": "string", "description": "Path to SSH private key"},
        },
        ["host", "username"],
    ),
    _optional_entry(
        "smb",
        "SMB/CIFS network share",
        "smbprotocol",
        {
            "host": {"type": "string", "description": "Server hostname or IP"},
            "port": {"type": "integer", "default": 445},
            "username": {"type": "string"},
            "password": {"type": "string"},
        },
        ["host"],
    ),
    _optional_entry(
        "s3",
        "Amazon S3 or S3-compatible storage",
        "s3fs",
        {
            "key": {"type": "string", "description": "AWS access key ID"},
            "secret": {"type": "string", "description": "AWS secret access key"},
            "endpoint_url": {"type": "string", "description": "S3-compatible endpoint (MinIO, etc.)"},
            "region_name": {"type": "string", "description": "AWS region"},
            "anon": {"type": "boolean", "description": "Anonymous access (public buckets)", "default": False},
        },
    ),
    _optional_entry(
        "gcs",
        "Google Cloud Storage",
        "gcsfs",
        {
            "project": {"type": "string", "description": "GCP project ID"},
            "token": {"type": "string", "description": "Path to service account JSON or 'anon'"},
        },
    ),
    _optional_entry(
        "az",
        "Azure Blob Storage",
        "adlfs",
        {
            "account_name": {"type": "string", "description": "Azure storage account name"},
            "account_key": {"type": "string", "description": "Azure storage account key"},
            "connection_string": {"type": "string", "description": "Full connection string"},
            "sas_token": {"type": "string", "description": "Shared Access Signature token"},
        },
        ["account_name"],
    ),
]

# Build the final tool schema as a flat discriminated union.
# Each oneOf entry is a complete self-contained object with name, protocol,
# base_path (where applicable), and kwargs - no shared property definitions.
_NAME_PROP = {
    "type": "string",
    "description": (
        "Short identifier for the mount - becomes the URI scheme. "
        "Example: name='docs' creates URIs like docs://path/to/file"
    ),
}

ADD_ROOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name", "protocol"],
    "oneOf": [
        {
            "title": "local",
            "description": "Local filesystem",
            "properties": {
                "name": _NAME_PROP,
                "protocol": {"const": "local"},
                "base_path": {
                    "type": "string",
                    "description": "Absolute path to the directory to mount.",
                },
            },
            "required": ["name", "protocol", "base_path"],
        },
        {
            "title": "memory",
            "description": "In-memory filesystem (ephemeral, lost on restart)",
            "properties": {
                "name": _NAME_PROP,
                "protocol": {"const": "memory"},
            },
            "required": ["name", "protocol"],
        },
        {
            "title": "webdav",
            "description": "WebDAV server (SharePoint, Nextcloud, etc.)",
            "properties": {
                "name": _NAME_PROP,
                "protocol": {"const": "webdav"},
                "base_path": {
                    "type": "string",
                    "description": "Subfolder path on the WebDAV server.",
                    "default": "",
                },
                "kwargs": {
                    "type": "object",
                    "properties": {
                        "base_url": {"type": "string", "description": "WebDAV endpoint URL"},
                        "auth": {
                            "type": "object",
                            "description": "Auth credentials",
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
            "required": ["name", "protocol", "kwargs"],
        },
        {
            "title": "sharepoint",
            "description": (
                "SharePoint Online or OneDrive for Business. "
                "Automatic browser-based auth (SSO, smartcard, or email code). "
                "Do NOT use base_path - use kwargs.subfolder instead."
            ),
            "properties": {
                "name": _NAME_PROP,
                "protocol": {"const": "sharepoint"},
                "kwargs": {
                    "type": "object",
                    "description": "Only site_url is required; doc_library and subfolder are auto-detected.",
                    "properties": {
                        "site_url": {
                            "type": "string",
                            "description": (
                                "SharePoint or OneDrive URL. Accepts: "
                                "team sites (https://tenant.sharepoint.com/sites/MySite), "
                                "personal sites (https://tenant-my.sharepoint.com/personal/user), "
                                "or OneDrive sharing links."
                            ),
                            "pattern": r"^https://[^/]+\.sharepoint\.com(/.*)?$",
                        },
                        "doc_library": {
                            "type": "string",
                            "description": "Document library name. Auto-detected if omitted.",
                        },
                        "subfolder": {
                            "type": "string",
                            "description": "Subfolder within the doc library. Empty = library root.",
                            "default": "",
                        },
                    },
                    "required": ["site_url"],
                },
            },
            "required": ["name", "protocol", "kwargs"],
        },
        {
            "title": "http",
            "description": "Read-only HTTP/HTTPS file access",
            "properties": {
                "name": _NAME_PROP,
                "protocol": {"const": "http"},
                "base_path": {
                    "type": "string",
                    "description": "Base URL to the HTTP directory.",
                },
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
            "required": ["name", "protocol", "base_path"],
        },
        {
            "title": "ftp",
            "description": "FTP file access",
            "properties": {
                "name": _NAME_PROP,
                "protocol": {"const": "ftp"},
                "base_path": {
                    "type": "string",
                    "description": "Directory path on the FTP server.",
                    "default": "",
                },
                "kwargs": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string", "description": "FTP hostname"},
                        "port": {"type": "integer", "default": 21},
                        "username": {"type": "string"},
                        "password": {"type": "string"},
                    },
                    "required": ["host"],
                },
            },
            "required": ["name", "protocol", "kwargs"],
        },
        {
            "title": "google_drive",
            "description": (
                "Google Drive. OAuth2 auth (opens browser on first use, caches token). "
                "Requires GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET in .env. "
                "Supports folders, files, and Google Docs/Sheets/Slides export. "
                "Do NOT use base_path."
            ),
            "properties": {
                "name": _NAME_PROP,
                "protocol": {"const": "google_drive"},
                "kwargs": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "share_url": {
                            "type": "string",
                            "description": (
                                "Google Drive URL. Accepts: "
                                "folder links (https://drive.google.com/drive/folders/ID), "
                                "file links (https://drive.google.com/file/d/ID/view), "
                                "Google Docs/Sheets/Slides links, "
                                "https://drive.google.com/drive/my-drive, "
                                "or https://drive.google.com/drive/shared-with-me."
                            ),
                            "pattern": r"^https://(drive|docs)\.google\.com/.*$",
                        },
                    },
                    "required": ["share_url"],
                },
            },
            "required": ["name", "protocol", "kwargs"],
        },
    ],
}

# Append optional protocols (only if their dependency is installed)
for _s in _OPTIONAL_SCHEMAS:
    if _s is not None:
        ADD_ROOT_SCHEMA["oneOf"].append(_s)
