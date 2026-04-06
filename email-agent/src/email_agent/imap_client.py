from __future__ import annotations

import email
import email.utils
import imaplib
from datetime import datetime
from email.header import decode_header
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from typing import Self

from .auth import Authenticator
from .config import Settings
from .exceptions import IMAPError
from .models import Attachment, EmailMessage


class IMAPClient:
    """IMAP client for Microsoft 365 using XOAUTH2 authentication."""

    def __init__(self, settings: Settings, authenticator: Authenticator) -> None:
        self._settings = settings
        self._authenticator = authenticator
        self._conn: imaplib.IMAP4_SSL | None = None

    # -- Connection management --

    def connect(self) -> None:
        """Connect and authenticate via XOAUTH2."""
        try:
            self._conn = imaplib.IMAP4_SSL(self._settings.imap_host, self._settings.imap_port)
            access_token = self._authenticator.authenticate()
            auth_string = f"user={self._settings.email_address}\x01auth=Bearer {access_token}\x01\x01"
            self._conn.authenticate("XOAUTH2", lambda _: auth_string.encode())
        except imaplib.IMAP4.error as exc:
            raise IMAPError(f"IMAP connection failed: {exc}") from exc

    def disconnect(self) -> None:
        """Close the IMAP connection."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()

    @property
    def _imap(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            raise IMAPError("Not connected. Call connect() first.")
        return self._conn

    def _find_folder(self, flag: str) -> str:
        """Find a folder by its IMAP special-use flag (e.g. \\Drafts, \\Sent)."""
        status, folders = self._imap.list()
        if status != "OK" or not folders:
            raise IMAPError(f"Could not list folders to find {flag}")
        for entry in folders:
            decoded = entry.decode(errors="replace")
            if flag in decoded:
                # Extract folder name: last token after delimiter
                parts = decoded.split('" "' if '" "' in decoded else '"/"')
                name = parts[-1].strip().strip('"')
                return name
        raise IMAPError(f"No folder with flag {flag} found")

    # -- Read operations --

    def list_messages(self, folder: str = "INBOX", limit: int = 25) -> list[EmailMessage]:
        """List recent messages from a folder (headers only).

        Args:
            folder: IMAP folder name (e.g. "INBOX", "Drafts").
            limit: Maximum number of messages to return.

        Returns:
            List of EmailMessage with headers populated (no body/attachments).
        """
        self._imap.select(folder, readonly=True)
        status, data = self._imap.search(None, "ALL")
        if status != "OK":
            raise IMAPError(f"IMAP search failed: {status}")

        uids = data[0].split()
        if not uids:
            return []

        # Take the most recent N messages
        uids = uids[-limit:]
        uids.reverse()

        messages: list[EmailMessage] = []
        # Fetch headers in batch
        uid_range = b",".join(uids)
        status, fetch_data = self._imap.fetch(uid_range, "(FLAGS BODY.PEEK[HEADER])")
        if status != "OK":
            raise IMAPError(f"IMAP fetch failed: {status}")

        for i in range(0, len(fetch_data), 2):
            item = fetch_data[i]
            if not isinstance(item, tuple) or len(item) < 2:
                continue

            meta_line = item[0].decode(errors="replace")
            raw_headers = item[1]
            msg = email.message_from_bytes(raw_headers)

            # Extract UID from the meta line
            uid_str = self._extract_uid_from_meta(meta_line, uids, i // 2)

            # Parse flags
            is_read = b"\\Seen" in item[0] if isinstance(item[0], bytes) else "\\Seen" in meta_line

            messages.append(self._parse_headers(uid_str, msg, is_read))

        return messages

    def get_message(self, uid: str) -> EmailMessage:
        """Fetch a full message by UID including body and attachments.

        Args:
            uid: The IMAP message sequence number or UID.

        Returns:
            Fully populated EmailMessage.
        """
        status, data = self._imap.fetch(uid.encode(), "(FLAGS RFC822)")
        if status != "OK" or not data or not data[0]:
            raise IMAPError(f"Failed to fetch message {uid}")

        item = data[0]
        if not isinstance(item, tuple):
            raise IMAPError(f"Unexpected fetch response for message {uid}")

        meta_line = item[0].decode(errors="replace")
        raw_msg = item[1]
        msg = email.message_from_bytes(raw_msg)

        is_read = "\\Seen" in meta_line
        return self._parse_full(uid, msg, is_read)

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
            List of message sequence numbers matching the criteria.
        """
        self._imap.select(folder, readonly=True)

        criteria: list[str] = []
        if from_addr:
            criteria.append(f'FROM "{from_addr}"')
        if subject:
            criteria.append(f'SUBJECT "{subject}"')
        if since:
            criteria.append(f"SINCE {since.strftime('%d-%b-%Y')}")
        if before:
            criteria.append(f"BEFORE {before.strftime('%d-%b-%Y')}")
        if unseen:
            criteria.append("UNSEEN")

        search_str = " ".join(criteria) if criteria else "ALL"
        status, data = self._imap.search(None, search_str)
        if status != "OK":
            raise IMAPError(f"IMAP search failed: {status}")

        uids = data[0].split()
        uids.reverse()
        return [uid.decode() for uid in uids]

    # -- Flags --

    def mark_as_read(self, uid: str) -> None:
        """Mark a message as read."""
        self._imap.store(uid.encode(), "+FLAGS", "\\Seen")

    def mark_as_unread(self, uid: str) -> None:
        """Mark a message as unread."""
        self._imap.store(uid.encode(), "-FLAGS", "\\Seen")

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
        """Save a message as a draft.

        Args:
            to: Recipient email addresses.
            subject: Email subject.
            body: Email body content.
            body_type: "html" or "plain".
            cc: Optional CC recipients.
            attachments: Optional file paths to attach.
        """
        msg = self._compose_message(to, subject, body, body_type, cc, attachments)
        raw = msg.as_bytes()
        drafts_folder = self._find_folder("\\Drafts")
        status, _ = self._imap.append(drafts_folder, "\\Draft", None, raw)
        if status != "OK":
            raise IMAPError("Failed to save draft")

    def list_drafts(self, limit: int = 25) -> list[EmailMessage]:
        """List messages from the Drafts folder."""
        return self.list_messages(folder=self._find_folder("\\Drafts"), limit=limit)

    def delete_draft(self, uid: str) -> None:
        """Delete a draft by UID."""
        self._imap.select(self._find_folder("\\Drafts"))
        self._imap.store(uid.encode(), "+FLAGS", "\\Deleted")
        self._imap.expunge()

    # -- Internal helpers --

    def _compose_message(
        self,
        to: list[str],
        subject: str,
        body: str,
        body_type: str = "html",
        cc: list[str] | None = None,
        attachments: list[Path] | None = None,
    ) -> MIMEMultipart:
        """Compose a MIME message."""
        msg = MIMEMultipart("mixed")
        msg["From"] = self._settings.email_address
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject

        msg.attach(MIMEText(body, body_type))

        if attachments:
            for path in attachments:
                part = MIMEBase("application", "octet-stream")
                with open(path, "rb") as f:
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{path.name}"')
                msg.attach(part)

        return msg

    @staticmethod
    def _decode_header_value(value: str | None) -> str:
        """Decode a MIME-encoded header value."""
        if not value:
            return ""
        parts = decode_header(value)
        decoded: list[str] = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(part)
        return "".join(decoded)

    def _parse_headers(self, uid: str, msg: Message, is_read: bool) -> EmailMessage:
        """Parse email headers into an EmailMessage (no body)."""
        sender_raw = msg.get("From", "")
        sender_name, sender_addr = email.utils.parseaddr(sender_raw)

        to_raw = msg.get("To", "")
        to_addrs = [addr for _, addr in email.utils.getaddresses([to_raw]) if addr]

        cc_raw = msg.get("Cc", "")
        cc_addrs = [addr for _, addr in email.utils.getaddresses([cc_raw]) if addr]

        date_str = msg.get("Date")
        date = None
        if date_str:
            parsed = email.utils.parsedate_to_datetime(date_str)
            date = parsed

        return EmailMessage(
            uid=uid,
            message_id=msg.get("Message-ID"),
            subject=self._decode_header_value(msg.get("Subject")),
            sender=sender_addr,
            sender_name=self._decode_header_value(sender_name),
            to=to_addrs,
            cc=cc_addrs,
            date=date,
            is_read=is_read,
        )

    def _parse_full(self, uid: str, msg: Message, is_read: bool) -> EmailMessage:
        """Parse a full email message including body and attachments."""
        email_msg = self._parse_headers(uid, msg, is_read)

        body_text = ""
        body_html = ""
        attachments: list[Attachment] = []

        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in disposition:
                filename = part.get_filename() or "attachment"
                filename = self._decode_header_value(filename)
                payload = part.get_payload(decode=True)
                if payload:
                    attachments.append(
                        Attachment(
                            filename=filename,
                            content_type=content_type,
                            data=payload,
                        )
                    )
            elif content_type == "text/plain" and not body_text:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body_text = payload.decode(charset, errors="replace")
            elif content_type == "text/html" and not body_html:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body_html = payload.decode(charset, errors="replace")

        email_msg.body_text = body_text
        email_msg.body_html = body_html
        email_msg.attachments = attachments
        return email_msg

    @staticmethod
    def _extract_uid_from_meta(meta_line: str, uids: list[bytes], index: int) -> str:
        """Extract the message sequence number from a fetch response line."""
        # The meta line looks like: b'1 (FLAGS (\\Seen) BODY[HEADER] {1234}'
        # Extract the sequence number (first token)
        parts = meta_line.split()
        if parts:
            return parts[0]
        # Fallback to positional match
        if index < len(uids):
            return uids[index].decode()
        return "0"
