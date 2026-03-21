from __future__ import annotations

import sys
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this package, not cwd.
_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
_ENV_FILES = [str(_AGENT_DIR / ".env"), ".env"]


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILES, env_file_encoding="utf-8", extra="ignore",
    )

    # Backend selection
    backend: str | None = Field(
        default=None,
        description="Force backend: 'com', 'imap', or 'graph'. Auto-detected if None.",
    )

    # Azure AD (for IMAP OAuth2)
    azure_client_id: str | None = Field(default=None, description="Azure App Registration client ID")
    azure_tenant_id: str = Field(default="common", description="Azure tenant ID")
    azure_client_secret: str | None = Field(default=None, description="Client secret (optional)")

    # User
    email_address: str | None = Field(default=None, description="User email address for XOAUTH2")

    # IMAP
    imap_host: str | None = Field(default=None)
    imap_port: int = 993

    # SMTP
    smtp_host: str | None = Field(default=None)
    smtp_port: int = 587

    # Token cache
    token_cache_path: str = ".token_cache.json"

    # LLM API (for mailgent - passed to litellm)
    model: str | None = Field(default=None, description="LLM model (litellm format)")
    azure_api_key: str | None = Field(default=None)
    azure_api_base: str | None = Field(default=None)
    anthropic_api_key: str | None = Field(default=None)

    # Mailgent agent
    mailgent_tag: str = "@mailgent"
    mailgent_model: str | None = Field(default=None, description="Override model for mailgent")
    mailgent_auto_reply: bool = False
    mailgent_auto_send: bool = False
    mailgent_trusted_reply: str = ""
    mailgent_trusted_send: str = ""

    # CalDAV (Phase 2)
    caldav_url: str | None = Field(default=None)
    caldav_username: str | None = Field(default=None)
    caldav_password: str | None = Field(default=None)
    caldav_calendar: str | None = Field(default=None, description="Calendar name (uses default if not set)")

    # Graph API (Phase 3)
    graph_client_id: str | None = Field(default=None, description="Triggers Graph backend")
    graph_tenant_id: str = "common"

    # Archive
    archive_db_path: str = ".email_archive.db"

    # Derived properties

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.azure_tenant_id}"

    @property
    def scopes(self) -> list[str]:
        return [
            "https://outlook.office365.com/IMAP.AccessAsUser.All",
            "https://outlook.office365.com/SMTP.Send",
        ]

    @property
    def detected_backend(self) -> str:
        """Determine which email backend to use."""
        from .exceptions import BackendNotAvailable

        if self.backend:
            return self.backend
        if self.imap_host and self.azure_client_id and self.email_address:
            return "imap"
        if self.graph_client_id:
            return "graph"
        if sys.platform == "win32":
            return "com"
        raise BackendNotAvailable(
            "No backend configured. Set IMAP_HOST + AZURE_CLIENT_ID + EMAIL_ADDRESS "
            "for IMAP, or GRAPH_CLIENT_ID for Graph, or run on Windows for COM."
        )

    @property
    def effective_mailgent_model(self) -> str:
        """Resolve the model to use for mailgent."""
        return self.mailgent_model or self.model or "anthropic/claude-sonnet-4-5"
