"""Protocol schemas for the add_root tool.

Extracted to avoid circular imports between service.py and tools.py.

The add_root input is ``{name, config}`` where ``config`` is a discriminated
union keyed on ``config.protocol`` (a const per variant). The union lives under
the ``config`` property - nested, so the Anthropic API accepts it (it rejects
oneOf/allOf/anyOf only at the TOP level of a tool input_schema).
"""

from __future__ import annotations

from typing import Any

_NAME_PROP = {
    "type": "string",
    "description": (
        "Short identifier for the mount - becomes the URI scheme. "
        "Example: name='docs' creates URIs like docs://path/to/file"
    ),
}


# Optional protocols - only included if their extra dependency is installed.
# Each returns a config variant: {protocol const + connection fields}.
def _optional_entry(
    title: str,
    description: str,
    check_import: str,
    fields: dict,
    required: list | None = None,
    *,
    allow_base_path: bool = True,
) -> dict | None:
    try:
        __import__(check_import)
    except ImportError:
        return None
    props: dict[str, Any] = {"protocol": {"const": title}}
    if allow_base_path:
        props["base_path"] = {
            "type": "string",
            "description": "Subfolder to scope the root to.",
            "default": "",
        }
    props.update(fields)
    return {
        "title": title,
        "description": description,
        "type": "object",
        "properties": props,
        "required": ["protocol", *(required or [])],
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


# Per-protocol config variants. Each is discriminated on the `protocol` const
# and carries its own connection fields (base_path where it applies, otherwise
# named fields like site_url / share_url).
_CORE_SCHEMAS: list[dict[str, Any]] = [
    {
        "title": "local",
        "description": "Local filesystem.",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "protocol": {"const": "local"},
            "base_path": {
                "type": "string",
                "description": "Absolute path to the directory to mount.",
            },
        },
        "required": ["protocol", "base_path"],
    },
    {
        "title": "memory",
        "description": "In-memory filesystem (ephemeral, lost on restart).",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "protocol": {"const": "memory"},
        },
        "required": ["protocol"],
    },
    {
        "title": "webdav",
        "description": "WebDAV server (Nextcloud, ownCloud, generic WebDAV).",
        "type": "object",
        "properties": {
            "protocol": {"const": "webdav"},
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
            "base_path": {
                "type": "string",
                "description": "Subfolder path on the WebDAV server.",
                "default": "",
            },
        },
        "required": ["protocol", "base_url"],
    },
    {
        "title": "sharepoint",
        "description": (
            "SharePoint Online or OneDrive for Business. Automatic browser-based auth (SSO, smartcard, or email code)."
        ),
        "type": "object",
        "properties": {
            "protocol": {"const": "sharepoint"},
            "site_url": {
                "type": "string",
                "description": (
                    "SharePoint or OneDrive URL. Accepts: "
                    "team sites (https://tenant.sharepoint.com/sites/MySite), "
                    "personal sites (https://tenant-my.sharepoint.com/personal/user), "
                    "or OneDrive sharing links, e.g. "
                    "https://tenant-my.sharepoint.com/:f:/g/personal/user/"
                    "Ab1Cd2Ef3?e=Xy9Zaa (folder) or .../:w:/g/.../Doc?e=... (file)."
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
        "required": ["protocol", "site_url"],
    },
    {
        "title": "http",
        "description": "Read-only HTTP/HTTPS file access.",
        "type": "object",
        "properties": {
            "protocol": {"const": "http"},
            "base_path": {
                "type": "string",
                "description": "Base URL to the HTTP directory.",
            },
            "headers": {
                "type": "object",
                "description": "Extra HTTP headers",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["protocol", "base_path"],
    },
    {
        "title": "ftp",
        "description": "FTP file access.",
        "type": "object",
        "properties": {
            "protocol": {"const": "ftp"},
            "host": {"type": "string", "description": "FTP hostname"},
            "port": {"type": "integer", "default": 21},
            "username": {"type": "string"},
            "password": {"type": "string"},
            "base_path": {
                "type": "string",
                "description": "Directory path on the FTP server.",
                "default": "",
            },
        },
        "required": ["protocol", "host"],
    },
    {
        "title": "google_drive",
        "description": (
            "Google Drive. OAuth2 auth (opens browser on first use, caches token). "
            "Requires GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET in .env. "
            "Supports folders, files, and Google Docs/Sheets/Slides export."
        ),
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "protocol": {"const": "google_drive"},
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
        "required": ["protocol", "share_url"],
    },
]

# add_root tool schema: {name, config}. config is a discriminated union keyed
# on protocol (nested under a property, so the Anthropic API accepts it -
# it rejects oneOf only at the TOP level). Defined statically, then one oneOf
# element is added per enabled protocol below.
ADD_ROOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": _NAME_PROP,
        "config": {
            "description": (
                "Mount configuration. Pick the variant matching your storage "
                "backend; each sets 'protocol' and its own fields."
            ),
            "oneOf": [],
        },
    },
    "required": ["name", "config"],
}

# Add one oneOf element per enabled protocol (core always; optional only when
# its dependency is installed).
for _variant in [*_CORE_SCHEMAS, *_OPTIONAL_SCHEMAS]:
    if _variant is not None:
        ADD_ROOT_SCHEMA["properties"]["config"]["oneOf"].append(_variant)

# Same list, exposed for server-side protocol validation in service.py.
_PROTOCOL_SCHEMAS = ADD_ROOT_SCHEMA["properties"]["config"]["oneOf"]
