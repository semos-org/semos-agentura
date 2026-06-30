from __future__ import annotations

import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Self

from .auth import Authenticator
from .config import Settings
from .exceptions import SMTPError


class SMTPClient:
    """SMTP client for Microsoft 365 using XOAUTH2 authentication."""

    def __init__(self, settings: Settings, authenticator: Authenticator) -> None:
        self._settings = settings
        self._authenticator = authenticator
        self._conn: smtplib.SMTP | None = None

    # -- Connection management --

    def connect(self) -> None:
        """Connect and authenticate via XOAUTH2 over STARTTLS."""
        try:
            self._conn = smtplib.SMTP(self._settings.smtp_host, self._settings.smtp_port)
            self._conn.ehlo()
            self._conn.starttls()
            self._conn.ehlo()

            access_token = self._authenticator.authenticate()
            auth_string = Authenticator.build_xoauth2_string(self._settings.email_address, access_token)
            code, msg = self._conn.docmd("AUTH", f"XOAUTH2 {auth_string}")
            if code != 235:
                raise SMTPError(f"SMTP XOAUTH2 auth failed ({code}): {msg.decode(errors='replace')}")
        except smtplib.SMTPException as exc:
            raise SMTPError(f"SMTP connection failed: {exc}") from exc

    def disconnect(self) -> None:
        """Close the SMTP connection."""
        if self._conn:
            try:
                self._conn.quit()
            except Exception:
                pass
            self._conn = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()

    @property
    def _smtp(self) -> smtplib.SMTP:
        if self._conn is None:
            raise SMTPError("Not connected. Call connect() first.")
        return self._conn

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
        msg = MIMEMultipart("mixed")
        msg["From"] = self._settings.email_address
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject

        msg.attach(MIMEText(body, body_type))

        if attachments:
            for path in attachments:
                if not path.exists():
                    raise FileNotFoundError(f"Attachment not found: {path}")
                part = MIMEBase("application", "octet-stream")
                with open(path, "rb") as f:
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{path.name}"')
                msg.attach(part)

        # Collect all recipients for the SMTP envelope
        all_recipients = list(to)
        if cc:
            all_recipients.extend(cc)
        if bcc:
            all_recipients.extend(bcc)

        try:
            self._smtp.sendmail(self._settings.email_address, all_recipients, msg.as_string())
        except smtplib.SMTPException as exc:
            raise SMTPError(f"Failed to send email: {exc}") from exc
