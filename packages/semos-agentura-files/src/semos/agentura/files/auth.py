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


# Store sessions next to this module so it works regardless of cwd
_TOKENS_DIR = Path(__file__).resolve().parent.parent.parent / ".tokens"
SESSION_PATH = _TOKENS_DIR / "sharepoint_session.json"
GOOGLE_SESSION_PATH = _TOKENS_DIR / "google_drive_session.json"


async def _extract_cookies_via_browser(
    sharepoint_url: str, session_path: Path = SESSION_PATH
) -> tuple[dict[str, str], str]:
    """Open a browser for SharePoint login (smartcard/SSO), then extract auth cookies.

    If a saved session exists, it is loaded first. After successful login the session
    is saved for reuse.

    Returns (cookies, final_url) where final_url is the page URL after auth
    (useful for extracting shared folder paths from sharing link redirects).
    """
    from playwright.async_api import async_playwright

    storage_state = str(session_path) if session_path.exists() else None

    pw = await async_playwright().start()
    final_url = ""
    try:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            storage_state=storage_state,
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # Navigate to SharePoint - triggers SSO / smartcard / email code prompt
        print("  Waiting for SharePoint login to complete...", flush=True)
        print("  (Complete smartcard/SSO login in the browser window)", flush=True)
        await page.goto(sharepoint_url, wait_until="domcontentloaded", timeout=120_000)

        # Poll for FedAuth cookie - the definitive sign that login succeeded
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

        # Capture final URL (after redirects) for shared folder extraction
        final_url = page.url

        # Save session for reuse
        state = await context.storage_state()
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

        print("  Browser closing automatically...", flush=True)
        await browser.close()
    finally:
        await pw.stop()

    return sp_cookies, final_url


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


def _validate_cookies(sharepoint_url: str, cookies: dict[str, str]) -> bool:
    """Check if cached cookies are still valid with a lightweight API call."""
    from urllib.parse import urlparse

    # Extract just the site URL (https://tenant/sites/name) for the API call,
    # even if the caller passed a deeper path like .../Shared%20Documents
    parsed = urlparse(sharepoint_url)
    parts = parsed.path.rstrip("/").split("/")
    # Find /sites/SiteName and stop there
    site_path = parsed.path
    if "/sites/" in parsed.path:
        idx = parts.index("sites")
        site_path = "/".join(parts[: idx + 2])
    elif "/personal/" in parsed.path:
        idx = parts.index("personal")
        site_path = "/".join(parts[: idx + 2])
    site_root = f"{parsed.scheme}://{parsed.netloc}{site_path}"

    try:
        r = httpx.get(
            f"{site_root}/_api/web/title",
            auth=CookieAuth(cookies),
            headers={"Accept": "application/json;odata=verbose"},
            follow_redirects=True,
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def extract_sharepoint_cookies(sharepoint_url: str, session_path: Path = SESSION_PATH) -> tuple[dict[str, str], str]:
    """Extract SharePoint cookies - reuses cached session if available, otherwise opens browser.

    Validates cached cookies before returning them. If expired, opens browser for re-login.
    Works from both sync and async contexts.

    Returns (cookies, redirect_url) where redirect_url is the final page URL
    after auth (empty string when using cached session).
    """
    cached = _load_cached_cookies(session_path)
    if cached:
        if _validate_cookies(sharepoint_url, cached):
            print("  Using cached session (no browser needed)", flush=True)
            return cached, ""
        print("  Cached session expired, re-authenticating...", flush=True)
        # Remove expired session so the browser doesn't load stale FedAuth
        # (which would cause the polling loop to exit immediately)
        session_path.unlink(missing_ok=True)
    try:
        asyncio.get_running_loop()
        # Already in an async context - can't use asyncio.run().
        # Use nest_asyncio to allow nested event loops.
        import nest_asyncio

        nest_asyncio.apply()
    except RuntimeError:
        pass  # No running loop - asyncio.run() will work fine
    return asyncio.run(_extract_cookies_via_browser(sharepoint_url, session_path))


# Google Drive auth
# Uses OAuth2 InstalledAppFlow: opens browser for consent, local callback server.
# Requires GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET in .env.

_GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_TOKEN_FILE = _TOKENS_DIR / "google_drive_token.json"


def _load_cached_google_token() -> str | None:
    """Load cached Google OAuth token, refresh if expired."""
    if not GOOGLE_TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(GOOGLE_TOKEN_FILE.read_text(encoding="utf-8"))
        token = data.get("access_token", "")
        if token and _validate_google_token(token):
            return token
        # Try refresh
        refresh = data.get("refresh_token", "")
        cid = data.get("client_id", "")
        csecret = data.get("client_secret", "")
        if refresh and cid:
            r = httpx.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": cid,
                    "client_secret": csecret,
                },
                timeout=10,
            )
            if r.status_code == 200:
                new_token = r.json().get("access_token", "")
                data["access_token"] = new_token
                GOOGLE_TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
                print("  Google token refreshed", flush=True)
                return new_token
    except Exception:
        pass
    return None


def _validate_google_token(token: str) -> bool:
    """Check if a Google OAuth token is still valid."""
    if not token:
        return False
    try:
        r = httpx.get(
            "https://www.googleapis.com/drive/v3/about?fields=user",
            auth=BearerAuth(token),
            follow_redirects=True,
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def _run_google_oauth_flow() -> str:
    """Run OAuth2 installed app flow: opens browser, user consents, returns token."""
    import os
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    client_id = os.environ.get("GOOGLE_DRIVE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_DRIVE_CLIENT_SECRET", "")
    if not client_id:
        print("  [ERROR] GOOGLE_DRIVE_CLIENT_ID not set in .env", flush=True)
        return ""

    auth_code = None
    redirect_port = 8085

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code
            qs = parse_qs(urlparse(self.path).query)
            auth_code = qs.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Google Drive connected!</h2><p>You can close this tab.</p></body></html>"
            )

        def log_message(self, *args):
            pass

    redirect_uri = f"http://localhost:{redirect_port}"
    auth_url = (
        f"https://accounts.google.com/o/oauth2/auth"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={'%20'.join(_GOOGLE_SCOPES)}"
        f"&access_type=offline"
        f"&prompt=consent"
    )

    print("  Opening browser for Google Drive authorization...", flush=True)
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", redirect_port), _Handler)
    server.timeout = 300
    while auth_code is None:
        server.handle_request()
    server.server_close()

    if not auth_code:
        return ""

    # Exchange code for tokens
    r = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    if r.status_code != 200:
        print(f"  [ERROR] Token exchange failed: {r.text}", flush=True)
        return ""

    token_data = r.json()
    save_data = {
        "access_token": token_data.get("access_token", ""),
        "refresh_token": token_data.get("refresh_token", ""),
        "client_id": client_id,
        "client_secret": client_secret,
    }
    GOOGLE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOOGLE_TOKEN_FILE.write_text(json.dumps(save_data, indent=2), encoding="utf-8")
    print("  Google Drive authorized!", flush=True)
    return token_data.get("access_token", "")


class GoogleDriveAuth(httpx.Auth):
    """Shared, auto-refreshing Google Drive auth.

    All GDriveFolderFS / SingleFileGDriveFS instances should share a single
    instance so token refresh in one mount is visible to all others.
    Validates at most once per 5 minutes to avoid excessive API calls.
    """

    def __init__(self) -> None:
        self._token: str = ""
        self._validated_at: float = 0

    @property
    def token(self) -> str:
        """Return a valid token, refreshing if needed."""
        import time

        now = time.time()
        # Skip validation if checked recently (5 min TTL)
        if self._token and (now - self._validated_at) < 300:
            return self._token
        if self._token and _validate_google_token(self._token):
            self._validated_at = now
            return self._token
        # Try loading/refreshing from disk
        refreshed = _load_cached_google_token()
        if refreshed:
            self._token = refreshed
            self._validated_at = now
            return self._token
        # Need fresh login
        self._token = _run_google_oauth_flow()
        self._validated_at = now
        return self._token

    def auth_flow(self, request: httpx.Request):
        t = self.token
        if t:
            request.headers["Authorization"] = f"Bearer {t}"
        yield request


# Singleton instance shared across all Google Drive mounts
_google_drive_auth: GoogleDriveAuth | None = None


def extract_google_drive_auth(share_url: str = "") -> GoogleDriveAuth:
    """Get the shared Google Drive auth instance.

    First call loads/refreshes cached token or runs OAuth flow.
    Subsequent calls return the same instance (shared across all mounts).
    """
    global _google_drive_auth
    if _google_drive_auth is None:
        _google_drive_auth = GoogleDriveAuth()
    # Ensure we have a valid token (triggers refresh/login if needed)
    if not _google_drive_auth.token:
        print("  [ERROR] Google Drive login failed", flush=True)
    else:
        print("  Using Google Drive auth (shared across mounts)", flush=True)
    return _google_drive_auth
