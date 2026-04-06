from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this package, not cwd.
# This ensures settings load correctly whether running from the
# workspace root (uv run uvicorn ...) or from document-agent/.
_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
_ENV_FILES = [str(_AGENT_DIR / ".env"), ".env"]


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Mistral API
    mistral_api_key: str | None = Field(default=None)

    # Document AI (Azure AI Foundry)
    document_ai_endpoint: str | None = Field(default=None)
    document_ai_api_key: str | None = Field(default=None)
    document_ai_model: str = Field(default="mistral-document-ai-2512")

    # Diagram code generation LLM
    diagram_codegen_endpoint: str | None = Field(default=None)
    diagram_codegen_api_key: str | None = Field(default=None)
    diagram_codegen_model: str | None = Field(default=None)

    # Diagram review LLM (falls back to document_ai_* if unset)
    diagram_review_endpoint: str | None = Field(default=None)
    diagram_review_api_key: str | None = Field(default=None)
    diagram_review_model: str | None = Field(default=None)

    # OCR settings
    table_format: str = Field(default="markdown")  # "markdown" or "html"
    max_pdf_pages: int = Field(default=10)

    # External tool paths (auto-detected on PATH if None)
    libre_office_path: str | None = Field(default=None)
    marp_path: str | None = Field(default=None)
    pandoc_path: str | None = Field(default=None)
    mmdc_path: str | None = Field(default=None)
    drawio_path: str | None = Field(default=None)

    @property
    def provider_type(self) -> str:
        if self.document_ai_endpoint and self.document_ai_api_key:
            return "azure"
        return "mistral"
