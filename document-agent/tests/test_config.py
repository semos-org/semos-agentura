"""Tests for config.py - settings loading and provider detection."""

import os
from unittest.mock import patch

from document_agent.config import Settings


class TestSettings:
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            s = Settings(_env_file=None)
            assert s.document_ai_model == "mistral-document-ai-2512"
            assert s.max_pdf_pages == 10
            assert s.table_format == "markdown"

    def test_provider_type_mistral(self):
        with patch.dict(os.environ, {}, clear=True):
            s = Settings(_env_file=None)
            assert s.provider_type == "mistral"

    def test_provider_type_azure(self):
        with patch.dict(
            os.environ,
            {
                "DOCUMENT_AI_ENDPOINT": "https://example.com/models",
                "DOCUMENT_AI_API_KEY": "test-key",
            },
            clear=True,
        ):
            s = Settings(_env_file=None)
            assert s.provider_type == "azure"

    def test_tool_paths_default_none(self):
        with patch.dict(os.environ, {}, clear=True):
            s = Settings(_env_file=None)
            assert s.libre_office_path is None
            assert s.marp_path is None
            assert s.pandoc_path is None
            assert s.mmdc_path is None
            assert s.drawio_path is None

    def test_image_gen_settings_exist(self):
        s = Settings(_env_file=None)
        assert isinstance(s.image_gen_endpoint, str | None)
        assert isinstance(s.image_gen_api_key, str | None)
        assert isinstance(s.image_gen_model, str)

    def test_image_gen_settings_from_values(self):
        with patch.dict(os.environ, {}, clear=True):
            s = Settings(
                _env_file=None,
                image_gen_endpoint="https://example.com/openai",
                image_gen_api_key="test-key",
                image_gen_model="dall-e-3",
            )
            assert s.image_gen_endpoint == "https://example.com/openai"
            assert s.image_gen_api_key == "test-key"
            assert s.image_gen_model == "dall-e-3"
