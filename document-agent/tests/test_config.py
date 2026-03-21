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
        with patch.dict(os.environ, {
            "DOCUMENT_AI_ENDPOINT": "https://example.com/models",
            "DOCUMENT_AI_API_KEY": "test-key",
        }, clear=True):
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
