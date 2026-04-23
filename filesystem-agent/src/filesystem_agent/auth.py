from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import msal


class BearerAuth(httpx.Auth):
    """httpx-compatible auth that injects a Bearer token."""

    def __init__(self, token: str) -> None:
        self.token = token

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


class CookieAuth(httpx.Auth):
    """httpx-compatible auth that injects cookies extracted from a browser session."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    def auth_flow(self, request: httpx.Request):
        request.headers["Cookie"] = self.cookie_header
        yield request


def acquire_sharepoint_token(tenant_id: str, client_id: str, client_secret: str, scope: list[str]) -> str:
    """Acquire an app-only access token via MSAL client_credentials flow."""
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(scopes=scope)

    if "access_token" in result:
        return result["access_token"]

    error = result.get("error_description", result.get("error", "Unknown error"))
    raise RuntimeError(f"Failed to acquire SharePoint token: {error}")


SESSION_PATH = Path(".tokens/sharepoint_session.json")


async def _extract_cookies_via_browser(sharepoint_url: str, session_path: Path = SESSION_PATH) -> dict[str, str]:
    """Open a browser for SharePoint login (smartcard/SSO), then extract auth cookies.

    If a saved session exists, it is loaded first. After successful login the session
    is saved for reuse.
    """
    from playwright.async_api import async_playwright

    storage_state = str(session_path) if session_path.exists() else None

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            storage_state=storage_state,
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # Navigate to SharePoint — triggers SSO / smartcard prompt
        print("  Waiting for SharePoint login to complete...", flush=True)
        print("  (Complete smartcard/SSO login in the browser window)", flush=True)
        await page.goto(sharepoint_url, wait_until="domcontentloaded", timeout=120_000)

        # Poll for FedAuth cookie — the definitive sign that login succeeded
        sp_cookies = {}
        for _ in range(600):  # up to 5 minutes
            all_cookies = await context.cookies()
            sp_cookies = {}
            for c in all_cookies:
                if "sharepoint.com" in c.get("domain", ""):
                    sp_cookies[c["name"]] = c["value"]
            if "FedAuth" in sp_cookies:
                break
            await asyncio.sleep(0.5)

        if "FedAuth" not in sp_cookies:
            print("  [WARN] Timed out waiting for FedAuth cookie", flush=True)

        # Save session for reuse
        state = await context.storage_state()
        session_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

        print("  Browser closing automatically...", flush=True)
        await browser.close()
    finally:
        await pw.stop()

    return sp_cookies


def _load_cached_cookies(session_path: Path) -> dict[str, str] | None:
    """Try to load SharePoint cookies from a saved session file without opening a browser."""
    if not session_path.exists():
        return None
    try:
        state = json.loads(session_path.read_text(encoding="utf-8"))
        sp_cookies = {}
        for c in state.get("cookies", []):
            if "sharepoint.com" in c.get("domain", ""):
                sp_cookies[c["name"]] = c["value"]
        if "FedAuth" in sp_cookies:
            return sp_cookies
    except Exception:
        pass
    return None


def extract_sharepoint_cookies(sharepoint_url: str, session_path: Path = SESSION_PATH) -> dict[str, str]:
    """Extract SharePoint cookies — reuses cached session if available, otherwise opens browser."""
    cached = _load_cached_cookies(session_path)
    if cached:
        print("  Using cached session (no browser needed)", flush=True)
        return cached
    return asyncio.run(_extract_cookies_via_browser(sharepoint_url, session_path))
