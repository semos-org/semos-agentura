"""Incremental email archive dump to SQLite.

Supports both COM (fast iteration) and IMAP (fetch-based) backends.
Resumable - tracks the last synced timestamp per folder.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from .backend import create_backend
from .config import Settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    entry_id TEXT PRIMARY KEY,
    subject TEXT,
    sender TEXT,
    sender_email TEXT,
    to_addr TEXT,
    cc TEXT,
    received TEXT,
    folder TEXT,
    has_attachments INTEGER,
    attachment_count INTEGER,
    attachment_names TEXT,
    body TEXT,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    entry_id TEXT,
    subject TEXT,
    start TEXT,
    end TEXT,
    location TEXT,
    all_day INTEGER,
    organizer TEXT,
    required TEXT,
    synced_at TEXT,
    PRIMARY KEY (entry_id, start)
);

CREATE TABLE IF NOT EXISTS sync_state (
    folder TEXT PRIMARY KEY,
    last_received TEXT,
    item_count INTEGER,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received);
CREATE INDEX IF NOT EXISTS idx_emails_subject ON emails(subject);
CREATE INDEX IF NOT EXISTS idx_emails_sender ON emails(sender);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
"""


class EmailArchive:
    """Incremental email archive to SQLite."""

    def __init__(self, settings: Settings | None = None, backend=None) -> None:
        self._settings = settings or Settings()
        self.backend = backend or create_backend(self._settings)
        self.backend.connect()
        self.db_path = Path(self._settings.archive_db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def get_sync_state(self, folder: str) -> str | None:
        row = self.conn.execute(
            "SELECT last_received FROM sync_state WHERE folder = ?", (folder,),
        ).fetchone()
        return row[0] if row else None

    def update_sync_state(self, folder: str, last_received: str, count: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sync_state (folder, last_received, item_count, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (folder, last_received, count, datetime.now().isoformat()),
        )
        self.conn.commit()

    def dump_emails(self, folder_name: str = "Inbox", include_body: bool = True,
                    batch_size: int = 100) -> int:
        """Dump emails from a folder. Uses COM iteration if available, else IMAP."""
        if self.backend.supports_com:
            return self._dump_emails_com(folder_name, include_body, batch_size)
        return self._dump_emails_imap(folder_name, include_body, batch_size)

    def _dump_emails_com(self, folder_name: str, include_body: bool, batch_size: int) -> int:
        from .com_client import OL_FOLDER_INBOX, OL_FOLDER_SENT, OL_FOLDER_DRAFTS, OL_FOLDER_DELETED

        folder_map = {
            "Inbox": OL_FOLDER_INBOX, "Sent": OL_FOLDER_SENT,
            "Drafts": OL_FOLDER_DRAFTS, "Deleted": OL_FOLDER_DELETED,
        }
        folder_id = folder_map.get(folder_name, OL_FOLDER_INBOX)
        last_synced = self.get_sync_state(f"email:{folder_name}")
        logger.info("Archiving %s emails (last sync: %s)", folder_name, last_synced or "never")

        com = self.backend.raw_com
        count = 0
        errors = 0
        batch = []
        last_received = last_synced
        start_time = time.time()

        for item, meta in com.iter_folder(folder_id, oldest_first=True):
            received = meta.get("received", "")
            if last_synced and received <= last_synced:
                continue

            try:
                body = ""
                if include_body:
                    try:
                        body = str(item.Body or "")
                    except Exception:
                        body = "(could not read body)"

                att_names = ", ".join(a["filename"] for a in meta.get("attachments", []))
                batch.append((
                    meta["entry_id"], meta["subject"], meta["sender"],
                    meta["sender_email"], meta.get("to", ""), meta.get("cc", ""),
                    received, folder_name,
                    int(meta.get("has_attachments", False)), meta.get("attachment_count", 0),
                    att_names, body, datetime.now().isoformat(),
                ))
                last_received = received
                count += 1
            except Exception as e:
                errors += 1
                logger.debug("Error processing email: %s", e)

            if len(batch) >= batch_size:
                self._insert_emails(batch)
                batch = []
                elapsed = time.time() - start_time
                rate = count / elapsed if elapsed > 0 else 0
                logger.info("  %s: %d emails (%.1f/sec, %d errors)", folder_name, count, rate, errors)
                if last_received:
                    self.update_sync_state(f"email:{folder_name}", last_received, count)

        if batch:
            self._insert_emails(batch)
        if last_received and last_received != last_synced:
            self.update_sync_state(f"email:{folder_name}", last_received, count)

        elapsed = time.time() - start_time
        logger.info("Done: %s - %d new emails in %.0fs (%d errors)", folder_name, count, elapsed, errors)
        return count

    def _dump_emails_imap(self, folder_name: str, include_body: bool, batch_size: int) -> int:
        """IMAP-based archive: fetch messages and store."""
        imap_folder_map = {"Inbox": "INBOX", "Sent": "Sent", "Drafts": "Drafts", "Deleted": "Deleted"}
        imap_folder = imap_folder_map.get(folder_name, "INBOX")
        last_synced = self.get_sync_state(f"email:{folder_name}")
        logger.info("Archiving %s via IMAP (last sync: %s)", folder_name, last_synced or "never")

        msgs = self.backend.list_messages(folder=imap_folder, limit=10000)
        count = 0
        errors = 0
        batch = []
        last_received = last_synced
        start_time = time.time()

        for msg in msgs:
            received = str(msg.date or "")
            if last_synced and received <= last_synced:
                continue

            try:
                body = ""
                if include_body:
                    try:
                        full = self.backend.get_message(msg.uid)
                        body = full.body
                    except Exception:
                        body = "(could not read body)"

                att_names = ", ".join(a.filename for a in msg.attachments)
                batch.append((
                    msg.uid, msg.subject, msg.sender_name or msg.sender,
                    msg.sender, "; ".join(msg.to), "; ".join(msg.cc),
                    received, folder_name,
                    int(bool(msg.attachments)), len(msg.attachments),
                    att_names, body, datetime.now().isoformat(),
                ))
                last_received = received
                count += 1
            except Exception as e:
                errors += 1
                logger.debug("Error processing email: %s", e)

            if len(batch) >= batch_size:
                self._insert_emails(batch)
                batch = []

        if batch:
            self._insert_emails(batch)
        if last_received and last_received != last_synced:
            self.update_sync_state(f"email:{folder_name}", last_received, count)

        elapsed = time.time() - start_time
        logger.info("Done: %s - %d new emails in %.0fs (%d errors)", folder_name, count, elapsed, errors)
        return count

    def _insert_emails(self, batch: list[tuple]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO emails "
            "(entry_id, subject, sender, sender_email, to_addr, cc, received, "
            "folder, has_attachments, attachment_count, attachment_names, body, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
        self.conn.commit()

    def dump_events(self, days_back: int = 365, days_forward: int = 90) -> int:
        """Dump calendar events. Uses COM or CalDAV if available."""
        cal = self.backend.calendar
        if cal is None:
            logger.info("Calendar not available - skipping event dump")
            return 0

        start = datetime.now() - timedelta(days=days_back)
        end = datetime.now() + timedelta(days=days_forward)
        logger.info("Archiving events %s to %s", start.strftime("%d.%m.%Y"), end.strftime("%d.%m.%Y"))

        events = cal.list_events(start, end, limit=100_000)
        count = 0
        batch = []
        for ev in events:
            batch.append((
                ev.entry_id, ev.subject,
                str(ev.start or ""), str(ev.end or ""),
                ev.location, int(ev.all_day),
                ev.organizer, ev.required_attendees,
                datetime.now().isoformat(),
            ))
            count += 1
            if len(batch) >= 100:
                self._insert_events(batch)
                batch = []

        if batch:
            self._insert_events(batch)

        self.update_sync_state("calendar", end.isoformat(), count)
        logger.info("Done: %d calendar events archived", count)
        return count

    def _insert_events(self, batch: list[tuple]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO events "
            "(entry_id, subject, start, end, location, all_day, organizer, required, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
        self.conn.commit()

    def dump_all(self, include_body: bool = True) -> dict:
        results = {}
        for folder_name in ("Inbox", "Sent", "Drafts", "Deleted"):
            results[folder_name] = self.dump_emails(folder_name, include_body=include_body)
        results["Calendar"] = self.dump_events()
        return results

    def stats(self) -> dict:
        email_count = self.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        event_count = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        sync_states = self.conn.execute(
            "SELECT folder, last_received, item_count, updated_at FROM sync_state"
        ).fetchall()
        return {
            "emails": email_count,
            "events": event_count,
            "db_size_mb": round(db_size / 1024 / 1024, 1),
            "sync_states": {r[0]: {"last": r[1], "count": r[2], "updated": r[3]} for r in sync_states},
        }

    def search(self, query: str, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT entry_id, subject, sender, received, folder, has_attachments "
            "FROM emails WHERE subject LIKE ? ORDER BY received DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [
            {"entry_id": r[0], "subject": r[1], "sender": r[2], "received": r[3],
             "folder": r[4], "has_attachments": bool(r[5])}
            for r in rows
        ]

    def close(self) -> None:
        self.conn.close()
