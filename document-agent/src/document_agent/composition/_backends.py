"""Shared backend detection for presentation tools.

Template application backends:
  - com: PowerPoint COM automation (Windows, full branding)
  - uno: LibreOffice UNO API (experimental, limited branding)
  - docker: LibreOffice UNO in Docker (experimental, limited branding)

Slide merge backends:
  - com: PowerPoint COM (preserves animations, transitions, media)
  - pptx: python-pptx pure Python (portable, no animations)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def has_com() -> bool:
    """Check if PowerPoint COM automation is available (Windows)."""
    try:
        import win32com.client  # noqa: F401

        return True
    except ImportError:
        return False


def has_pptx() -> bool:
    """Check if python-pptx is available."""
    try:
        import pptx  # noqa: F401

        return True
    except ImportError:
        return False


def select_merge_backend(preference: str = "auto") -> str:
    """Select the best available backend for slide merging.

    Returns "com" or "pptx".
    Raises RuntimeError if no backend is available.
    """
    if preference != "auto":
        return preference

    if has_com():
        return "com"
    if has_pptx():
        return "pptx"
    raise RuntimeError(
        "No slide merge backend available. "
        "Install python-pptx (pip install python-pptx) "
        "or use Windows with PowerPoint for COM support."
    )


def select_template_backend(preference: str = "auto") -> str:
    """Select the best available backend for template application.

    Returns "com", "uno", or "docker".
    Raises RuntimeError if no backend is available.
    """
    if preference != "auto":
        return preference

    if has_com():
        return "com"
    # UNO and Docker are experimental stubs - they transfer placeholder
    # structure but not visual branding (logos, bars) because LO's OOXML
    # importer doesn't expose slide master shapes via UNO API.
    logger.warning(
        "No template backend available. "
        "PowerPoint COM is the only backend that transfers full branding. "
        "UNO/Docker transfer placeholder structure only."
    )
    raise RuntimeError(
        "No template backend available. PowerPoint COM (Windows) is required for full corporate branding."
    )
