"""Unified email client - IMAP/SMTP + Outlook COM, with LLM agent and archive."""

from .client import MailClient
from .config import Settings
from .models import Attachment, EmailMessage, EventInfo
from .backend import EmailBackend, CalendarBackend, IMAPBackend, create_backend
from .archive import EmailArchive
from .mailgent import Mailgent

__all__ = [
    "Attachment",
    "EmailMessage",
    "EventInfo",
    "MailClient",
    "Settings",
    "EmailBackend",
    "CalendarBackend",
    "IMAPBackend",
    "create_backend",
    "EmailArchive",
    "Mailgent",
]

# COM imports are platform-guarded
try:
    from .backend import COMBackend  # noqa: F401

    __all__.append("COMBackend")
except (ImportError, NameError):
    pass
