"""Tests for composition/_slides.py - browser detection."""

from pathlib import Path
from unittest.mock import patch

from document_agent.composition._slides import _find_browser


class TestFindBrowser:
    def test_returns_path_or_none(self):
        result = _find_browser()
        # Depending on the environment, a browser may or may not be found
        assert result is None or isinstance(result, Path)

    def test_found_browser_exists(self):
        result = _find_browser()
        if result is not None:
            assert result.is_file()

    @patch("shutil.which", return_value=None)
    def test_fallback_to_system_paths(self, mock_which):
        # Even with which returning None, system paths may find a browser
        result = _find_browser()
        # Just verify it doesn't crash
        assert result is None or isinstance(result, Path)
