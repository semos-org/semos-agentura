from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlparse

from dotenv import load_dotenv
from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env from agent dir first, then workspace root
_agent_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_agent_dir / ".env")
load_dotenv(_agent_dir.parent / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    azure_tenant_id: str = Field(default="", description="Azure AD tenant ID (GUID)")
    azure_client_id: str = Field(default="", description="Azure AD app registration client ID")
    azure_client_secret: str = Field(default="", description="Azure AD app registration client secret")

    sharepoint_drive_path: str = Field(
        default="",
        description="Local/mapped drive path to SharePoint document library, e.g. T:\\Documents",
    )

    sharepoint_site_url: str = Field(
        default="",
        description="SharePoint site URL (no trailing slash), e.g. https://contoso.sharepoint.com/sites/MySite",
    )
    sharepoint_doc_library: str = Field(
        default="Freigegebene Dokumente",
        description="Document library display name",
    )
    sharepoint_subfolder: str = Field(
        default="General",
        description="Subfolder within the document library (empty for root)",
    )

    @computed_field
    @property
    def sharepoint_tenant(self) -> str:
        return urlparse(self.sharepoint_site_url).hostname or ""

    @computed_field
    @property
    def webdav_base_url(self) -> str:
        return f"{self.sharepoint_site_url}/{quote(self.sharepoint_doc_library)}"

    @computed_field
    @property
    def webdav_folder_path(self) -> str:
        if self.sharepoint_subfolder:
            return f"/{quote(self.sharepoint_subfolder)}"
        return "/"

    @computed_field
    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.azure_tenant_id}"

    @computed_field
    @property
    def sharepoint_scope(self) -> list[str]:
        return [f"https://{self.sharepoint_tenant}/.default"]
