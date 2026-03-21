"""OCR providers: Mistral API and Azure AI Foundry."""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from mistralai.client import Mistral
from mistralai.client.models.documenturlchunk import DocumentURLChunk
from mistralai.client.models.imageurlchunk import ImageURLChunk
from mistralai.extra.utils.response_format import response_format_from_pydantic_model
from pydantic import BaseModel

from .._constants import IMAGE_EXTENSIONS, MIME_MAP, PDF_EXTENSIONS
from ..config import Settings
from ..exceptions import ProviderError
from ..models import ImageDescription
from ._ocr_models import OCRResponse

logger = logging.getLogger(__name__)

# Registry of Pydantic models used for annotations
_KNOWN_SCHEMAS: list[type[BaseModel]] = [ImageDescription]


def register_schema(cls: type[BaseModel]) -> None:
    if cls not in _KNOWN_SCHEMAS:
        _KNOWN_SCHEMAS.append(cls)


# ---------------------------------------------------------------------------
# Mistral API provider
# ---------------------------------------------------------------------------


class MistralProvider:
    """Direct Mistral API via the official SDK."""

    def __init__(self, settings: Settings):
        if not settings.mistral_api_key:
            raise ProviderError("MISTRAL_API_KEY is not set.")
        self.client = Mistral(api_key=settings.mistral_api_key)
        self.model = "mistral-ocr-latest"

    def ocr(self, file_path: Path, **ocr_kwargs: Any) -> OCRResponse:
        ext = file_path.suffix.lower()
        if ext in PDF_EXTENSIONS:
            uploaded = self.client.files.upload(
                file={"file_name": file_path.name, "content": file_path.read_bytes()},
                purpose="ocr",
            )
            signed_url = self.client.files.get_signed_url(file_id=uploaded.id, expiry=1)
            resp = self.client.ocr.process(
                model=self.model,
                document=DocumentURLChunk(document_url=signed_url.url),
                include_image_base64=True,
                table_format=ocr_kwargs.pop("table_format", "markdown"),
                **ocr_kwargs,
            )
        else:
            b64 = base64.b64encode(file_path.read_bytes()).decode()
            suffix = file_path.suffix.lstrip(".").lower()
            mime = MIME_MAP.get(suffix, f"image/{suffix}")
            data_uri = f"data:{mime};base64,{b64}"
            resp = self.client.ocr.process(
                model=self.model,
                document=ImageURLChunk(image_url=data_uri),
                include_image_base64=True,
                table_format=ocr_kwargs.pop("table_format", "markdown"),
                **ocr_kwargs,
            )
        return OCRResponse(resp)


# ---------------------------------------------------------------------------
# Azure AI Foundry provider
# ---------------------------------------------------------------------------


def _pydantic_to_azure_schema(sdk_format: Any) -> dict:
    """Convert a response_format_from_pydantic_model() result into the raw
    JSON schema dict Azure expects."""
    dumped = sdk_format.model_dump() if hasattr(sdk_format, "model_dump") else sdk_format
    js = dumped.get("json_schema", {})
    if js.get("schema") is not None:
        return dumped
    name = js.get("name", "annotation")
    schema = _get_json_schema_for_name(name)
    if schema is None:
        return dumped
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


def _get_json_schema_for_name(name: str) -> dict | None:
    for cls in _KNOWN_SCHEMAS:
        if cls.__name__ == name:
            schema = cls.model_json_schema()
            schema["additionalProperties"] = False
            return schema
    return None


class AzureProvider:
    """Azure AI Foundry deployment via raw HTTP."""

    def __init__(self, settings: Settings):
        if not settings.document_ai_endpoint or not settings.document_ai_api_key:
            raise ProviderError("DOCUMENT_AI_ENDPOINT and DOCUMENT_AI_API_KEY must both be set.")
        ep = settings.document_ai_endpoint.rstrip("/")
        # Strip /models suffix if present - OCR uses a different path
        if ep.endswith("/models"):
            ep = ep[:-7]
        self.endpoint = ep
        self.api_key = settings.document_ai_api_key
        self.model = settings.document_ai_model

    def ocr(self, file_path: Path, **ocr_kwargs: Any) -> OCRResponse:
        ext = file_path.suffix.lower()
        b64 = base64.b64encode(file_path.read_bytes()).decode()

        if ext in PDF_EXTENSIONS:
            document = {"type": "document_url", "document_url": f"data:application/pdf;base64,{b64}"}
        else:
            suffix = ext.lstrip(".")
            mime = MIME_MAP.get(suffix, f"image/{suffix}")
            document = {"type": "image_url", "image_url": f"data:{mime};base64,{b64}"}

        payload: dict[str, Any] = {
            "model": self.model,
            "document": document,
            "include_image_base64": True,
            "table_format": ocr_kwargs.pop("table_format", "markdown"),
        }

        for key, kwarg_key in (
            ("bbox_annotation_format", "bbox_annotation_format"),
            ("document_annotation_format", "document_annotation_format"),
        ):
            if kwarg_key not in ocr_kwargs:
                continue
            payload[key] = _pydantic_to_azure_schema(ocr_kwargs[kwarg_key])
        if "document_annotation_prompt" in ocr_kwargs:
            payload["document_annotation_prompt"] = ocr_kwargs["document_annotation_prompt"]

        url = f"{self.endpoint}/providers/mistral/azure/ocr"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

        max_retries = 3
        backoff = 30
        with httpx.Client() as http:
            for attempt in range(1, max_retries + 1):
                try:
                    resp = http.post(url, headers=headers, json=payload, timeout=300.0)
                except httpx.ReadTimeout:
                    if attempt < max_retries:
                        wait = backoff * attempt
                        logger.warning("Timeout, retrying in %ds (%d/%d)...", wait, attempt, max_retries)
                        time.sleep(wait)
                        continue
                    raise

                if resp.status_code in (408, 429, 500, 502, 503, 504):
                    if attempt < max_retries:
                        wait = backoff * attempt
                        logger.warning(
                            "HTTP %d, retrying in %ds (%d/%d)...",
                            resp.status_code, wait, attempt, max_retries,
                        )
                        time.sleep(wait)
                        continue
                    resp.raise_for_status()

                if resp.status_code >= 400:
                    raise ProviderError(f"Azure API error {resp.status_code}: {resp.text}")

                return OCRResponse(resp.json())
        raise ProviderError("Unreachable: all retries exhausted")


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


def get_provider(settings: Settings) -> MistralProvider | AzureProvider:
    """Pick provider based on available settings."""
    if settings.provider_type == "azure":
        logger.info(
            "Using Azure AI Foundry: %s",
            settings.document_ai_endpoint,
        )
        return AzureProvider(settings)
    return MistralProvider(settings)
