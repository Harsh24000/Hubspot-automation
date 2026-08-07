#!/usr/bin/env python3
"""
Weekly ClickUp Ops Report
Runs every Monday morning (triggered externally via cron-job.org → GitHub Actions).

Four sections, each covering the workspace as a whole (not just one list):

1. Client Implementation — tasks tagged with a "Client" custom field, grouped
   by client. Shows a derived status, time tracked last week, last activity,
   and a per-resource breakdown.
2. Resource → Project Tracking — every open task, grouped by assignee, showing
   task name, due date, and project (ClickUp List) name.
3. Customer Support — tickets from the Customer Support list (same list the
   daily report reads), grouped by client: total / resolved / pending counts,
   tracked time, and assigned resource(s).
4. Overdue Tasks — every open task across the workspace past its due date.

ASSUMPTIONS (stated up front since ClickUp has no literal "Project" object):
- "Project" = the ClickUp List name a task belongs to.
- "Resource" = the task's assignee.
- "Client" = a custom field whose name contains "client" or "company".
- "Tracked time" = time logged via ClickUp time tracking, for last week
  (Monday 00:00 IST through Sunday 23:59 IST, the week before this run).
- Client Implementation "Status" is a heuristic: all tasks closed → Completed;
  none started → Not Started; anything else → In Progress.
"""

import os
import sys
import smtplib
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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


def _fmt_due(due_ms) -> tuple:
    """Returns (display_str, days_left_or_None, is_overdue)."""
    if not due_ms:
        return ('No due date', None, False)
    due_dt = datetime.fromtimestamp(int(due_ms) / 1000, tz=timezone.utc)
    days_left = (due_dt - datetime.now(timezone.utc)).days
    return (due_dt.strftime('%d %b %Y'), days_left, days_left < 0)


# ── Data collection ───────────────────────────────────────────────────────────

def fetch_workspace_tasks(client: ClickUpClient, team_id: str, include_closed: bool) -> list:
    """All tasks across every space/list in the workspace."""
    all_tasks = []
    spaces = client.get_spaces(team_id)
    print(f'Found {len(spaces)} space(s)')
    for space in spaces:
        lists = client.get_all_lists(space['id'])
        print(f'  Space: {space["name"]} — {len(lists)} list(s)')
        for lst in lists:
            tasks = client.get_tasks(lst['id'], include_closed=include_closed)
            print(f'    → {lst["name"]}: {len(tasks)} task(s)')
            all_tasks.extend(tasks)
    return all_tasks


def fetch_support_tickets(client: ClickUpClient) -> list:
    """Support list tickets, including closed ones (needed for resolved counts)."""
    return client.get_tasks(CLICKUP_SUPPORT_LIST_ID, include_closed=True)


def fetch_time_by_task(client: ClickUpClient, team_id: str) -> dict:
    """task_id -> total ms tracked last week, across every workspace member."""
    start_ms, end_ms = _last_week_range_ms()
    members = client.get_workspace_members(team_id)
    print(f'Fetching last-week time entries for {len(members)} member(s)...')
    task_ms = defaultdict(int)
    for m in members:
        uid = m.get('id')
        if not uid:
            continue
        entries = client.get_time_entries(team_id, start_ms, end_ms, assignee_id=uid)
        for e in entries:
            task = e.get('task') or {}
            tid = task.get('id')
            duration = int(e.get('duration', 0))
            if tid and duration > 0:
                task_ms[tid] += duration
    return task_ms


# ── Section builders (return plain data, not HTML — kept separate for testability) ──

def build_client_implementation(all_tasks: list, task_ms: dict) -> dict:
    by_client = defaultdict(list)
    for t in all_tasks:
        client_name = _client_name_from_task(t)
        if not client_name or 'niro' in client_name.lower():
            continue  # only external client-tagged implementation work
        by_client[client_name].append(t)

    result = {}
    for client_name, tasks in by_client.items():
        closed = [t for t in tasks if t.get('status', {}).get('type') == 'closed']
        started = [t for t in tasks if t.get('status', {}).get('type') not in ('open', None)]
        if len(closed) == len(tasks):
            status = 'Completed'
        elif not started:
            status = 'Not Started'
        else:
            status = 'In Progress'

        total_ms = sum(task_ms.get(t['id'], 0) for t in tasks)
        last_activity_ms = max((int(t.get('date_updated', 0) or 0) for t in tasks), default=0)

        resources = []
        for t in tasks:
            for res in _assignees(t):
                resources.append({
                    'resource': res,
                    'project': _project_name(t),
                    'tracked_ms': task_ms.get(t['id'], 0),
                    'task_name': t.get('name', 'Untitled'),
                })

        result[client_name] = {
            'status': status,
            'duration_ms': total_ms,
            'last_activity_ms': last_activity_ms,
            'task_count': len(tasks),
            'resources': resources,
        }
    return result


def build_resource_project_tracking(open_tasks: list) -> dict:
    by_resource = defaultdict(list)
    for t in open_tasks:
        due_str, days_left, overdue = _fmt_due(t.get('due_date'))
        for res in _assignees(t):
            by_resource[res].append({
                'task_name': t.get('name', 'Untitled'),
                'due_str': due_str,
                'days_left': days_left,
                'overdue': overdue,
                'project': _project_name(t),
                'task_url': t.get('url', '#'),
            })
    return by_resource


def build_customer_support(tickets: list, task_ms: dict) -> dict:
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


def build_overdue(all_open_tasks: list) -> list:
    overdue = []
    for t in all_open_tasks:
        due_str, days_left, is_overdue = _fmt_due(t.get('due_date'))
        if not is_overdue:
            continue
        overdue.append({
            'task_name': t.get('name', 'Untitled'),
            'assignees': ', '.join(_assignees(t)),
            'project': _project_name(t),
            'due_str': due_str,
            'days_overdue': abs(days_left),
            'task_url': t.get('url', '#'),
        })
    overdue.sort(key=lambda x: -x['days_overdue'])
    return overdue


# ── HTML builder ──────────────────────────────────────────────────────────────

def _slab(label: str, color: str) -> str:
    return (f'<tr><td style="background:{color};padding:10px 32px;">'
            f'<div style="color:#fff;font-size:11px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:2px;">{label}</div></td></tr>')


def _section_title(label: str) -> str:
    return (f'<div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;'
            f'letter-spacing:1.5px;margin-bottom:14px;">&#9632; {label}</div>')


def build_email_html(client_impl: dict, resource_tracking: dict, support: dict, overdue: list, report_date: date) -> str:
    today_str = report_date.strftime('%A, %B %d %Y')

    # ── Section 1: Client Implementation ──
    impl_html = ''
    for client_name, data in sorted(client_impl.items()):
        status_colors = {'Completed': ('#166534', '#f0fdf4'), 'In Progress': ('#1d4ed8', '#eff6ff'),
                          'Not Started': ('#92400e', '#fef3c7')}
        fg, bg = status_colors.get(data['status'], ('#475569', '#f1f5f9'))
        last_activity = (datetime.fromtimestamp(data['last_activity_ms'] / 1000, tz=timezone.utc)
                          .strftime('%d %b %Y') if data['last_activity_ms'] else 'Unknown')
        rows = ''.join(
            f'<tr><td style="padding:6px 10px;font-size:12px;color:#334155;">{r["resource"]}</td>'
            f'<td style="padding:6px 10px;font-size:12px;color:#334155;">{r["project"]}</td>'
            f'<td style="padding:6px 10px;font-size:12px;color:#334155;">{_fmt_duration(r["tracked_ms"])}</td></tr>'
            for r in data['resources']
        )
        impl_html += f'''
    <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px 18px;margin-bottom:12px;">
      <div style="display:flex;justify-content:space-between;margin-bottom:8px;">
        <span style="font-size:15px;font-weight:700;color:#0f172a;">{client_name}</span>
        <span style="display:inline-block;padding:3px 10px;border-radius:10px;background:{bg};color:{fg};font-size:11px;font-weight:700;">{data['status']}</span>
      </div>
      <div style="font-size:12px;color:#64748b;margin-bottom:10px;">
        {data['task_count']} task(s) &middot; {_fmt_duration(data['duration_ms'])} tracked last week &middot; last activity {last_activity}
      </div>
      <table width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #f1f5f9;">
        <tr><th style="text-align:left;padding:6px 10px;font-size:11px;color:#94a3b8;">Resource</th>
            <th style="text-align:left;padding:6px 10px;font-size:11px;color:#94a3b8;">Project</th>
            <th style="text-align:left;padding:6px 10px;font-size:11px;color:#94a3b8;">Tracked</th></tr>
        {rows}
      </table>
    </div>'''
    if not impl_html:
        impl_html = '<div style="color:#94a3b8;font-size:13px;">No client-tagged implementation work found.</div>'

    # ── Section 2: Resource → Project Tracking ──
    tracking_html = ''
    for resource, tasks in sorted(resource_tracking.items()):
        rows = ''
        for t in sorted(tasks, key=lambda x: (x['days_left'] is None, x['days_left'] or 0)):
            due_color = '#b91c1c' if t['overdue'] else '#334155'
            rows += (f'<tr><td style="padding:6px 10px;font-size:12px;color:#0f172a;">'
                     f'<a href="{t["task_url"]}" style="color:#0f172a;text-decoration:none;">{t["task_name"]}</a></td>'
                     f'<td style="padding:6px 10px;font-size:12px;color:{due_color};">{t["due_str"]}</td>'
                     f'<td style="padding:6px 10px;font-size:12px;color:#64748b;">{t["project"]}</td></tr>')
        tracking_html += f'''
    <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px 18px;margin-bottom:12px;">
      <div style="font-size:14px;font-weight:700;color:#0f172a;margin-bottom:8px;">{resource} &middot; {len(tasks)} task(s)</div>
      <table width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #f1f5f9;">
        <tr><th style="text-align:left;padding:6px 10px;font-size:11px;color:#94a3b8;">Task</th>
            <th style="text-align:left;padding:6px 10px;font-size:11px;color:#94a3b8;">Due Date</th>
            <th style="text-align:left;padding:6px 10px;font-size:11px;color:#94a3b8;">Project</th></tr>
        {rows}
      </table>
    </div>'''
    if not tracking_html:
        tracking_html = '<div style="color:#94a3b8;font-size:13px;">No open tasks found.</div>'

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
        support_rows = '<tr><td colspan="6" style="padding:12px;color:#94a3b8;font-size:13px;">No support tickets found.</td></tr>'

    # ── Section 4: Overdue ──
    overdue_rows = ''
    for t in overdue:
        overdue_rows += (
            f'<tr><td style="padding:8px 12px;font-size:12px;color:#0f172a;">'
            f'<a href="{t["task_url"]}" style="color:#0f172a;text-decoration:none;">{t["task_name"]}</a></td>'
            f'<td style="padding:8px 12px;font-size:12px;color:#64748b;">{t["assignees"]}</td>'
            f'<td style="padding:8px 12px;font-size:12px;color:#64748b;">{t["project"]}</td>'
            f'<td style="padding:8px 12px;font-size:12px;color:#b91c1c;font-weight:700;">{t["days_overdue"]}d overdue</td></tr>'
        )
    if not overdue_rows:
        overdue_rows = '<tr><td colspan="4" style="padding:12px;color:#166534;font-size:13px;">Nothing overdue — clean slate.</td></tr>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:720px;margin:0 auto;">

  <tr><td style="border-radius:20px 20px 0 0;overflow:hidden;background:linear-gradient(135deg,#0f2744 0%,#1a56a0 100%);padding:36px 32px;">
    <div style="color:#dbeafe;font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;">Weekly Ops Report</div>
    <div style="color:#fff;font-size:28px;font-weight:800;">Weekly Report</div>
    <div style="color:rgba(255,255,255,0.7);font-size:14px;margin-top:4px;">{today_str}</div>
  </td></tr>
  <tr><td style="height:4px;background:linear-gradient(90deg,#6366F1,#A855F7,#EC4899);"></td></tr>

  {_slab('&#128188; Client Implementation', '#7c3aed')}
  <tr><td style="background:#fff;padding:20px 32px;">{_section_title('Client Implementation — Status &amp; Last Activity')}{impl_html}</td></tr>

  {_slab('&#128100; Resource → Project Tracking', '#1a56a0')}
  <tr><td style="background:#fff;padding:20px 32px;">{_section_title('Open Tasks by Resource')}{tracking_html}</td></tr>

  {_slab('&#127919; Customer Support', '#166534')}
  <tr><td style="background:#fff;padding:20px 32px;">
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

  {_slab('&#9888; Overdue Tasks', '#b91c1c')}
  <tr><td style="background:#fff;padding:20px 32px 32px;border-radius:0 0 20px 20px;">
    {_section_title(f'All Overdue Tasks ({len(overdue)})')}
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #fecaca;border-radius:8px;overflow:hidden;">
      <tr style="background:#fef2f2;">
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Task</th>
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Assignee</th>
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Project</th>
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Overdue by</th>
      </tr>
      {overdue_rows}
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

    print('Fetching open tasks workspace-wide...')
    open_tasks = fetch_workspace_tasks(client, team_id, include_closed=False)
    print(f'Total open tasks: {len(open_tasks)}\n')

    print('Fetching ALL tasks (open + closed) workspace-wide, for client implementation status...')
    all_tasks = fetch_workspace_tasks(client, team_id, include_closed=True)
    print(f'Total tasks (all): {len(all_tasks)}\n')

    print('Fetching support tickets (open + closed)...')
    support_tickets = fetch_support_tickets(client)
    print(f'Total support tickets: {len(support_tickets)}\n')

    task_ms = fetch_time_by_task(client, team_id)
    print(f'Time entries found for {len(task_ms)} distinct task(s)\n')

    client_impl = build_client_implementation(all_tasks, task_ms)
    resource_tracking = build_resource_project_tracking(open_tasks)
    support = build_customer_support(support_tickets, task_ms)
    overdue = build_overdue(open_tasks)

    print(f'Summary: {len(client_impl)} client(s) in implementation, '
          f'{len(resource_tracking)} resource(s) tracked, '
          f'{len(support)} client(s) in support, '
          f'{len(overdue)} overdue task(s)')

    report_date = date.today()
    html = build_email_html(client_impl, resource_tracking, support, overdue, report_date)
    subject = f'Weekly Ops Report — {report_date.strftime("%d %b %Y")}'

    send_email(html, subject, {
        'from': gmail_from,
        'app_password': gmail_password,
        'to': email_to,
    })


if __name__ == '__main__':
    main()
