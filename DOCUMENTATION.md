# NirogGyan Automation — System Documentation

**What this is:** An automated reporting system that pulls data from five external services every morning and emails a formatted Daily Pulse report to the leadership team. A separate bi-weekly News Brief sends client news signals to Discord.

---

## Scripts Overview

| Script | Purpose | Trigger |
|---|---|---|
| `scripts/daily_report_v2.py` | Daily Pulse email — all CRM, calendar, tickets, sequences | Daily at 9:00 AM IST (cron) or manual |
| `scripts/news_report.py` | Client News Brief PDF — Google News signals for all customers | Manual / bi-weekly |

---

## Daily Pulse Report — What's Inside

The report is sent as an **HTML email** with a **PDF attachment** to:
- `kondapuram.reddy22b@iiitg.ac.in`
- `joyneel@niroggyan.com`

### 1. Header Stats Bar
Four numbers shown at the top:
- **Open Deals** — total non-closed deals in HubSpot
- **Open Tickets** — open ClickUp tasks in the Customer Support list
- **Meetings Today** — HubSpot meetings scheduled for today in IST
- **Active This Month** — companies contacted in the last 30 days

---

### 2. AI Morning Briefing
- **Source:** Groq API (model: `llama-3.3-70b-versatile`)
- **What it does:** Reads today's calendar, HubSpot meetings, ClickUp overdue/urgent tickets, and top active deals, then writes 4–6 bullet points interpreting what needs action, what's risky, and what to follow up on.
- **Format:** Plain bullet points with an amber left-border box.

---

### 3. Shweta's Calendar — This Week
- **Source:** Google Calendar API (`Shweta@niroggyan.com` calendar)
- **What it shows:** All calendar events for the current Mon–Sat week, grouped by day. Today's row is highlighted blue with a "TODAY" badge. Each event shows: title, time (IST), external attendees.
- **Filtered out:** "Niro Scrum Call" and "Weekend Update and Sprint Planning" are excluded from the calendar view.
- **Google Meet badge:** Events with a Meet link show a blue "Meet" badge.

---

### 4. HubSpot Meetings — This Week
- **Source:** HubSpot CRM API (`/crm/v3/objects/meetings/search`)
- **What it shows:** All meetings in HubSpot for the current week, grouped by day. Today's meetings are highlighted. Each meeting shows:
  - **Title** — meeting name from HubSpot
  - **Lead type badge** (purple) — contact's lifecycle stage (Lead, MQL, SQL, Customer)
  - **Source badge** (green = Inbound / grey = Manual) — `BIDIRECTIONAL_SYNC` means the prospect booked via Cal.id link (inbound); `CRM_UI` means the team created it manually
  - **Company** — linked company from HubSpot
  - **Time** in IST
  - **Phone** — contact's mobile/phone number (or parsed from meeting body if not in contact record)
  - **Agenda** (italic sub-row) — extracted from the "Additional notes" field in the meeting booking form

---

### 5. Active Pipeline Deals
- **Source:** HubSpot CRM API (`/crm/v3/objects/deals`)
- **What it shows:** Up to 10 open deals sorted by most recently active (based on notes_last_updated, notes_last_contacted, last modified). Shows deal name, company, days since last activity.

---

### 6. Active Client Conversations This Month
- **Source:** HubSpot CRM API (`/crm/v3/objects/companies`)
- **What it shows:** Up to 10 companies that have had contact in the last 30 days, sorted by most recently contacted. Shows company name, days since last contact, last logged call date, lifecycle stage.

---

### 7. Customer Support — Active Tickets
- **Source:** ClickUp API (`/api/v2/list/901615411023/task`)
- **What it shows:** All open tasks from the Customer Support list in ClickUp. Sorted with overdue first, then by due date. Each ticket shows: task name, status badge (In Progress / other), priority badge (URGENT / HIGH / NORMAL), due date with overdue warning if applicable, assigned team members.
- **Overdue count** shown as a red badge in the section header.

---

### 8. Email Sequences (Apollo)
- **Source:** Apollo.io API
- **What it shows:** All non-archived email sequences. For each sequence:
  - Name, Active/Paused status, current step
  - Per-step table: Delivered, Opened, Open %, Replied, Reply %, Bounced
  - Names of people who replied (shown as italic sub-row)
- **How numbers are calculated:** Campaign-level `unique_delivered`, `unique_opened`, `unique_bounced` are fetched from Apollo's campaign detail endpoint (these exactly match what Apollo UI shows). These are then distributed across steps proportionally based on how many completed messages each step has. Replies are detected from individual messages.

---

## News Brief Report — What's Inside

A separate PDF sent to Discord.

- **Source:** HubSpot (customer companies list) + Google News RSS + Groq
- **What it does:**
  1. Fetches all companies with `lifecyclestage = customer` from HubSpot
  2. For each company, searches Google News RSS for articles from the last 14 days
  3. Feeds headlines to Groq to determine if the news is relevant to NirogGyan and classify it as: **RISK**, **OPPORTUNITY**, or **NEUTRAL**. Irrelevant results are skipped.
  4. Generates a global AI summary across all signals
  5. Builds a PDF and posts it to the NirogGyan Discord server
- **Deduplication:** Article URLs are saved to `data/seen_news.json` so they don't appear in the next run.

---

## Data Flow — How APIs Are Called

```
Run starts
│
├── HubSpot API
│   ├── GET /crm/v3/objects/companies    → all companies (paginated)
│   ├── GET /crm/v3/objects/deals        → all deals (paginated)
│   ├── POST /crm/v3/objects/meetings/search → week's meetings
│   ├── POST /crm/v3/associations/meetings/companies/batch/read → company per meeting
│   ├── POST /crm/v3/associations/meetings/contacts/batch/read  → contacts per meeting
│   └── POST /crm/v3/objects/contacts/batch/read → phone + lifecycle stage per contact
│
├── Google Calendar API
│   └── events().list(calendarId='Shweta@niroggyan.com', week range)
│
├── ClickUp API
│   └── GET /api/v2/list/901615411023/task
│
├── Apollo API
│   ├── POST /api/v1/emailer_campaigns/search → all sequences
│   ├── GET  /api/v1/emailer_campaigns/{id}   → per-sequence stats (unique_delivered etc.)
│   └── GET  /api/v1/emailer_messages/search  → individual messages per sequence
│
├── Groq API
│   └── POST chat/completions (llama-3.3-70b-versatile) → AI morning briefing
│
└── Gmail SMTP
    └── Send HTML email + PDF to both recipients
```

---

## Key Constants in the Code

| Constant | Value | What it does |
|---|---|---|
| `CLICKUP_LIST_ID` | `901615411023` | Customer Support list ID in ClickUp |
| `EXCLUDED_TITLES` | `{'niro scrum call', 'weekend update and sprint planning'}` | Internal calendar events hidden from report |
| `RECIPIENTS` | `[RECIPIENT_EMAIL, 'joyneel@niroggyan.com']` | Who gets the daily email |
| `WEEK_START` / `WEEK_END` | Mon 00:00 IST → Sat 23:59 IST | Window for meetings shown in report |

---

## Timezone
All times are shown in **IST (UTC+5:30)**. The script runs in UTC on GitHub Actions and converts all timestamps.

---

## Report Format
- **HTML email:** Rendered inline in Gmail/Outlook, max width 720px, responsive.
- **PDF attachment:** Generated with `fpdf2`, A4 landscape-friendly, same sections as the email.
