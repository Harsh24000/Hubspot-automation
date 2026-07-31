"""
ClickUp Evening Report
Reads morning_snapshot.json (saved by main.py), fetches today's time entries,
then emails per-person planned vs actual with any unplanned tasks that came up.
"""

import json
import os
import smtplib
import sys
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List

from clickup_client import ClickUpClient
from email_template_evening import generate_email_html

SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "morning_snapshot.json")
IST = timezone(timedelta(hours=5, minutes=30))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today_range_ms() -> tuple:
    """Return (start_ms, end_ms) for today in IST (midnight to 23:59:59)."""
    now = datetime.now(IST)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _format_duration(ms: int) -> str:
    total_minutes = int(ms) // 60000
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    elif hours:
        return f"{hours}h"
    elif minutes:
        return f"{minutes}m"
    return "0m"


# ── Core logic ────────────────────────────────────────────────────────────────

def load_snapshot() -> dict:
    if not os.path.exists(SNAPSHOT_PATH):
        print("ERROR: morning_snapshot.json not found — morning script must run first.")
        sys.exit(1)
    with open(SNAPSHOT_PATH) as f:
        data = json.load(f)
    if data.get("date") != str(date.today()):
        print(f"ERROR: Snapshot is from {data.get('date')}, not today ({date.today()}).")
        sys.exit(1)
    return data


def _get_product_module(task: dict) -> str:
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
            for opt in options:
                if str(opt.get("orderindex", "")) == str(value) or opt.get("id") == str(value):
                    return opt.get("name", str(value))
            try:
                return options[int(value)]["name"]
            except (IndexError, ValueError, TypeError):
                return str(value)
        if field_type in ("text", "short_text", "url"):
            return str(value).strip() or "N/A"
        if field_type == "labels":
            options = field.get("type_config", {}).get("options", [])
            matched = [o["name"] for o in options if o.get("id") in (value or [])]
            return ", ".join(matched) if matched else "N/A"
        return str(value).strip() or "N/A"
    return "N/A"


def build_report(client: ClickUpClient, snapshot: dict, time_entries: List[dict]) -> Dict[str, dict]:
    # Group time entries: username → task_id → total ms logged today
    user_task_ms: dict = defaultdict(lambda: defaultdict(int))
    entry_task_meta: dict = {}

    for entry in time_entries:
        task = entry.get("task") or {}
        task_id = task.get("id")
        if not task_id:
            continue
        duration = int(entry.get("duration", 0))
        if duration <= 0:
            continue
        user = entry.get("user", {}).get("username", "Unknown")
        user_task_ms[user][task_id] += duration
        if task_id not in entry_task_meta:
            entry_task_meta[task_id] = {
                "task_id": task_id,
                "task_name": task.get("name", "Unknown"),
                "task_number": f"#{task_id}",
                "task_url": task.get("url", "#"),
                "product_module": "N/A",
                "avatar": entry.get("user", {}).get("profilePicture"),
            }

    # Fetch product module for unplanned tasks
    planned_task_ids = {t["task_id"] for tasks in snapshot["people"].values() for t in tasks}
    for task_id, meta in entry_task_meta.items():
        if task_id not in planned_task_ids:
            try:
                full_task = client._get(f"task/{task_id}")
                meta["product_module"] = _get_product_module(full_task)
                meta["task_name"] = full_task.get("name", meta["task_name"])
                meta["task_url"] = full_task.get("url", meta["task_url"])
            except Exception:
                pass

    report: Dict[str, dict] = {}

    # People who had morning tasks — show all planned (0h if no time logged)
    for person, morning_tasks in snapshot["people"].items():
        planned_ids = {t["task_id"] for t in morning_tasks}
        person_time = user_task_ms.get(person, {})

        planned = []
        for task in morning_tasks:
            logged_ms = person_time.get(task["task_id"], 0)
            planned.append({
                **task,
                "logged": _format_duration(logged_ms) if logged_ms > 0 else "0h",
                "logged_ms": logged_ms,
            })

        unplanned = [
            {**entry_task_meta[tid], "logged": _format_duration(ms), "logged_ms": ms}
            for tid, ms in person_time.items()
            if tid not in planned_ids
        ]

        report[person] = {
            "planned": planned,
            "unplanned": unplanned,
            "avatar": morning_tasks[0].get("avatar") if morning_tasks else None,
        }

    # People who logged time today but had NO morning tasks
    for user, task_times in user_task_ms.items():
        if user in report:
            continue
        report[user] = {
            "planned": [],
            "unplanned": [
                {**entry_task_meta[tid], "logged": _format_duration(ms), "logged_ms": ms}
                for tid, ms in task_times.items()
            ],
            "avatar": None,
        }

    return report


# ── Email ─────────────────────────────────────────────────────────────────────

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
    api_token = (os.environ.get("CLICKUP_TOKEN") or "").strip()
    team_id = (os.environ.get("CLICKUP_WORKSPACE") or "").strip()
    gmail_from = (os.environ.get("EMAIL_FROM") or "").strip()
    gmail_password = (os.environ.get("EMAIL_PASSWORD") or "").strip()
    email_to = os.environ.get("EMAIL_TO", "").strip()
    email_cc = os.environ.get("EMAIL_CC", "").strip()

    missing = [k for k, v in {
        "CLICKUP_TOKEN": api_token,
        "CLICKUP_WORKSPACE": team_id,
        "EMAIL_FROM": gmail_from,
        "EMAIL_PASSWORD": gmail_password,
        "EMAIL_TO": email_to,
    }.items() if not v]
    if missing:
        print(f"ERROR: Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    print(f"=== ClickUp Evening Report — {date.today()} ===")

    snapshot = load_snapshot()
    print(f"Loaded morning snapshot: {sum(len(v) for v in snapshot['people'].values())} planned tasks across {len(snapshot['people'])} people")

    client = ClickUpClient(api_token)
    start_ms, end_ms = _today_range_ms()

    # Extract user_id per person from snapshot
    person_user_ids: dict = {}
    for person, tasks in snapshot["people"].items():
        for task in tasks:
            if task.get("user_id"):
                person_user_ids[person] = task["user_id"]
                break

    # Also fetch all workspace members to catch people not in morning snapshot
    print("Fetching workspace members...")
    all_members = client.get_workspace_members(team_id)
    for m in all_members:
        username = m.get("username")
        uid = m.get("id")
        if username and uid and username not in person_user_ids:
            person_user_ids[username] = uid

    # Query time entries per user (token owner can only see entries via assignee filter)
    print(f"Fetching time entries for {len(person_user_ids)} members...")
    time_entries = []
    for uid in person_user_ids.values():
        entries = client.get_time_entries(team_id, start_ms, end_ms, assignee_id=uid)
        time_entries.extend(entries)
    print(f"Found {len(time_entries)} time entries total")

    report = build_report(client, snapshot, time_entries)

    if not report:
        print("No time logged today — no email sent.")
        return

    total_planned = sum(len(r["planned"]) for r in report.values())
    total_unplanned = sum(len(r["unplanned"]) for r in report.values())
    print(f"\nSummary: {len(report)} people | {total_planned} planned | {total_unplanned} unplanned")
    for user, data in sorted(report.items()):
        print(f"  {user}: {len(data['planned'])} planned, {len(data['unplanned'])} unplanned")

    report_date = date.today()
    html = generate_email_html(report, report_date)
    subject = f"Evening Report — {report_date.strftime('%A, %b %d')}"

    send_email(html, subject, {
        "from": gmail_from,
        "app_password": gmail_password,
        "to": email_to,
        "cc": email_cc,
    })


if __name__ == "__main__":
    main()
