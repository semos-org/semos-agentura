# AI Assistant Capabilities – Use Case Example

**Multi-step Meeting Coordination with Calendar, Survey & Email Integration**

*Date: May 2026*

---

## Scenario

A project coordinator at a research institution needs to schedule a **pre-kickoff meeting** with contributors of a specific work package (WP) before an upcoming **EU project kickoff event** in another city. The meeting should cover two task-level agenda items. A **scheduling poll** needs to be created and distributed via email to all relevant colleagues.

---

## What the Assistant Did

### 1. Context gathering (parallel)

The assistant simultaneously:

- **Searched emails** for the upcoming kickoff event to determine its date and location
- **Searched emails** for the most recent communication to the WP mailing list to identify all contributor addresses
- **Retrieved the current date** to anchor all relative date references

> *All three actions were executed in a single step, in parallel.*

---

### 2. Calendar analysis

Using the identified kickoff date as the upper bound and "end of this week" as the lower bound, the assistant:

- **Listed all calendar events** across the full relevant period (~4 weeks)
- **Computed free slots** within business hours across all candidate days
- Identified days to **exclude** due to travel, fully blocked days, or events ending too close to lunch

---

### 3. Slot selection with constraints

The assistant applied a layered set of constraints to select poll options:

| Constraint | Source |
|---|---|
| Must fall before kickoff date | Extracted from calendar event |
| Must be free in organizer's calendar | Free-slot computation |
| No lunchtime slots (12:00–13:00) | User instruction |
| Specific date excluded entirely | User instruction |
| Two slots per day (morning + afternoon) | User instruction |
| Slots must be exactly 1 hour and fit within free windows | Logical inference |

The final poll contained **14 slots across 7 days**.

---

### 4. Meeting poll

The assistant created and deployed a **Doodle-style scheduling survey** using a connected survey service:

- Name field (non-anonymized, to identify respondents)
- Matrix question: each time slot as a row, availability options (Available / Maybe / Not available) as columns
- Free-text comment field for alternative suggestions
- Survey activated immediately upon deployment

---

### 5. Email draft

The assistant composed a professional **email draft** addressed to all WP contributors (extracted from the email thread), including:

- Context: upcoming EU project kickoff event
- Meeting purpose: alignment on two task-level agenda items within the work package
- Direct link to the scheduling poll
- Human-readable summary of all proposed time slots
- Response deadline (end of current week)
- Sender signature

The draft was saved to the drafts folder (not sent).

---

## Tools & Integrations Used

| Tool | Purpose |
|---|---|
| `get_current_time` | Anchor relative date references |
| `semos_agentura_email__search_emails` | Find kickoff event info and WP contributor addresses |
| `semos_agentura_email__read_email` | Extract full recipient list and thread context |
| `semos_agentura_email__list_events` | Retrieve calendar events over the relevant period |
| `semos_agentura_email__free_slots` | Compute available time windows per day |
| `survey_agent__deploy_survey` | Create and activate the scheduling poll |
| `survey_agent__delete_survey` | Clean up outdated poll versions |
| `semos_agentura_email__create_draft` | Compose and save the invitation email |

---

## Key Capabilities Demonstrated

- **Parallel tool execution** – multiple searches and lookups fired simultaneously
- **Cross-agent orchestration** – email, calendar, survey, and file system agents combined in one workflow
- **Constraint reasoning** – applying overlapping rules (free windows, excluded dates, no-lunch policy) to generate valid options
- **Context extraction from emails** – identifying attendees, dates, and topics from existing correspondence
- **End-to-end automation** – from raw user request to deployed poll + ready-to-send email draft
