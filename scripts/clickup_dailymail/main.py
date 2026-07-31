"""
ClickUp Daily Standup Email Reporter
Scans ClickUp tasks/subtasks for comments matching "est Xhr" posted today,
then emails a formatted report grouped by assignee.
"""

import json
import os
import re
import smtplib
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List

SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "morning_snapshot.json")

from clickup_client import ClickUpClient, this_week_ms
from email_template import generate_email_html

# Matches: est 4hr / est 4hrs / est 4h / est 30min / est 30mins / est 1.5hr
EST_PATTERN = re.compile(
    r"\best\s*:?\s*\d+(\.\d+)?\s*(hr|hrs|h|hour|hours|min|mins|minutes?|m)\b",
    re.IGNORECASE,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_today(timestamp_ms: int) -> bool:
    comment_date = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date()
    return comment_date == datetime.now(timezone.utc).date()


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
                "product_module": product_module,
                "task_url": task.get("url", "#"),
                "avatar": assignee.get("profilePicture"),
                "user_id": assignee.get("id"),
            })


def collect_tasks(client: ClickUpClient, team_id: str) -> Dict[str, List[dict]]:
    person_tasks: Dict[str, List[dict]] = defaultdict(list)

    week_start_ms, week_end_ms = this_week_ms()

    spaces = client.get_spaces(team_id)
    print(f"Found {len(spaces)} space(s)")

    for space in spaces:
        print(f"  Space: {space['name']}")
        lists = client.get_all_lists(space["id"])
        print(f"    {len(lists)} list(s)")

        for lst in lists:
            print(f"    → List: {lst['name']}", end="", flush=True)
            tasks = client.get_tasks(lst["id"], week_start_ms, week_end_ms)
            print(f" ({len(tasks)} tasks)")

            for task in tasks:
                process_task(client, task, person_tasks)
                time.sleep(0.05)

    return person_tasks


# ── Email ────────────────────────────────────────────────────────────────────

def send_email(html_content: str, subject: str, cfg: dict) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    if cfg.get("cc"):
        msg["Cc"] = cfg["cc"]

    msg.attach(MIMEText(html_content, "html"))

    # cfg["to"] / cfg["cc"] may be a single address or a comma-separated list —
    # split into a real list so every address actually gets the email, not
    # just the first one.
    to_list = [addr.strip() for addr in cfg["to"].split(",") if addr.strip()]
    cc_list = [addr.strip() for addr in cfg.get("cc", "").split(",") if addr.strip()]
    recipients = to_list + cc_list

    print(f"Sending email via Gmail SMTP to: {', '.join(recipients)}")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(cfg["from"], cfg["app_password"])
        server.sendmail(cfg["from"], recipients, msg.as_string())
    print("Email sent successfully!")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
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

    print(f"=== ClickUp Daily Updates — {date.today()} ===")
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

    total_tasks = sum(len(t) for t in person_tasks.values())
    print(f"\nSummary: {len(person_tasks)} people, {total_tasks} tasks")
    for name, tasks in sorted(person_tasks.items()):
        print(f"  {name}: {len(tasks)} task(s)")

    report_date = date.today()
    html = generate_email_html(person_tasks, report_date)
    subject = f"Daily Updates — {report_date.strftime('%A, %b %d')}"

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
