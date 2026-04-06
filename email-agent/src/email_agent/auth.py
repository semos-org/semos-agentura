from __future__ import annotations

import base64
import os
from typing import Callable

import msal

from .config import Settings
from .exceptions import AuthenticationError


class TokenCache:
    """Persistent MSAL token cache backed by a JSON file."""

    def __init__(self, cache_path: str) -> None:
        self._path = cache_path
        self._cache = msal.SerializableTokenCache()
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path, "r") as f:
                self._cache.deserialize(f.read())

    def save(self) -> None:
        if self._cache.has_state_changed:
            with open(self._path, "w") as f:
                f.write(self._cache.serialize())

    @property
    def cache(self) -> msal.SerializableTokenCache:
        return self._cache


class Authenticator:
    """OAuth2 authenticator using MSAL device code flow."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token_cache = TokenCache(settings.token_cache_path)
        self._app = msal.PublicClientApplication(
            client_id=settings.azure_client_id,
            authority=settings.authority,
            token_cache=self._token_cache.cache,
        )

    def authenticate(
        self,
        on_device_code: Callable[[str], None] | None = None,
    ) -> str:
        """Authenticate and return an access token.

        Tries silent auth from cache first, falls back to device code flow.

        Args:
            on_device_code: Optional callback to display the device code message.
                            Defaults to print().

        Returns:
            Access token string.
        """
        token = self._try_silent()
        if token:
            return token
        return self._device_code_flow(on_device_code)

    def _try_silent(self) -> str | None:
        accounts = self._app.get_accounts()
        if not accounts:
            return None
        result = self._app.acquire_token_silent(
            self._settings.scopes, account=accounts[0]
        )
        if result and "access_token" in result:
            return result["access_token"]
        return None

    def _device_code_flow(
        self,
        on_device_code: Callable[[str], None] | None = None,
    ) -> str:
        flow = self._app.initiate_device_flow(scopes=self._settings.scopes)
        if "error" in flow:
            raise AuthenticationError(
                f"Device flow initiation failed: {flow.get('error_description', flow['error'])}"
            )

        message = flow.get("message", "")
        if on_device_code:
            on_device_code(message)
        else:
            print(message)

        result = self._app.acquire_token_by_device_flow(flow)
        if "error" in result:
            raise AuthenticationError(
                f"Authentication failed: {result.get('error_description', result['error'])}"
            )

        self._token_cache.save()
        return result["access_token"]

    @staticmethod
    def build_xoauth2_string(user: str, access_token: str) -> str:
        """Build an XOAUTH2 authentication string for IMAP/SMTP.

        Format: base64("user=<email>\\x01auth=Bearer <token>\\x01\\x01")

        Returns:
            Base64-encoded XOAUTH2 string.
        """
        auth_string = f"user={user}\x01auth=Bearer {access_token}\x01\x01"
        return base64.b64encode(auth_string.encode()).decode()
