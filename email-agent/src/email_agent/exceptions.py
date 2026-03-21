from __future__ import annotations


class EmailAgentError(Exception):
    """Base exception for all email agent errors."""


class AuthenticationError(EmailAgentError):
    """Authentication-related errors (OAuth2 / MSAL)."""


class IMAPError(EmailAgentError):
    """IMAP operation errors."""


class SMTPError(EmailAgentError):
    """SMTP operation errors."""


class COMError(EmailAgentError):
    """Outlook COM automation errors."""


class BackendNotAvailable(EmailAgentError):
    """Requested backend is not available on this system."""


class CalendarNotSupported(EmailAgentError):
    """Calendar operation not supported by the current backend."""
