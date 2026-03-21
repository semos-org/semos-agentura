"""CLI entry point for email-agent."""

import json
import logging
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

from email_agent import create_backend, EmailArchive, Mailgent, Settings


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-25s  %(levelname)-8s  %(message)s",
    )


def _safe_print(text: str) -> None:
    print(text.encode("utf-8", errors="replace").decode("utf-8"))


# Email commands

def cmd_search_emails(query: str):
    backend = create_backend()
    backend.connect()
    results = backend.search_emails(query)
    _safe_print(f"\nFound {len(results)} emails matching '{query}':\n")
    for r in results:
        att = f" [{len(r.attachments)} att]" if r.attachments else ""
        date_str = str(r.date)[:19] if r.date else "?"
        name = r.sender_name or r.sender
        _safe_print(f"  {date_str}  {name[:30]:30s}  {r.subject[:60]}{att}")


def cmd_read_email(query: str, save_attachments: bool = False):
    backend = create_backend()
    backend.connect()
    results = backend.search_emails(query, limit=1)
    if not results:
        print(f"No emails found matching '{query}'")
        return
    msg = backend.get_message(results[0].uid)
    _safe_print(f"\nFrom: {msg.sender_name} <{msg.sender}>")
    _safe_print(f"To: {', '.join(msg.to)}")
    _safe_print(f"CC: {', '.join(msg.cc)}")
    _safe_print(f"Subject: {msg.subject}")
    _safe_print(f"Date: {msg.date}")
    if msg.attachments:
        _safe_print("Attachments:")
        for a in msg.attachments:
            _safe_print(f"  - {a.filename}")
    _safe_print(f"\n{msg.body[:3000]}")


def cmd_draft(to: str, subject: str, body: str, cc: str = "", attachments: str = ""):
    backend = create_backend()
    backend.connect()
    att_list = [a.strip() for a in attachments.split(",") if a.strip()] if attachments else None
    entry_id = backend.create_draft(to, subject, body, cc=cc, attachments=att_list)
    print(f"Draft created: {subject} -> {to}")


def cmd_send(to: str, subject: str, body: str, cc: str = "", attachments: str = ""):
    backend = create_backend()
    backend.connect()
    att_list = [a.strip() for a in attachments.split(",") if a.strip()] if attachments else None
    backend.send_email(to, subject, body, cc=cc, attachments=att_list)
    print(f"Email sent: {subject} -> {to}")


# Calendar commands

def cmd_events(days: int = 14):
    backend = create_backend()
    backend.connect()
    cal = backend.calendar
    if cal is None:
        print("Calendar not available with the current backend.")
        return
    start = datetime.now()
    end = start + timedelta(days=days)
    events = cal.list_events(start, end)
    _safe_print(f"\nCalendar events ({start.strftime('%d.%m')} - {end.strftime('%d.%m.%Y')}): {len(events)}\n")
    for ev in events:
        _safe_print(f"  {ev}")


def cmd_free_slots(days: int = 14):
    backend = create_backend()
    backend.connect()
    cal = backend.calendar
    if cal is None:
        print("Calendar not available with the current backend.")
        return
    start = datetime.now()
    end = start + timedelta(days=days)
    slots = cal.free_slots(start, end)
    _safe_print(f"\nFree slots ({start.strftime('%d.%m')} - {end.strftime('%d.%m.%Y')}):\n")
    for day, free in slots.items():
        if free:
            slot_str = ", ".join(f"{s}-{e}" for s, e in free)
            _safe_print(f"  {day}: {slot_str}")
        else:
            _safe_print(f"  {day}: (no free slots)")


def cmd_create_event(subject: str, start_str: str, end_str: str,
                     location: str = "", attendees: str = ""):
    backend = create_backend()
    backend.connect()
    cal = backend.calendar
    if cal is None:
        print("Calendar not available with the current backend.")
        return
    start = datetime.strptime(start_str, "%Y-%m-%d %H:%M")
    end = datetime.strptime(end_str, "%Y-%m-%d %H:%M")
    cal.create_event(subject, start, end, location=location, required_attendees=attendees)
    print(f"Event created: {subject} ({start_str} - {end_str})")


# Archive commands

def cmd_archive(include_body: bool = True):
    archive = EmailArchive()
    print("Starting incremental archive dump...")
    print("(This may take hours for the first run. Safe to interrupt and resume.)\n")
    results = archive.dump_all(include_body=include_body)
    print(f"\nArchive complete:")
    for folder, count in results.items():
        print(f"  {folder}: {count} new items")
    stats = archive.stats()
    print(f"\nTotal: {stats['emails']} emails, {stats['events']} events, {stats['db_size_mb']} MB")
    archive.close()


def cmd_archive_stats():
    archive = EmailArchive()
    stats = archive.stats()
    print(f"Archive: {stats['emails']} emails, {stats['events']} events, {stats['db_size_mb']} MB")
    for folder, state in stats["sync_states"].items():
        last = state['last'][:19] if state['last'] else 'never'
        updated = state['updated'][:19] if state['updated'] else 'never'
        print(f"  {folder}: last={last}, count={state['count']}, updated={updated}")
    archive.close()


def cmd_archive_search(query: str):
    archive = EmailArchive()
    results = archive.search(query)
    _safe_print(f"\nArchive search '{query}': {len(results)} results\n")
    for r in results:
        att = " [att]" if r["has_attachments"] else ""
        _safe_print(f"  {r['received'][:19]}  {r['sender'][:30]:30s}  {r['subject'][:60]}{att}")
    archive.close()


# Main

def main():
    setup_logging()

    if len(sys.argv) < 2:
        settings = Settings()
        backend_name = settings.detected_backend
        print(f"Email Agent (backend: {backend_name})")
        print()
        print("Email commands:")
        print("  search-emails <query>          Search emails by subject")
        print("  read-email <query>             Read the most recent matching email")
        print("  read-email <query> --save-att  Read email and save attachments")
        print("  draft <to> <subject> <body>    Create a draft")
        print("  send <to> <subject> <body>     Send an email")
        print()
        print("Calendar commands:")
        print("  events [days]                  List events (default: 14 days)")
        print("  free-slots [days]              Show free slots (default: 14 days)")
        print("  create-event <subject> <start> <end>  Create event (YYYY-MM-DD HH:MM)")
        print()
        print("Agent commands:")
        print("  agent [interval]               Start @mailgent polling loop")
        print("  agent-once                     Process pending @mailgent emails and exit")
        print()
        print("Archive commands:")
        print("  archive                        Full incremental dump (resumable)")
        print("  archive-stats                  Show archive statistics")
        print("  archive-search <query>         Search local archive (offline)")
        sys.exit(0)

    cmd = sys.argv[1]

    match cmd:
        case "search-emails":
            cmd_search_emails(sys.argv[2] if len(sys.argv) > 2 else "")
        case "read-email":
            save = "--save-att" in sys.argv
            query = [a for a in sys.argv[2:] if not a.startswith("--")]
            cmd_read_email(" ".join(query), save_attachments=save)
        case "draft":
            if len(sys.argv) < 5:
                print("Usage: draft <to> <subject> <body> [--cc=...] [--att=file1,file2]")
                sys.exit(1)
            flags = {a.split("=", 1)[0].lstrip("-"): a.split("=", 1)[1] for a in sys.argv[5:] if "=" in a}
            cmd_draft(sys.argv[2], sys.argv[3], sys.argv[4],
                      cc=flags.get("cc", ""), attachments=flags.get("att", ""))
        case "send":
            if len(sys.argv) < 5:
                print("Usage: send <to> <subject> <body> [--cc=...] [--att=file1,file2]")
                sys.exit(1)
            flags = {a.split("=", 1)[0].lstrip("-"): a.split("=", 1)[1] for a in sys.argv[5:] if "=" in a}
            cmd_send(sys.argv[2], sys.argv[3], sys.argv[4],
                     cc=flags.get("cc", ""), attachments=flags.get("att", ""))
        case "events":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 14
            cmd_events(days)
        case "free-slots":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 14
            cmd_free_slots(days)
        case "create-event":
            if len(sys.argv) < 5:
                print("Usage: create-event <subject> '<YYYY-MM-DD HH:MM>' '<YYYY-MM-DD HH:MM>'")
                sys.exit(1)
            cmd_create_event(sys.argv[2], sys.argv[3], sys.argv[4],
                             location=sys.argv[5] if len(sys.argv) > 5 else "")
        case "archive":
            cmd_archive()
        case "archive-stats":
            cmd_archive_stats()
        case "archive-search":
            cmd_archive_search(sys.argv[2] if len(sys.argv) > 2 else "")
        case "agent":
            interval = int(sys.argv[2]) if len(sys.argv) > 2 else 30
            agent = Mailgent()
            agent.run(poll_interval=interval)
        case "agent-once":
            agent = Mailgent()
            count = agent.run_once()
            print(f"Processed {count} @mailgent email(s)")
        case _:
            print(f"Unknown command: {cmd}")
            sys.exit(1)


if __name__ == "__main__":
    main()
