# NirogGyan Automation — Setup & Handover Guide

This guide covers everything needed to take ownership of this project from scratch: getting all API keys, setting up GitHub, configuring secrets, running tests, and setting up the cron schedule.

---

## Prerequisites

- A GitHub account with access to the `aksharReddy/hubspot-automation` repository
- Python 3.11+ installed locally (for test runs)
- `git` installed locally
- `gh` CLI installed (optional, for triggering workflows from terminal) — https://cli.github.com

---

## Step 1 — Clone the Repository

```bash
git clone https://github.com/aksharReddy/hubspot-automation.git
cd hubspot-automation
```

---

## Step 2 — API Keys You Need

You need **8 secrets** total. Here's where to get each one:

---

### 1. `HUBSPOT_TOKEN`
**What it is:** HubSpot Private App token. Gives access to the CRM (companies, deals, meetings, contacts).

**How to get it:**
1. Log in to HubSpot → Settings (gear icon, top right)
2. Integrations → Private Apps → Create a private app
3. Give it a name (e.g. "Daily Automation")
4. Under **Scopes**, enable:
   - `crm.objects.companies.read`
   - `crm.objects.deals.read`
   - `crm.objects.contacts.read`
   - `crm.objects.meetings.read`
   - `crm.schemas.companies.read`
5. Click Create → copy the token shown (starts with `pat-`)

> **Note:** HubSpot Starter plan is sufficient. Sales Hub Professional is NOT required.

---

### 2. `GMAIL_ADDRESS`
**What it is:** The Gmail address used to send the report.

**Value:** `kambampati.harshith@gmail.com` (or whichever Gmail account will send)

---

### 3. `GMAIL_APP_PASSWORD`
**What it is:** A 16-character app password for Gmail SMTP. **Not** your regular Gmail password.

**How to get it:**
1. Go to your Google Account → Security
2. Enable **2-Step Verification** (required)
3. Search for "App Passwords" → Create one for "Mail" / "Other"
4. Copy the 16-character password shown (no spaces)

---

### 4. `RECIPIENT_EMAIL`
**What it is:** The primary recipient's email address.

**Value:** `kondapuram.reddy22b@iiitg.ac.in`

> The second recipient (`joyneel@niroggyan.com`) is hardcoded in `daily_report_v2.py` in the `RECIPIENTS` list. To change it, edit line ~1330 in the script.

---

### 5. `APOLLO_API_KEY`
**What it is:** Apollo.io API key for fetching email sequence analytics.

**How to get it:**
1. Log in to Apollo.io → Settings → Integrations → API
2. Copy the API key

---

### 6. `GOOGLE_SERVICE_ACCOUNT_JSON`
**What it is:** A Google Cloud service account JSON that has read access to `Shweta@niroggyan.com`'s Google Calendar.

**How to get it:**
1. Go to https://console.cloud.google.com
2. Create or select a project
3. Enable the **Google Calendar API** (APIs & Services → Enable APIs)
4. Go to IAM & Admin → Service Accounts → Create Service Account
5. Give it a name, click Create
6. Click the service account → Keys → Add Key → JSON → download the file
7. Open the JSON file → copy the **entire contents** as the secret value
8. Share `Shweta@niroggyan.com`'s Google Calendar with the service account email (found inside the JSON as `client_email`) — give it "See all event details" permission

> The secret value is a large JSON block like `{"type": "service_account", "project_id": "...", ...}`. Paste the whole thing as the secret.

---

### 7. `CLICKUP_TOKEN`
**What it is:** ClickUp personal API token for fetching Customer Support tasks.

**How to get it:**
1. Log in to ClickUp → Profile (bottom left) → Apps
2. Under "API Token" → copy it

> The script uses list ID `901615411023` (Customer Support list). If ClickUp workspace changes, find the new list ID by going to the list in ClickUp → right-click → Copy link → the number at the end of the URL is the list ID. Update `CLICKUP_LIST_ID` in `daily_report_v2.py`.

---

### 8. `GROQ_API_KEY`
**What it is:** Groq API key for the AI morning briefing (uses Llama 3.3 70B).

**How to get it:**
1. Go to https://console.groq.com
2. Sign in → API Keys → Create API Key
3. Copy the key (starts with `gsk_`)

> Groq has a generous free tier. No payment needed for this usage level.

---

### 9. `DISCORD_WEBHOOK` (for news_report.py only)
**What it is:** Discord incoming webhook URL for the #meetings or relevant channel.

**How to get it:**
1. In Discord, go to the channel → Edit Channel → Integrations → Webhooks → New Webhook
2. Copy the webhook URL (starts with `https://discord.com/api/webhooks/...`)

> Only needed for the news report script. Not used in the daily report.

---

## Step 3 — Add Secrets to GitHub

1. Go to `https://github.com/aksharReddy/hubspot-automation`
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret** for each of the 8/9 secrets above
4. Name must match exactly (case sensitive): `HUBSPOT_TOKEN`, `GMAIL_ADDRESS`, etc.

---

## Step 4 — Set Up the Cron Schedule (Automated Daily Run)

The daily report workflow currently only runs on `workflow_dispatch` (manual trigger). To make it run automatically every morning at 9:00 AM IST:

1. Open `.github/workflows/daily_report_v2.yml`
2. Change the `on:` section from:
```yaml
on:
  workflow_dispatch:
```
To:
```yaml
on:
  workflow_dispatch:
  schedule:
    - cron: '30 3 * * 1-6'   # 9:00 AM IST (UTC+5:30) = 3:30 AM UTC, Mon-Sat
```
3. Save, commit, and push:
```bash
git add .github/workflows/daily_report_v2.yml
git commit -m "Add cron schedule for daily report"
git push origin main
```

> **Important:** GitHub cron runs on UTC. IST is UTC+5:30, so 9:00 AM IST = 3:30 AM UTC = `30 3 * * *`. The `1-6` means Monday through Saturday (1=Mon, 6=Sat). Remove `1-6` for 7 days a week.

> **Note:** GitHub free-tier crons can be delayed by up to 15–30 minutes under heavy load. This is normal.

For the **news report** (bi-weekly), add:
```yaml
schedule:
  - cron: '0 4 1,15 * *'   # 9:30 AM IST on 1st and 15th of each month
```

---

## Step 5 — Test Run (Manual Trigger)

### Option A — GitHub UI
1. Go to `https://github.com/aksharReddy/hubspot-automation/actions`
2. Click **NirogGyan Daily Pulse V2** in the left sidebar
3. Click **Run workflow** → **Run workflow**
4. Watch the logs — should complete in ~60 seconds
5. Check both email inboxes

### Option B — GitHub CLI (from terminal)
```bash
gh workflow run daily_report_v2.yml --repo aksharReddy/hubspot-automation
gh run list --repo aksharReddy/hubspot-automation --workflow=daily_report_v2.yml --limit=3
```

### Option C — Local test run
```bash
cd hubspot-automation
pip install requests fpdf2 google-auth google-api-python-client

export HUBSPOT_TOKEN="pat-..."
export GMAIL_ADDRESS="kambampati.harshith@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export RECIPIENT_EMAIL="your@email.com"
export APOLLO_API_KEY="..."
export GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
export CLICKUP_TOKEN="pk_..."
export GROQ_API_KEY="gsk_..."

python scripts/daily_report_v2.py
```

---

## Step 6 — Making Code Changes

1. Edit the script in your code editor
2. Test locally (Step 5 Option C) if possible
3. Push to GitHub:
```bash
git add scripts/daily_report_v2.py
git commit -m "Describe what you changed"
git push origin main
```
4. Trigger a test run (Step 5 Option A or B) to verify

> The workflow always pulls the latest code from `main` when it runs. Push before triggering.

---

## Common Changes & Where to Make Them

| What you want to change | Where |
|---|---|
| Add/remove an email recipient | `daily_report_v2.py` → `RECIPIENTS` list (~line 1330) |
| Change calendar owner | `daily_report_v2.py` → `fetch_calendar_week()` → `calendarId=` |
| Exclude a recurring meeting from the report | `daily_report_v2.py` → `EXCLUDED_TITLES` set (line ~59) |
| Change ClickUp list | `daily_report_v2.py` → `CLICKUP_LIST_ID` (line ~35) |
| Change cron time | `.github/workflows/daily_report_v2.yml` → `cron:` line |
| Add AI context (e.g. Apollo data to briefing) | `daily_report_v2.py` → `build_ai_briefing()` function |

---

## Troubleshooting

**Report didn't arrive:**
- Check GitHub Actions logs: `Actions` tab → click the failed run → expand each step
- Check spam/junk/Promotions folder
- If logs show "Email sent." but not received — the SMTP server accepted it, so it's a delivery/spam issue on the recipient's end

**`AI unavailable` in the morning briefing:**
- Groq API key may be expired or invalid
- Check https://console.groq.com → usage/quota

**Calendar shows no events:**
- Service account may not have been granted access to the calendar
- Verify: open Google Calendar → `Shweta@niroggyan.com` → Settings → Share with specific people → check the service account `client_email` is listed

**Apollo shows wrong numbers:**
- The script uses `unique_delivered`, `unique_opened`, `unique_bounced` from Apollo's campaign detail. These match the "Overall Statistics" box in Apollo UI exactly. If Apollo UI numbers change, the report will reflect that on the next run.

**HubSpot meetings missing:**
- Meetings are fetched for the current Mon–Sat week in IST. Meetings outside this window won't appear.
- Check that the HubSpot Private App token has `crm.objects.meetings.read` scope.

**Pushed code but workflow still uses old version:**
- Always push to `main` before triggering. The workflow runs `git checkout` at runtime and uses whatever is in `main`.

---

## Repository Structure

```
hubspot-automation/
├── scripts/
│   ├── daily_report_v2.py      ← Main daily report (use this one)
│   ├── daily_report.py         ← Old V1 report (kept for reference, not scheduled)
│   ├── news_report.py          ← Bi-weekly client news brief
│   ├── hs_email_probe.py       ← Dev probe script (not production)
│   └── hs_company_probe.py     ← Dev probe script (not production)
├── .github/
│   └── workflows/
│       ├── daily_report_v2.yml ← Production workflow
│       ├── daily_report.yml    ← Old V1 workflow (manual only, disabled)
│       ├── news_report.yml     ← News brief workflow
│       ├── hs_email_probe.yml  ← Dev probe workflow
│       └── hs_company_probe.yml
├── data/
│   └── seen_news.json          ← Tracks which news articles have already been reported
├── DOCUMENTATION.md            ← This project's full feature documentation
└── SETUP_GUIDE.md              ← This file
```

---

## Key Contacts for Access

| What you need | Who has it |
|---|---|
| HubSpot admin access (to create/manage Private Apps) | Sir (HubSpot admin) |
| Apollo.io access | Sir / team admin |
| Google Workspace admin (to grant Calendar access) | Sir / Google admin |
| ClickUp workspace admin | Sir / team admin |
| GitHub repository access | aksharReddy (current owner) |
| Discord server admin (for webhook) | Sir |
