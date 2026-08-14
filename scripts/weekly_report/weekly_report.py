#!/usr/bin/env python3
"""
Weekly ClickUp Ops Report
Runs every Monday morning (triggered externally via cron-job.org → GitHub Actions).

Three sections, matching the team's own reference spreadsheet layout:

1. Onboarding — tasks in an "Active Onboardings" list, tagged with a "Client"
   custom field, grouped by client. Shows Client Name, Status (the current
   status of that client's most recently updated task), Duration (days since
   their earliest onboarding task was created), and Last Activity (relative,
   e.g. "3 days ago"). No time-tracking figures in this section.
2. Resource → Product Module Tracking — for each resource (assignee), every
   product module they logged time against this week, with hours tracked,
   plus a total-tracked-time badge at the top of their card.
3. Customer Support — tickets from the Customer Support list (same list the
   daily report reads), grouped by client: total / resolved / pending counts,
   tracked time, and assigned resource(s).

Only ROOT tasks are scanned — subtasks are deliberately excluded (this also
cuts API call volume substantially, which reduces rate-limit risk).

ASSUMPTIONS:
- "Resource" = the task's assignee.
- "Product Module" = a custom field whose name contains "module" or "product".
- "Client" = a custom field whose name contains "client" or "company".
- "Active Onboardings" scope = any list whose name contains "onboarding"
  (case-insensitive) — the Onboarding section only counts tasks living there.
- "Tracked time" = time logged via ClickUp time tracking, for this reporting
  week (Monday 00:00 IST through Sunday 23:59 IST, the week before this run).
- Onboarding "Status" = the ClickUp status of whichever of that client's
  onboarding tasks was updated most recently (so it shows real status names
  like "UAT", not a synthetic label).
- "Duration" = whole days between the client's earliest onboarding task being
  created and today.
"""

import os
import sys
import smtplib
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List

from clickup_client import ClickUpClient

CLICKUP_SUPPORT_LIST_ID = os.environ.get('CLICKUP_SUPPORT_LIST_ID', '901615411023')
IST = timezone(timedelta(hours=5, minutes=30))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_custom_field_value(field: dict) -> str:
    val = field.get('value')
    if val is None or val == '':
        return ''
    ftype = field.get('type', '')
    if ftype in ('drop_down', 'labels'):
        options = (field.get('type_config') or {}).get('options', [])
        if isinstance(val, list):
            names = [o.get('name') for o in options if o.get('id') in val or o.get('orderindex') in val]
            return ', '.join(n for n in names if n)
        match = next((o.get('name') for o in options if o.get('orderindex') == val or o.get('id') == val), None)
        return match or str(val)
    return str(val)


def _client_name_from_task(task: dict) -> str:
    for field in task.get('custom_fields', []):
        name_lower = (field.get('name') or '').lower()
        if 'client' in name_lower or 'company' in name_lower:
            value = _extract_custom_field_value(field)
            if value:
                return value.strip()
    return ''


def _product_module_name(task: dict) -> str:
    """Extract 'Product Module' from a custom field (drop_down, text, or labels type)."""
    for field in task.get('custom_fields', []):
        name_lower = (field.get('name') or '').lower()
        if 'module' not in name_lower and 'product' not in name_lower:
            continue
        value = field.get('value')
        if value is None or value == '':
            continue
        field_type = field.get('type', '')
        if field_type == 'drop_down':
            options = (field.get('type_config') or {}).get('options', [])
            for opt in options:
                if str(opt.get('orderindex', '')) == str(value) or opt.get('id') == str(value):
                    return opt.get('name', str(value))
            try:
                return options[int(value)]['name']
            except (IndexError, ValueError, TypeError):
                return str(value)
        if field_type == 'labels':
            options = (field.get('type_config') or {}).get('options', [])
            matched = [o['name'] for o in options if o.get('id') in (value or [])]
            return ', '.join(matched) if matched else 'N/A'
        return str(value).strip() or 'N/A'
    return 'N/A'


def _project_name(task: dict) -> str:
    return (task.get('list') or {}).get('name', 'Unknown')


def _assignees(task: dict) -> list:
    names = [a.get('username') or a.get('email', '') for a in task.get('assignees', [])]
    return [n for n in names if n] or ['Unassigned']


def _last_week_range_ms() -> tuple:
    """Monday 00:00 IST through Sunday 23:59:59 IST of the week before this run."""
    now_ist = datetime.now(IST)
    this_monday = (now_ist - timedelta(days=now_ist.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    last_monday = this_monday - timedelta(days=7)
    last_sunday_end = this_monday - timedelta(seconds=1)
    return int(last_monday.timestamp() * 1000), int(last_sunday_end.timestamp() * 1000)


def _last_week_display() -> str:
    """e.g. 'Monday 27 Jul – Sunday 02 Aug 2026' — explicit Monday-to-Sunday range."""
    start_ms, end_ms = _last_week_range_ms()
    start = datetime.fromtimestamp(start_ms / 1000, tz=IST)
    end = datetime.fromtimestamp(end_ms / 1000, tz=IST)
    return f"Monday {start.strftime('%d %b')} – Sunday {end.strftime('%d %b %Y')}"


def _fmt_duration(ms: int) -> str:
    if not ms:
        return '0h'
    total_minutes = int(ms) // 60000
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f'{hours}h {minutes}m'
    if hours:
        return f'{hours}h'
    return f'{minutes}m'


def _relative_time(ms: int) -> str:
    """e.g. 'Today', '1 day ago', '5 days ago' — used for Last Activity instead
    of a raw date, per the team's reference sheet."""
    if not ms:
        return 'Unknown'
    then = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    days = (datetime.now(timezone.utc) - then).days
    if days <= 0:
        return 'Today'
    if days == 1:
        return '1 day ago'
    return f'{days} days ago'


def _days_since(ms: int) -> int:
    if not ms:
        return 0
    then = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - then).days)


# ── Data collection ───────────────────────────────────────────────────────────

def fetch_workspace_tasks(client: ClickUpClient, team_id: str) -> list:
    """
    All ROOT tasks (no subtasks) across every space/list in the workspace,
    open and closed both. Subtasks are deliberately excluded — this report
    is scoped to main tasks only, and skipping them also cuts API call
    volume a lot (subtask fetching is one call per parent task).
    """
    all_tasks = []
    spaces = client.get_spaces(team_id)
    print(f'Found {len(spaces)} space(s)')
    for space in spaces:
        lists = client.get_all_lists(space['id'])
        print(f'  Space: {space["name"]} — {len(lists)} list(s)')
        for lst in lists:
            tasks = client.get_tasks(lst['id'], include_closed=True, include_subtasks=False)
            print(f'    → {lst["name"]}: {len(tasks)} task(s)')
            all_tasks.extend(tasks)
    return all_tasks


def fetch_support_tickets(client: ClickUpClient) -> list:
    """Support list tickets, main tasks only, including closed ones (needed for resolved counts)."""
    return client.get_tasks(CLICKUP_SUPPORT_LIST_ID, include_closed=True, include_subtasks=False)


def fetch_time_by_person_task(client: ClickUpClient, team_id: str) -> Dict[str, Dict[str, int]]:
    """person username -> {task_id: total_ms} for this reporting week."""
    start_ms, end_ms = _last_week_range_ms()
    members = client.get_workspace_members(team_id)
    print(f'Fetching time entries for {len(members)} member(s), this reporting week...')
    person_task_ms: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for m in members:
        uid = m.get('id')
        username = m.get('username')
        if not uid or not username:
            continue
        try:
            entries = client.get_time_entries(team_id, start_ms, end_ms, assignee_id=uid)
        except Exception as exc:
            print(f'  [warn] Could not fetch time entries for {username}: {exc}')
            continue
        for e in entries:
            task = e.get('task') or {}
            tid = task.get('id')
            duration = int(e.get('duration', 0))
            if tid and duration > 0:
                person_task_ms[username][tid] += duration
    return person_task_ms


def _flatten_task_ms(person_task_ms: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    """Sum across all people -> task_id -> total ms (used by the support-ticket rollup)."""
    task_ms: Dict[str, int] = defaultdict(int)
    for person, tasks in person_task_ms.items():
        for tid, ms in tasks.items():
            task_ms[tid] += ms
    return task_ms


# ── Section builders ────────────────────────────────────────────────────────

def build_onboarding(all_tasks: list) -> dict:
    by_client = defaultdict(list)
    for t in all_tasks:
        if 'onboarding' not in _project_name(t).lower():
            continue
        client_name = _client_name_from_task(t)
        if not client_name or 'niro' in client_name.lower():
            continue
        by_client[client_name].append(t)

    result = {}
    for client_name, tasks in by_client.items():
        # Status = status of whichever task was updated most recently, so we
        # show a real ClickUp status name rather than a synthetic label.
        most_recent = max(tasks, key=lambda t: int(t.get('date_updated', 0) or 0))
        status = (most_recent.get('status', {}).get('status') or 'Unknown').title()

        # Duration = days since the earliest task in this client's onboarding work was created.
        created_ms = [int(t.get('date_created', 0) or 0) for t in tasks if t.get('date_created')]
        duration_days = _days_since(min(created_ms)) if created_ms else 0

        last_activity_ms = max((int(t.get('date_updated', 0) or 0) for t in tasks), default=0)

        result[client_name] = {
            'status': status,
            'duration_days': duration_days,
            'last_activity_ms': last_activity_ms,
            'task_count': len(tasks),
        }
    return result


def build_resource_tracking(all_tasks: list, person_task_ms: Dict[str, Dict[str, int]]) -> dict:
    """resource -> {'total_ms': int, 'rows': [{'product_module', 'tracked_ms', 'task_name'}, ...]}"""
    task_by_id = {t['id']: t for t in all_tasks}
    result = {}
    for person, task_times in person_task_ms.items():
        rows = []
        total_ms = 0
        for tid, ms in task_times.items():
            task = task_by_id.get(tid)
            module = _product_module_name(task) if task else 'Unknown'
            rows.append({
                'product_module': module,
                'tracked_ms': ms,
                'task_name': task.get('name', 'Untitled') if task else 'Untitled',
            })
            total_ms += ms
        if not rows:
            continue
        rows.sort(key=lambda r: -r['tracked_ms'])
        result[person] = {'total_ms': total_ms, 'rows': rows}
    return result


def build_customer_support(tickets: list, task_ms: Dict[str, int]) -> dict:
    by_client = defaultdict(lambda: {'total': 0, 'resolved': 0, 'pending': 0,
                                      'tracked_ms': 0, 'resources': set(), 'statuses': set()})
    for t in tickets:
        client_name = _client_name_from_task(t) or 'Unspecified'
        entry = by_client[client_name]
        entry['total'] += 1
        is_closed = t.get('status', {}).get('type') == 'closed'
        if is_closed:
            entry['resolved'] += 1
        else:
            entry['pending'] += 1
        entry['tracked_ms'] += task_ms.get(t['id'], 0)
        entry['resources'].update(_assignees(t))
        entry['statuses'].add(t.get('status', {}).get('status', 'unknown'))
    return by_client


# ── HTML builder ──────────────────────────────────────────────────────────────

def _slab(label: str, color: str) -> str:
    return (f'<tr><td style="background:{color};padding:10px 32px;">'
            f'<div style="color:#fff;font-size:11px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:2px;">{label}</div></td></tr>')


def _section_title(label: str) -> str:
    return (f'<div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;'
            f'letter-spacing:1.5px;margin-bottom:14px;">&#9632; {label}</div>')


def build_email_html(onboarding: dict, resource_tracking: dict, support: dict, report_date: date) -> str:
    today_str = report_date.strftime('%A, %B %d %Y')
    week_label = _last_week_display()

    # ── Section 1: Onboarding ──
    onboarding_rows = ''
    for client_name, d in sorted(onboarding.items()):
        onboarding_rows += (
            f'<tr><td style="padding:10px 12px;font-size:13px;color:#0f172a;font-weight:600;">{client_name}</td>'
            f'<td style="padding:10px 12px;font-size:12px;">'
            f'<span style="display:inline-block;padding:3px 10px;border-radius:10px;'
            f'background:#eff6ff;color:#1d4ed8;font-size:11px;font-weight:700;">{d["status"]}</span></td>'
            f'<td style="padding:10px 12px;font-size:13px;color:#334155;">{d["duration_days"]} '
            f'day{"s" if d["duration_days"] != 1 else ""}</td>'
            f'<td style="padding:10px 12px;font-size:13px;color:#64748b;">{_relative_time(d["last_activity_ms"])}</td></tr>'
        )
    if not onboarding_rows:
        onboarding_rows = ('<tr><td colspan="4" style="padding:12px;color:#94a3b8;font-size:13px;">'
                            'No active onboarding clients found.</td></tr>')

    # ── Section 2: Resource → Product Module Tracking ──
    tracking_html = ''
    for resource, data in sorted(resource_tracking.items(), key=lambda kv: -kv[1]['total_ms']):
        rows = ''.join(
            f'<tr><td style="padding:6px 10px;font-size:12px;color:#0f172a;">{r["product_module"]}</td>'
            f'<td style="padding:6px 10px;font-size:12px;color:#64748b;">{_fmt_duration(r["tracked_ms"])}</td></tr>'
            for r in data['rows']
        )
        tracking_html += f'''
    <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px 18px;margin-bottom:12px;">
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;">
        <tr>
          <td style="font-size:14px;font-weight:700;color:#0f172a;">{resource}</td>
          <td align="right">
            <span style="display:inline-block;padding:5px 12px;border-radius:12px;background:#ECFDF5;
                         color:#059669;font-size:13px;font-weight:700;white-space:nowrap;">
              &#9203; {_fmt_duration(data['total_ms'])} total
            </span>
          </td>
        </tr>
      </table>
      <table width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #f1f5f9;">
        <tr><th style="text-align:left;padding:6px 10px;font-size:11px;color:#94a3b8;">Product Module</th>
            <th style="text-align:left;padding:6px 10px;font-size:11px;color:#94a3b8;">Tracked Time</th></tr>
        {rows}
      </table>
    </div>'''
    if not tracking_html:
        tracking_html = '<div style="color:#94a3b8;font-size:13px;">No time tracked this week.</div>'

    # ── Section 3: Customer Support ──
    support_rows = ''
    for client_name, d in sorted(support.items()):
        support_rows += (
            f'<tr><td style="padding:8px 12px;font-size:12px;color:#0f172a;font-weight:600;">{client_name}</td>'
            f'<td style="padding:8px 12px;font-size:12px;text-align:center;">{d["total"]}</td>'
            f'<td style="padding:8px 12px;font-size:12px;text-align:center;color:#166534;">{d["resolved"]}</td>'
            f'<td style="padding:8px 12px;font-size:12px;text-align:center;color:#b45309;">{d["pending"]}</td>'
            f'<td style="padding:8px 12px;font-size:12px;">{_fmt_duration(d["tracked_ms"])}</td>'
            f'<td style="padding:8px 12px;font-size:12px;color:#64748b;">{", ".join(sorted(d["resources"]))}</td></tr>'
        )
    if not support_rows:
        support_rows = ('<tr><td colspan="6" style="padding:12px;color:#94a3b8;font-size:13px;">'
                         'No support tickets found.</td></tr>')

    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:720px;margin:0 auto;">

  <tr><td style="border-radius:20px 20px 0 0;overflow:hidden;background:linear-gradient(135deg,#0f2744 0%,#1a56a0 100%);padding:36px 32px;">
    <div style="color:#dbeafe;font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;">Weekly Ops Report &middot; {week_label}</div>
    <div style="color:#fff;font-size:28px;font-weight:800;">Weekly Report</div>
    <div style="color:rgba(255,255,255,0.7);font-size:14px;margin-top:4px;">Sent {today_str}</div>
  </td></tr>
  <tr><td style="height:4px;background:linear-gradient(90deg,#6366F1,#A855F7,#EC4899);"></td></tr>

  {_slab('&#128188; Onboarding', '#7c3aed')}
  <tr><td style="background:#fff;padding:20px 32px;">
    {_section_title('Active Onboarding Clients')}
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
      <tr style="background:#f5f3ff;">
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Client Name</th>
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Status</th>
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Duration</th>
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Last Activity</th>
      </tr>
      {onboarding_rows}
    </table>
  </td></tr>

  {_slab('&#128100; Resource → Product Module Tracking', '#1a56a0')}
  <tr><td style="background:#fff;padding:20px 32px;">{_section_title(f'This Week — {week_label}')}{tracking_html}</td></tr>

  {_slab('&#127919; Customer Support', '#166534')}
  <tr><td style="background:#fff;padding:20px 32px 32px;border-radius:0 0 20px 20px;">
    {_section_title('Tickets by Client')}
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
      <tr style="background:#f0fdf4;">
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Client</th>
        <th style="padding:8px 12px;font-size:11px;color:#64748b;">Total</th>
        <th style="padding:8px 12px;font-size:11px;color:#64748b;">Resolved</th>
        <th style="padding:8px 12px;font-size:11px;color:#64748b;">Pending</th>
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Tracked</th>
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Resource(s)</th>
      </tr>
      {support_rows}
    </table>
  </td></tr>

</table>
</body>
</html>'''


# ── Email sender ──────────────────────────────────────────────────────────────

def send_email(html: str, subject: str, cfg: dict) -> None:
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = cfg['from']
    msg['To'] = cfg['to']
    msg.attach(MIMEText(html, 'html'))

    to_list = [a.strip() for a in cfg['to'].split(',') if a.strip()]
    print(f'Sending email via Gmail SMTP to: {", ".join(to_list)}')
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(cfg['from'], cfg['app_password'])
        server.sendmail(cfg['from'], to_list, msg.as_string())
    print('Email sent successfully!')


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    api_token = os.environ['CLICKUP_TOKEN']
    team_id = os.environ.get('CLICKUP_WORKSPACE', '').strip()
    gmail_from = os.environ['GMAIL_ADDRESS']
    gmail_password = os.environ['GMAIL_APP_PASSWORD']
    email_to = os.environ['EMAIL_TO']

    client = ClickUpClient(api_token)
    if not team_id:
        team_id = client.get_team_id()
        print(f'Auto-detected team ID: {team_id}')

    print('=== Weekly ClickUp Ops Report ===\n')

    print('Fetching ALL root tasks (open + closed, no subtasks) workspace-wide...')
    all_tasks = fetch_workspace_tasks(client, team_id)
    print(f'Total root tasks: {len(all_tasks)}\n')

    print('Fetching support tickets (open + closed, no subtasks)...')
    support_tickets = fetch_support_tickets(client)
    print(f'Total support tickets: {len(support_tickets)}\n')

    person_task_ms = fetch_time_by_person_task(client, team_id)
    task_ms = _flatten_task_ms(person_task_ms)
    print(f'Time entries found for {len(task_ms)} distinct task(s) across {len(person_task_ms)} people\n')

    onboarding = build_onboarding(all_tasks)
    resource_tracking = build_resource_tracking(all_tasks, person_task_ms)
    support = build_customer_support(support_tickets, task_ms)

    print(f'Summary: {len(onboarding)} client(s) onboarding, '
          f'{len(resource_tracking)} resource(s) with tracked time, '
          f'{len(support)} client(s) in support')

    report_date = date.today()
    html = build_email_html(onboarding, resource_tracking, support, report_date)
    subject = f'Weekly Ops Report — {_last_week_display()}'

    send_email(html, subject, {
        'from': gmail_from,
        'app_password': gmail_password,
        'to': email_to,
    })


if __name__ == '__main__':
    main()
