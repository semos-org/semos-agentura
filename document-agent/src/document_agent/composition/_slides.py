"""Slide generation via Marp CLI."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .._utils import _project_root
from ..exceptions import CompositionError
from ..models import OutputFormat

logger = logging.getLogger(__name__)

def _find_browser() -> Path | None:
    """Auto-detect a usable Chromium-based browser.

    Search order: Playwright managed > Puppeteer bundled > system Edge >
    system Chrome > PATH (Linux/macOS).  Works on Windows, Linux (Docker),
    and macOS.
    """
    root = _project_root()

    # 1. Playwright managed Chromium (cross-platform, headless-ready)
    # Playwright stores browsers in a known cache dir
    pw_cache = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        or (
            Path.home() / "AppData" / "Local" / "ms-playwright"
            if os.name == "nt"
            else Path.home() / ".cache" / "ms-playwright"
        )
    )
    if pw_cache.is_dir():
        # Find newest chromium dir
        for chrome_dir in sorted(
            pw_cache.glob("chromium-*"), reverse=True
        ):
            for candidate in (
                chrome_dir / "chrome-win" / "chrome.exe",
                chrome_dir / "chrome-linux" / "chrome",
                chrome_dir / "chrome-mac" / "Chromium.app"
                / "Contents" / "MacOS" / "Chromium",
            ):
                if candidate.is_file():
                    return candidate

    # 2. Puppeteer bundled Chromium (draw.io-export)
    try:
        p = next(root.glob(
            "tools/node_modules/**/chrome-win/chrome.exe"
        ))
        if p.is_file():
            return p
    except StopIteration:
        pass
    try:
        p = next(root.glob(
            "tools/node_modules/**/chrome-linux/chrome"
        ))
        if p.is_file():
            return p
    except StopIteration:
        pass

    # 3. System browsers
    system_paths = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge"
             r"\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome"
             r"\Application\chrome.exe"),
    ]
    for sp in system_paths:
        if sp.is_file():
            return sp

    # 4. PATH lookup (Linux / macOS / Docker)
    for name in (
        "google-chrome-stable", "google-chrome",
        "chromium-browser", "chromium",
    ):
        found = shutil.which(name)
        if found:
            return Path(found)

    return None


def compose_slides(
    md_path: Path,
    output_path: Path,
    format: OutputFormat,
    *,
    marp_path: Path,
    libre_office_path: str | None = None,
) -> Path:
    """Generate slides from Marp-flavored markdown.

    Supports HTML, PDF, and PPTX output.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if format == OutputFormat.HTML:
        _run_marp(marp_path, md_path, output_path, "--html")
    elif format == OutputFormat.PDF:
        _run_marp(marp_path, md_path, output_path, "--pdf")
    elif format == OutputFormat.PPTX:
        _run_marp(marp_path, md_path, output_path, "--pptx", extra_flags=["--pptx-editable"])
    else:
        raise CompositionError(f"Unsupported slide format: {format}")

    if not output_path.exists():
        raise CompositionError(f"Marp did not produce output: {output_path}")
    logger.info("Slides written: %s", output_path)
    return output_path


def _run_marp(
    marp_path: Path, md_path: Path, output_path: Path, format_flag: str,
    extra_flags: list[str] | None = None,
) -> None:
    """Run the marp CLI."""
    env = os.environ.copy()
    # Auto-detect browser for PDF/PPTX if not already configured
    if not env.get("CHROME_PATH") and format_flag in ("--pdf", "--pptx"):
        browser = _find_browser()
        if browser:
            logger.info("Auto-detected browser: %s", browser)
            env["CHROME_PATH"] = str(browser)
            env["PUPPETEER_EXECUTABLE_PATH"] = str(browser)

    cmd = [
        str(marp_path),
        str(md_path),
        format_flag,
        "--allow-local-files",
        "--output", str(output_path),
        *(extra_flags or []),
    ]
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120,
        shell=(os.name == "nt"), env=env,
    )
    if result.returncode != 0:
        raise CompositionError(f"Marp failed: {result.stderr}")
