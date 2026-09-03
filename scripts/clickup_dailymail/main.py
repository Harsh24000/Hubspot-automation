"""
ClickUp Daily Updates Email Reporter — morning run.

Scans every task/subtask across the workspace for comments matching
"est Xhr" (or Xmin/Xh/etc.) posted TODAY, groups them by assignee, sums each
person's estimated hours for the day, and emails a formatted report.

Also writes morning_snapshot.json, which the evening report reads later the
same day to compare planned vs. actual work.

Does NOT send anything on Sunday — see the guard at the top of main().
"""

import json
import os
import re
import smtplib
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List

from clickup_client import ClickUpClient
from email_template import generate_email_html
import summary

SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "morning_snapshot.json")
IST = timezone(timedelta(hours=5, minutes=30))

# Matches: est 4hr / est 4hrs / est 4h / est 30min / est 30mins / est 1.5hr
# Captures the number and the unit separately so we can turn it into a real
# hour value for the per-person daily total, not just detect that it exists.
EST_PATTERN = re.compile(
    r"\best\s*:?\s*(\d+(?:\.\d+)?)\s*(hrs?|hours?|h|mins?|minutes?|m)\b",
    re.IGNORECASE,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _today_range_ms() -> tuple:
    """(start_ms, end_ms) for today, midnight to 23:59:59 IST."""
    now = datetime.now(IST)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _is_today(timestamp_ms: int) -> bool:
    # Compare in IST, not UTC — the team works in IST, so a comment posted
    # at, say, 1am IST is genuinely "today" for them even though it's still
    # "yesterday" in UTC (UTC's day only rolls over at 5:30am IST).
    comment_date = datetime.fromtimestamp(timestamp_ms / 1000, tz=IST).date()
    return comment_date == datetime.now(IST).date()


def _parse_est_hours(comment_text: str) -> float:
    """Turn 'est 2hr' / 'est 90min' / 'est 1.5h' etc. into a decimal-hours float."""
    match = EST_PATTERN.search(comment_text)
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith('h'):
        return number
    return number / 60.0  # minutes -> hours


# Most specific first, so a "Client Name" field wins over a "Client Type" one.
_CLIENT_FIELD_HINTS = ("client name", "client_name", "client")


def _custom_field_text(field: dict) -> str:
    """Decode one ClickUp custom field to display text, or '' if it has none.

    Same decoding as _get_product_module, but returns '' instead of 'N/A' —
    an empty string is the signal to hide the badge entirely.
    """
    value = field.get("value")
    if value is None or value == "":
        return ""

    field_type = field.get("type", "")

    if field_type == "drop_down":
        options = field.get("type_config", {}).get("options", [])
        for opt in options:
            if str(opt.get("orderindex", "")) == str(value) or opt.get("id") == str(value):
                return (opt.get("name") or "").strip()
        try:
            return (options[int(value)].get("name") or "").strip()
        except (IndexError, ValueError, TypeError, AttributeError):
            return str(value).strip()

    if field_type == "labels":
        options = field.get("type_config", {}).get("options", [])
        matched = [o.get("name", "") for o in options if o.get("id") in (value or [])]
        return ", ".join(n for n in matched if n)

    if isinstance(value, dict):
        return str(value.get("name") or value.get("value") or "").strip()

    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("name") or item.get("username") or "").strip())
            else:
                parts.append(str(item).strip())
        return ", ".join(p for p in parts if p)

    return str(value).strip()


def _get_client_name(task: dict) -> str:
    """The task's Client Name custom field, or '' when the task has none."""
    fields = task.get("custom_fields") or []
    for hint in _CLIENT_FIELD_HINTS:
        for field in fields:
            if hint in (field.get("name") or "").lower():
                text = _custom_field_text(field)
                if text and text.upper() != "N/A":
                    return text
    return ""


def _get_product_module(task: dict) -> str:
    """
    Extract 'Product Module' from ClickUp custom fields.
    Handles text, drop_down, and label field types.
    """
    for field in task.get("custom_fields", []):
        name_lower = field.get("name", "").lower()
        if "module" not in name_lower and "product" not in name_lower:
            continue

        value = field.get("value")
        if value is None:
            continue

        field_type = field.get("type", "")

        if field_type == "drop_down":
            options = field.get("type_config", {}).get("options", [])
            # value is the option id (string) or index (int/str)
            for opt in options:
                if str(opt.get("orderindex", "")) == str(value) or opt.get("id") == str(value):
                    return opt.get("name", str(value))
            # fallback: try direct integer index
            try:
                return options[int(value)]["name"]
            except (IndexError, ValueError, TypeError):
                return str(value)

        if field_type in ("text", "short_text", "url"):
            return str(value).strip() or "N/A"

        if field_type == "labels":
            # value is a list of option ids
            options = field.get("type_config", {}).get("options", [])
            matched = [o["name"] for o in options if o.get("id") in (value or [])]
            return ", ".join(matched) if matched else "N/A"

        # Generic fallback
        return str(value).strip() or "N/A"

    return "N/A"


def _task_number(task: dict) -> str:
    custom_id = task.get("custom_id") or task.get("id", "")
    return f"#{custom_id}" if custom_id and not custom_id.startswith("#") else custom_id


# ── Core logic ───────────────────────────────────────────────────────────────

def process_task(client: ClickUpClient, task: dict, person_tasks: Dict[str, List[dict]]) -> None:
    """Check a task's comments; if any match est pattern from today, add to person_tasks."""
    try:
        comments = client.get_comments(task["id"])
    except Exception as exc:
        print(f"    [warn] Could not fetch comments for task {task['id']}: {exc}")
        return

    for comment in comments:
        timestamp = int(comment.get("date", 0))
        if not _is_today(timestamp):
            continue

        comment_text = comment.get("comment_text", "").strip()
        if not comment_text or not EST_PATTERN.search(comment_text):
            continue

        assignees = task.get("assignees", [])
        if not assignees:
            assignees = [{"username": "Unassigned", "email": "", "profilePicture": None}]

        product_module = _get_product_module(task)
        client_name = _get_client_name(task)
        est_hours = _parse_est_hours(comment_text)

        for assignee in assignees:
            name = (
                assignee.get("username")
                or assignee.get("email", "")
                or "Unknown"
            )
            person_tasks[name].append({
                "task_id": task.get("id", ""),
                "task_number": _task_number(task),
                "task_name": task.get("name", "Untitled"),
                "comment": comment_text,
                "est_hours": est_hours,
                "product_module": product_module,
                "client_name": client_name,
                "task_url": task.get("url", "#"),
                "avatar": assignee.get("profilePicture"),
                "user_id": assignee.get("id"),
            })


def collect_tasks(client: ClickUpClient, team_id: str) -> Dict[str, List[dict]]:
    person_tasks: Dict[str, List[dict]] = defaultdict(list)

    spaces = client.get_spaces(team_id)
    print(f"Found {len(spaces)} space(s)")

    for space in spaces:
        print(f"  Space: {space['name']}")
        lists = client.get_all_lists(space["id"])
        print(f"    {len(lists)} list(s)")

        for lst in lists:
            print(f"    → List: {lst['name']}", end="", flush=True)
            tasks = client.get_tasks(lst["id"])
            print(f" ({len(tasks)} tasks)")

            for task in tasks:
                process_task(client, task, person_tasks)
                time.sleep(0.05)

    return person_tasks


def compute_person_totals(person_tasks: Dict[str, List[dict]]) -> Dict[str, float]:
    """Sum each person's estimated hours across all their tasks today."""
    return {
        name: sum(t.get("est_hours", 0.0) for t in tasks)
        for name, tasks in person_tasks.items()
    }


def fetch_person_logged_hours(client: ClickUpClient, team_id: str, person_tasks: Dict[str, List[dict]]) -> Dict[str, float]:
    """
    Actual time LOGGED today per person, via ClickUp's time tracker — distinct
    from the estimated hours above, which come from parsing "est Xhr" comment
    text. This queries ClickUp's time_entries endpoint directly.
    """
    # Get each person's ClickUp user_id from whatever tasks we already have
    # for them (assignee id was captured in process_task); fall back to a
    # full workspace member lookup for anyone missing it.
    person_user_ids: Dict[str, int] = {}
    for name, tasks in person_tasks.items():
        for t in tasks:
            if t.get("user_id"):
                person_user_ids[name] = t["user_id"]
                break

    missing = [name for name in person_tasks if name not in person_user_ids]
    if missing:
        try:
            members = client.get_workspace_members(team_id)
            by_username = {m.get("username"): m.get("id") for m in members if m.get("username")}
            for name in missing:
                if name in by_username:
                    person_user_ids[name] = by_username[name]
        except Exception as exc:
            print(f"  [warn] Could not fetch workspace members for logged-time lookup: {exc}")

    start_ms, end_ms = _today_range_ms()
    logged_hours: Dict[str, float] = {}
    for name, uid in person_user_ids.items():
        try:
            entries = client.get_time_entries(team_id, start_ms, end_ms, assignee_id=uid)
            total_ms = sum(int(e.get("duration", 0)) for e in entries if int(e.get("duration", 0)) > 0)
            logged_hours[name] = total_ms / 3600000.0  # ms -> hours
        except Exception as exc:
            print(f"  [warn] Could not fetch time entries for {name}: {exc}")
            logged_hours[name] = 0.0

    return logged_hours


# ── Email ────────────────────────────────────────────────────────────────────

def _parse_addresses(raw) -> list:
    """Split a recipient string on commas, semicolons OR whitespace/newlines.

    GitHub secrets are frequently pasted one address per line. A raw newline
    inside a To/Cc header makes Python 3.11 raise
    HeaderWriteError: folded header contains newline
    and the whole send fails after all the work is already done. Splitting on
    whitespace too makes the script tolerant of however the secret is entered.
    """
    if not raw:
        return []
    return [a.strip() for a in re.split(r"[,;\s]+", str(raw)) if a.strip()]


def send_email(html_content: str, subject: str, cfg: dict) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    to_list = _parse_addresses(cfg.get("to"))
    cc_list = _parse_addresses(cfg.get("cc"))
    if not to_list:
        raise SystemExit("No recipients configured — check the EMAIL_TO secret.")

    # Join with ", " ourselves. Never assign the raw secret to a header: if it
    # contains a newline, Python refuses to write the message at all.
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)

    msg.attach(MIMEText(html_content, "html"))

    recipients = to_list + cc_list

    print(f"Sending email via Gmail SMTP to: {', '.join(recipients)}")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(cfg["from"], cfg["app_password"])
        server.sendmail(cfg["from"], recipients, msg.as_string())
    print("Email sent successfully!")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    # No mail on Sundays — check this in IST (the team's timezone), not
    # whatever timezone the GitHub Actions runner happens to be in (UTC).
    today_ist = datetime.now(IST)
    if today_ist.weekday() == 6:  # Monday=0 ... Sunday=6
        print(f"Today ({today_ist.strftime('%A, %d %b %Y')}) is Sunday — skipping, no email sent.")
        return

    # Load config from environment variables
    # Support both naming conventions
    api_token = (os.environ.get("CLICKUP_TOKEN") or os.environ.get("CLICKUP_API_TOKEN", "")).strip()
    team_id = (os.environ.get("CLICKUP_WORKSPACE") or os.environ.get("CLICKUP_TEAM_ID", "")).strip()
    gmail_from = (os.environ.get("EMAIL_FROM") or os.environ.get("GMAIL_FROM", "")).strip()
    gmail_password = (os.environ.get("EMAIL_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD", "")).strip()
    email_to = os.environ.get("EMAIL_TO", "").strip()
    email_cc = os.environ.get("EMAIL_CC", "").strip()  # optional

    if not api_token:
        print("ERROR: Missing required environment variable: CLICKUP_TOKEN")
        sys.exit(1)

    print(f"=== ClickUp Daily Updates — {today_ist.strftime('%A, %d %b %Y')} ===")
    client = ClickUpClient(api_token)

    # Auto-detect team_id if not explicitly set — must happen BEFORE the
    # missing-vars check below, otherwise a missing CLICKUP_WORKSPACE always
    # fails even though auto-detection could have resolved it.
    if not team_id:
        try:
            team_id = client.get_team_id()
            print(f"Auto-detected team ID: {team_id}")
        except Exception as exc:
            print(f"ERROR: Could not auto-detect ClickUp team ID: {exc}")
            sys.exit(1)

    missing = [k for k, v in {
        "EMAIL_FROM": gmail_from,
        "EMAIL_PASSWORD": gmail_password,
        "EMAIL_TO": email_to,
    }.items() if not v]

    if missing:
        print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    person_tasks = collect_tasks(client, team_id)

    if not person_tasks:
        print("No 'est Xhr' comments found today — no email sent.")
        return

    person_totals = compute_person_totals(person_tasks)

    print("Fetching actual logged time for today...")
    person_logged_hours = fetch_person_logged_hours(client, team_id, person_tasks)

    total_tasks = sum(len(t) for t in person_tasks.values())
    grand_total_hours = sum(person_totals.values())
    grand_logged_hours = sum(person_logged_hours.values())
    print(f"\nSummary: {len(person_tasks)} people, {total_tasks} tasks, "
          f"{grand_total_hours:.1f}h estimated total, {grand_logged_hours:.1f}h logged total")
    for name, tasks in sorted(person_tasks.items()):
        print(f"  {name}: {len(tasks)} task(s), {person_totals[name]:.1f}h estimated, "
              f"{person_logged_hours.get(name, 0.0):.1f}h logged")

    report_date = date.today()
    print("Building key takeaway...")
    takeaway = summary.key_takeaway(
        summary.morning_facts(person_tasks, person_totals, person_logged_hours))
    print(f"  {takeaway}")

    html = generate_email_html(person_tasks, report_date, person_totals=person_totals,
                                person_logged_hours=person_logged_hours,
                                key_takeaway=takeaway)
    subject = f"Daily Updates — {today_ist.strftime('%A, %b %d')}"

    send_email(html, subject, {
        "from": gmail_from,
        "app_password": gmail_password,
        "to": email_to,
        "cc": email_cc,
    })

    # Save snapshot for evening script
    snapshot = {
        "date": str(report_date),
        "people": {name: tasks for name, tasks in person_tasks.items()},
    }
    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(snapshot, f)
    print(f"Morning snapshot saved → {SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
