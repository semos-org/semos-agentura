from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Self

from .auth import Authenticator
from .config import Settings
from .imap_client import IMAPClient
from .models import EmailMessage
from .smtp_client import SMTPClient


class MailClient:
    """Unified email client combining IMAP (read/search) and SMTP (send).

    Usage::

        from email_agent import MailClient

        with MailClient() as client:
            # List inbox
            for msg in client.fetch_messages():
                print(msg)

            # Search
            results = client.search(from_addr="boss@company.com", unseen=True)

            # Read full message
            full = client.get_message(results[0])
            print(full.body)

            # Send
            client.send(
                to=["colleague@company.com"],
                subject="Hello",
                body="<p>Hi there!</p>",
            )

            # Drafts
            client.save_draft(
                to=["someone@company.com"],
                subject="WIP",
                body="<p>Not ready yet</p>",
            )
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self._authenticator = Authenticator(self._settings)
        self._imap = IMAPClient(self._settings, self._authenticator)
        self._smtp = SMTPClient(self._settings, self._authenticator)

    # -- Connection management --

    def connect(self) -> None:
        """Connect both IMAP and SMTP clients."""
        self._imap.connect()
        self._smtp.connect()

    def disconnect(self) -> None:
        """Disconnect both clients."""
        self._imap.disconnect()
        self._smtp.disconnect()

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()

    # -- Read --

    def fetch_messages(
        self, folder: str = "INBOX", limit: int = 25
    ) -> list[EmailMessage]:
        """List recent messages from a folder (headers only).

        Args:
            folder: IMAP folder name.
            limit: Maximum number of messages.

        Returns:
            List of EmailMessage with headers populated.
        """
        return self._imap.list_messages(folder=folder, limit=limit)

    def get_message(self, uid: str) -> EmailMessage:
        """Fetch a full message by UID including body and attachments.

        Args:
            uid: Message sequence number.

        Returns:
            Fully populated EmailMessage.
        """
        return self._imap.get_message(uid)

    # -- Search --

    def search(
        self,
        folder: str = "INBOX",
        from_addr: str | None = None,
        subject: str | None = None,
        since: datetime | None = None,
        before: datetime | None = None,
        unseen: bool = False,
    ) -> list[str]:
        """Search for messages matching criteria.

        Args:
            folder: IMAP folder to search in.
            from_addr: Filter by sender address.
            subject: Filter by subject text.
            since: Messages received on or after this date.
            before: Messages received before this date.
            unseen: Only return unread messages.

        Returns:
            List of message sequence numbers.
        """
        return self._imap.search(
            folder=folder,
            from_addr=from_addr,
            subject=subject,
            since=since,
            before=before,
            unseen=unseen,
        )

    # -- Send --

    def send(
        self,
        to: list[str],
        subject: str,
        body: str,
        body_type: str = "html",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[Path] | None = None,
    ) -> None:
        """Send an email.

        Args:
            to: Recipient email addresses.
            subject: Email subject.
            body: Email body content.
            body_type: "html" or "plain".
            cc: Optional CC recipients.
            bcc: Optional BCC recipients.
            attachments: Optional file paths to attach.
        """
        self._smtp.send(
            to=to,
            subject=subject,
            body=body,
            body_type=body_type,
            cc=cc,
            bcc=bcc,
            attachments=attachments,
        )

    # -- Flags --

    def mark_as_read(self, uid: str) -> None:
        """Mark a message as read."""
        self._imap.mark_as_read(uid)

    def mark_as_unread(self, uid: str) -> None:
        """Mark a message as unread."""
        self._imap.mark_as_unread(uid)

    # -- Drafts --

    def save_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        body_type: str = "html",
        cc: list[str] | None = None,
        attachments: list[Path] | None = None,
    ) -> None:
        """Save a message as a draft in the Drafts folder.

        Args:
            to: Recipient email addresses.
            subject: Email subject.
            body: Email body content.
            body_type: "html" or "plain".
            cc: Optional CC recipients.
            attachments: Optional file paths to attach.
        """
        self._imap.save_draft(
            to=to,
            subject=subject,
            body=body,
            body_type=body_type,
            cc=cc,
            attachments=attachments,
        )

    def list_drafts(self, limit: int = 25) -> list[EmailMessage]:
        """List messages from the Drafts folder.

        Args:
            limit: Maximum number of drafts to return.

        Returns:
            List of EmailMessage from Drafts.
        """
        return self._imap.list_drafts(limit=limit)

    def delete_draft(self, uid: str) -> None:
        """Delete a draft by UID.

        Args:
            uid: Draft message sequence number.
        """
        self._imap.delete_draft(uid)
