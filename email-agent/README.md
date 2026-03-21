# email-agent

Unified email client with LLM agent — supports IMAP/SMTP, Outlook COM, CalDAV, and Microsoft Graph API backends.

## Backends

| Backend | Email | Calendar | Platform | Provider |
|---------|-------|----------|----------|----------|
| COM | Yes | Yes | Windows only | M365/Exchange |
| IMAP/SMTP | Yes | No | Any | Any |
| CalDAV | No | Yes | Any | Google, Nextcloud, iCloud |
| Graph API | No | No | Any | M365 (Phase 3) |

Email and calendar backends are independent — e.g. IMAP for email + CalDAV for calendar.

**Auto-detection priority:**
1. Explicit `BACKEND=com|imap|graph` overrides
2. `IMAP_HOST` + `AZURE_CLIENT_ID` + `EMAIL_ADDRESS` → IMAP
3. `GRAPH_CLIENT_ID` → Graph API
4. Windows → COM

## Setup

```bash
uv sync              # base (IMAP + LLM agent)
uv sync --extra com  # + Outlook COM (Windows)
```

Copy `.env.example` to `.env` and configure.

### IMAP/SMTP (any OS)

```env
AZURE_CLIENT_ID=your-azure-app-client-id
AZURE_TENANT_ID=your-tenant-id
EMAIL_ADDRESS=user@example.com
IMAP_HOST=outlook.office365.com
SMTP_HOST=smtp.office365.com
```

### COM (Windows, no config needed)

Outlook must be running. Auto-detected on Windows if no IMAP config is set.

### @mailgent agent

```env
MODEL=anthropic/claude-sonnet-4-5
AZURE_API_KEY=your-api-key
AZURE_API_BASE=https://your-endpoint.openai.azure.com
```

## CLI

```bash
uv run python run.py                    # show help

# Email
uv run python run.py search-emails <query>
uv run python run.py read-email <query>
uv run python run.py read-email <query> --save-att
uv run python run.py draft <to> <subject> <body> [--cc=...] [--att=file1,file2]
uv run python run.py send <to> <subject> <body>

# Calendar
uv run python run.py events [days]
uv run python run.py free-slots [days]
uv run python run.py create-event <subject> <start> <end>

# @mailgent agent
uv run python run.py agent [interval]   # polling loop (default 30s)
uv run python run.py agent-once         # process pending and exit

# Archive
uv run python run.py archive            # incremental dump to SQLite
uv run python run.py archive-stats
uv run python run.py archive-search <query>
```

## @mailgent

Write `@mailgent:` in a draft or incoming email to trigger the LLM agent:

```
Dear colleague,
@mailgent: What are my free slots next week?

Best regards
```

The agent processes the prompt, calls tools (search emails, check calendar, etc.), and inserts the response directly below the prompt with a unique ID:

```
Dear colleague,
@mailgent[a1b2c3d4]: What are my free slots next week?
------------ mailgent[a1b2c3d4] ------------
Monday: 10:00-12:00, 14:00-17:00
Tuesday: 8:00-10:00, 13:00-17:00
------------ mailgent[a1b2c3d4] ------------

Best regards
```

**Re-run:** Delete the response block and remove the `[id]` from the prompt.

**Multiple prompts** per email are supported — each gets its own ID.

### Tools available to @mailgent

- `search_emails` — search by subject
- `read_email` — read full email content
- `list_events` — calendar events for next N days
- `free_slots` — free meeting slots
- `create_draft` — create email draft
- `draft_reply` / `send_reply` — reply to an email
- `draft_event` / `send_event` — create calendar event

### Config

```env
MAILGENT_TAG=@mailgent
MAILGENT_MODEL=anthropic/claude-sonnet-4-5    # or use MODEL=
MAILGENT_AUTO_REPLY=false                      # reply to inbox emails
MAILGENT_AUTO_SEND=false                       # send replies immediately
MAILGENT_TRUSTED_REPLY=*@company.com           # auto-reply trust list
MAILGENT_TRUSTED_SEND=me@company.com           # auto-send trust list
```

## Tests

```bash
uv run pytest tests/ -v                        # all tests
uv run pytest tests/test_backend.py -v         # unit tests (mocked, no backend needed)
uv run pytest tests/test_integration.py -v     # integration tests (needs real backend)
```

## Architecture

```
src/email_agent/
  config.py        Settings (Pydantic BaseSettings)
  models.py        EmailMessage, Attachment, EventInfo
  exceptions.py    Exception hierarchy
  auth.py          OAuth2 (MSAL device code flow)
  imap_client.py   IMAP read/search/draft
  smtp_client.py   SMTP send
  client.py        MailClient facade (IMAP + SMTP)
  backend.py       EmailBackend + CalendarBackend protocols, IMAPBackend, COMBackend, factory
  com_client.py    Outlook COM wrapper (Windows)
  tools.py         LLM tool definitions + executor
  mailgent.py      @mailgent agent loop
  formatting.py    Markdown ↔ plain/HTML conversion
  archive.py       Incremental SQLite archive
```
