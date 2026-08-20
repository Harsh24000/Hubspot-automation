#!/usr/bin/env python3
"""
Weekly ClickUp Ops Report
Runs every Monday morning (triggered externally via cron-job.org → GitHub Actions).

STRICT WEEK SCOPING
-------------------
Every number in this report describes LAST WEEK ONLY — Monday 00:00:00 IST
through Sunday 23:59:59 IST of the week before the run. A task/ticket is
counted as "last week's" if ANY of these fall inside that window:

    - it was created            (date_created)
    - it was updated            (date_updated)
    - it was closed             (date_closed)
    - someone logged time on it (ClickUp time entries, already week-bounded)

Anything outside that window is excluded, even if it is still open. This is the
difference from the previous version, which reported the LIFETIME contents of
the support and onboarding lists and therefore showed totals like "13 tickets"
for a client that had nothing at all last week.

Three sections:

1. Onboarding — clients with onboarding-list activity last week. Tasks whose
   status is CLOSED are excluded, and a client whose tasks are all closed drops
   off the report entirely. Shows Client, Status, Duration, Last Activity.
2. Resource → Project Tracking — per resource, time logged last week, grouped
   by project. One row per project with the summed time (NOT one row per task).
3. Customer Support — tickets from the Customer Support list that saw activity
   last week, grouped by client: ticket type, total / resolved / pending,
   tracked time, and assigned resource(s).

Only ROOT tasks are scanned — subtasks are deliberately excluded (this also
cuts API call volume substantially, which reduces rate-limit risk).

ASSUMPTIONS
- "Resource"     = the person who logged the time.
- "Project"      = a custom field whose name contains "module" or "product".
- "Client"       = a custom field whose name contains "client" or "company".
- "Ticket Type"  = a custom field whose name contains "ticket type" (falling
                   back to any field containing "type"). The field actually
                   matched is printed at startup so it can be verified.
- "Active Onboardings" scope = any list whose name contains "onboarding".
"""

import os
import re
import sys
import smtplib
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Set

from clickup_client import ClickUpClient

CLICKUP_SUPPORT_LIST_ID = os.environ.get('CLICKUP_SUPPORT_LIST_ID', '901615411023')
IST = timezone(timedelta(hours=5, minutes=30))

# Remembers which custom field supplied Ticket Type, so the run log can say so.
_TICKET_TYPE_FIELDS_SEEN: Set[str] = set()


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


def _ticket_type_from_task(task: dict) -> str:
    """Ticket Type custom field.

    Prefers a field literally named like "Ticket Type"; falls back to any
    custom field with "type" in its name so a slightly different label in
    ClickUp still works. Records which field won, for the startup log.
    """
    fallback = ''
    for field in task.get('custom_fields', []):
        field_name = (field.get('name') or '').strip()
        name_lower = field_name.lower()
        if 'type' not in name_lower:
            continue
        value = _extract_custom_field_value(field)
        if not value:
            continue
        if 'ticket type' in name_lower or name_lower == 'type':
            _TICKET_TYPE_FIELDS_SEEN.add(field_name)
            return value.strip()
        if not fallback:
            fallback = value.strip()
            _TICKET_TYPE_FIELDS_SEEN.add(field_name)
    return fallback


def _product_module_name(task: dict) -> str:
    """Extract the project ('Product Module' custom field in ClickUp)."""
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


def _is_closed(task: dict) -> bool:
    return (task.get('status') or {}).get('type') == 'closed'


def _is_milestone(task: dict) -> bool:
    """True when the ClickUp task is of the Milestone task type.

    ClickUp models Milestones as a custom task type: custom_item_id == 1.
    Some payloads also carry an explicit 'milestone' flag, so both are checked.
    """
    if task.get('milestone') is True:
        return True
    try:
        return int(task.get('custom_item_id')) == 1
    except (TypeError, ValueError):
        return False


def build_resource_milestones(all_tasks: list, task_ms: Dict[str, int]) -> dict:
    """resource -> {'completed': [names], 'in_progress': [(name, status)]}

    Only tasks flagged as Milestones in ClickUp, and only those that saw
    activity during the reporting week. A milestone with several assignees
    appears under each of them.
    """
    start_ms, end_ms = _last_week_range_ms()
    result: Dict[str, dict] = defaultdict(lambda: {'completed': [], 'in_progress': []})
    seen = 0

    for task in all_tasks:
        if not _is_milestone(task):
            continue
        if not _touched_last_week(task, start_ms, end_ms, task_ms):
            continue
        seen += 1
        name = task.get('name', 'Untitled')
        status = (task.get('status', {}).get('status') or '').title()
        bucket = 'completed' if _is_closed(task) else 'in_progress'
        for person in _assignees(task):
            result[person][bucket].append((name, status))

    print(f'  Milestones: {seen} milestone task(s) active last week '
          f'across {len(result)} resource(s)')
    return result


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


def _in_week(timestamp_ms, start_ms: int, end_ms: int) -> bool:
    try:
        ts = int(timestamp_ms or 0)
    except (TypeError, ValueError):
        return False
    return bool(ts) and start_ms <= ts <= end_ms


def _touched_last_week(task: dict, start_ms: int, end_ms: int,
                       task_ms: Dict[str, int]) -> bool:
    """Created, updated, closed, or time-tracked inside the reporting week.

    This is THE filter that keeps the report honest — without it the support
    and onboarding sections report the whole lifetime of their lists.
    """
    if _in_week(task.get('date_created'), start_ms, end_ms):
        return True
    if _in_week(task.get('date_updated'), start_ms, end_ms):
        return True
    if _in_week(task.get('date_closed'), start_ms, end_ms):
        return True
    # Time entries were already fetched with the same window, so any hours here
    # are by definition last week's.
    return task_ms.get(task.get('id'), 0) > 0


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
    """e.g. 'Today', '1 day ago', '5 days ago'. Computed in IST, matching the
    reporting window (this used to be done in UTC and could be a day out)."""
    if not ms:
        return 'Unknown'
    then = datetime.fromtimestamp(int(ms) / 1000, tz=IST)
    days = (datetime.now(IST).date() - then.date()).days
    if days <= 0:
        return 'Today'
    if days == 1:
        return '1 day ago'
    return f'{days} days ago'


def _days_since(ms: int) -> int:
    if not ms:
        return 0
    then = datetime.fromtimestamp(int(ms) / 1000, tz=IST)
    return max(0, (datetime.now(IST).date() - then.date()).days)


# ── Data collection ───────────────────────────────────────────────────────────

def fetch_workspace_tasks(client: ClickUpClient, team_id: str) -> list:
    """All ROOT tasks (no subtasks) across every space/list, open and closed."""
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
    """Support list tickets, main tasks only, including closed ones (needed to
    count what was resolved during the week)."""
    return client.get_tasks(CLICKUP_SUPPORT_LIST_ID, include_closed=True, include_subtasks=False)


def fetch_time_by_person_task(client: ClickUpClient, team_id: str) -> Dict[str, Dict[str, int]]:
    """person username -> {task_id: total_ms} for this reporting week."""
    start_ms, end_ms = _last_week_range_ms()
    members = client.get_workspace_members(team_id)
    print(f'Fetching time entries for {len(members)} member(s), this reporting week...')
    person_task_ms: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    seen_entry_ids: Set[str] = set()
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
            # A member can be returned more than once by ClickUp; never let the
            # same entry be counted twice.
            entry_id = e.get('id')
            if entry_id and entry_id in seen_entry_ids:
                continue
            if entry_id:
                seen_entry_ids.add(entry_id)
            task = e.get('task') or {}
            tid = task.get('id')
            try:
                duration = int(e.get('duration', 0))
            except (TypeError, ValueError):
                duration = 0
            if tid and duration > 0:
                person_task_ms[username][tid] += duration
    return {person: tasks for person, tasks in person_task_ms.items() if tasks}


def _flatten_task_ms(person_task_ms: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    """Sum across all people -> task_id -> total ms."""
    task_ms: Dict[str, int] = defaultdict(int)
    for person, tasks in person_task_ms.items():
        for tid, ms in tasks.items():
            task_ms[tid] += ms
    return task_ms


# ── Section builders ────────────────────────────────────────────────────────

def build_onboarding(all_tasks: list, task_ms: Dict[str, int]) -> dict:
    """Onboarding clients with activity last week, closed tasks excluded."""
    start_ms, end_ms = _last_week_range_ms()
    by_client = defaultdict(list)
    skipped_closed = 0
    skipped_stale = 0

    for t in all_tasks:
        if 'onboarding' not in _project_name(t).lower():
            continue
        client_name = _client_name_from_task(t)
        if not client_name or 'niro' in client_name.lower():
            continue
        if _is_closed(t):
            skipped_closed += 1
            continue
        if not _touched_last_week(t, start_ms, end_ms, task_ms):
            skipped_stale += 1
            continue
        by_client[client_name].append(t)

    print(f'  Onboarding: {len(by_client)} client(s) active last week '
          f'({skipped_closed} closed task(s) hidden, {skipped_stale} with no activity last week)')

    result = {}
    for client_name, tasks in by_client.items():
        most_recent = max(tasks, key=lambda t: int(t.get('date_updated', 0) or 0))
        status = (most_recent.get('status', {}).get('status') or 'Unknown').title()
        created_ms = [int(t.get('date_created', 0) or 0) for t in tasks if t.get('date_created')]
        duration_days = _days_since(min(created_ms)) if created_ms else 0
        last_activity_ms = max((int(t.get('date_updated', 0) or 0) for t in tasks), default=0)
        result[client_name] = {
            'status': status,
            'duration_days': duration_days,
            'last_activity_ms': last_activity_ms,
        }
    return result


def build_resource_tracking(client: ClickUpClient, all_tasks: list,
                            person_task_ms: Dict[str, Dict[str, int]]) -> dict:
    """resource -> {'total_ms': int, 'rows': [{'project', 'tracked_ms'}, ...]}

    Rows are aggregated PER PROJECT, not per task — the previous version
    emitted one row per task, so a person who logged nine separate sessions
    against "Rituals" got nine identical-looking rows.
    """
    task_by_id = {t['id']: t for t in all_tasks}
    lookup_cache: Dict[str, dict] = {}

    def _resolve_task(tid: str):
        if tid in task_by_id:
            return task_by_id[tid]
        if tid in lookup_cache:
            return lookup_cache[tid]
        try:
            fetched = client._get(f'task/{tid}')
            lookup_cache[tid] = fetched
            return fetched
        except Exception as exc:
            print(f'  [warn] Could not look up task {tid} (likely a subtask): {exc}')
            lookup_cache[tid] = None
            return None

    def _resolve_module(task: dict) -> str:
        module = _product_module_name(task)
        if module not in ('N/A', ''):
            return module
        parent_id = task.get('parent')
        if parent_id:
            parent_task = _resolve_task(parent_id)
            if parent_task:
                parent_module = _product_module_name(parent_task)
                if parent_module not in ('N/A', ''):
                    return parent_module
        return module

    result = {}
    for person, task_times in person_task_ms.items():
        per_project: Dict[str, int] = defaultdict(int)
        total_ms = 0
        for tid, ms in task_times.items():
            task = _resolve_task(tid)
            project = _resolve_module(task) if task else 'Unknown'
            per_project[project] += ms
            total_ms += ms
        if not per_project:
            continue
        rows = [{'project': name, 'tracked_ms': ms} for name, ms in per_project.items()]
        rows.sort(key=lambda r: -r['tracked_ms'])
        result[person] = {'total_ms': total_ms, 'rows': rows}
    return result


def build_customer_support(tickets: list, task_ms: Dict[str, int]) -> dict:
    """Support tickets that saw activity last week, grouped by client."""
    start_ms, end_ms = _last_week_range_ms()
    by_client = defaultdict(lambda: {'total': 0, 'resolved': 0, 'pending': 0,
                                     'tracked_ms': 0, 'resources': set(), 'types': set()})
    considered = 0
    for t in tickets:
        if not _touched_last_week(t, start_ms, end_ms, task_ms):
            continue
        considered += 1
        client_name = _client_name_from_task(t) or 'Unspecified'
        entry = by_client[client_name]
        entry['total'] += 1
        if _is_closed(t):
            entry['resolved'] += 1
        else:
            entry['pending'] += 1
        entry['tracked_ms'] += task_ms.get(t['id'], 0)
        entry['resources'].update(_assignees(t))
        ticket_type = _ticket_type_from_task(t)
        if ticket_type:
            entry['types'].add(ticket_type)

    print(f'  Customer Support: {considered} of {len(tickets)} ticket(s) had activity last week')
    return by_client


# ── HTML builder ──────────────────────────────────────────────────────────────

def _slab(label: str, color: str) -> str:
    return (f'<tr><td style="background:{color};padding:10px 32px;">'
            f'<div style="color:#fff;font-size:11px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:2px;">{label}</div></td></tr>')


def _section_title(label: str) -> str:
    return (f'<div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;'
            f'letter-spacing:1.5px;margin-bottom:14px;">&#9632; {label}</div>')


def _milestone_block(milestones: dict) -> str:
    """Completed / In Progress milestone lists for one resource."""
    completed = milestones.get('completed', [])
    in_progress = milestones.get('in_progress', [])
    if not completed and not in_progress:
        return ''

    def rows(items, color, bg, border):
        out = ''
        for name, status in items:
            badge = (f'<span style="display:inline-block;margin-left:8px;padding:1px 7px;'
                     f'border-radius:8px;background:{bg};color:{color};font-size:10px;'
                     f'font-weight:700;border:1px solid {border};">{status}</span>'
                     ) if status else ''
            out += (f'<div style="font-size:12px;color:#0f172a;padding:3px 0;">'
                    f'&bull; {name}{badge}</div>')
        return out

    blocks = ''
    if completed:
        blocks += (f'<div style="font-size:11px;font-weight:700;color:#059669;'
                   f'text-transform:uppercase;letter-spacing:0.5px;margin:10px 0 2px;">'
                   f'&#10003; Completed ({len(completed)})</div>'
                   f'{rows(completed, "#059669", "#ECFDF5", "#A7F3D0")}')
    if in_progress:
        blocks += (f'<div style="font-size:11px;font-weight:700;color:#B45309;'
                   f'text-transform:uppercase;letter-spacing:0.5px;margin:10px 0 2px;">'
                   f'&#9203; In Progress ({len(in_progress)})</div>'
                   f'{rows(in_progress, "#B45309", "#FFFBEB", "#FDE68A")}')

    return (f'<div style="border-top:1px solid #f1f5f9;margin-top:10px;padding-top:2px;">'
            f'<div style="font-size:11px;font-weight:700;color:#64748b;'
            f'text-transform:uppercase;letter-spacing:1px;">Milestones</div>'
            f'{blocks}</div>')


def build_email_html(onboarding: dict, resource_tracking: dict, support: dict, report_date: date,
                     milestones: dict = None) -> str:
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
                           'No onboarding activity last week.</td></tr>')

    # ── Section 2: Resource → Project Tracking ──
    tracking_html = ''
    for resource, data in sorted(resource_tracking.items(), key=lambda kv: -kv[1]['total_ms']):
        rows = ''.join(
            f'<tr><td style="padding:6px 10px;font-size:12px;color:#0f172a;">{r["project"]}</td>'
            f'<td style="padding:6px 10px;font-size:12px;color:#64748b;text-align:right;">'
            f'{_fmt_duration(r["tracked_ms"])}</td></tr>'
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
        <tr><th style="text-align:left;padding:6px 10px;font-size:11px;color:#94a3b8;">Project Tracking</th>
            <th style="text-align:right;padding:6px 10px;font-size:11px;color:#94a3b8;">Tracked Time</th></tr>
        {rows}
      </table>
      {_milestone_block((milestones or {}).get(resource, {}))}
    </div>'''
    if not tracking_html:
        tracking_html = '<div style="color:#94a3b8;font-size:13px;">No time tracked last week.</div>'

    # ── Section 3: Customer Support ──
    support_rows = ''
    for client_name, d in sorted(support.items()):
        types = ', '.join(sorted(d['types'])) if d['types'] else '—'
        support_rows += (
            f'<tr><td style="padding:8px 12px;font-size:12px;color:#0f172a;font-weight:600;">{client_name}</td>'
            f'<td style="padding:8px 12px;font-size:12px;color:#475569;">{types}</td>'
            f'<td style="padding:8px 12px;font-size:12px;text-align:center;">{d["total"]}</td>'
            f'<td style="padding:8px 12px;font-size:12px;text-align:center;color:#166534;">{d["resolved"]}</td>'
            f'<td style="padding:8px 12px;font-size:12px;text-align:center;color:#b45309;">{d["pending"]}</td>'
            f'<td style="padding:8px 12px;font-size:12px;">{_fmt_duration(d["tracked_ms"])}</td>'
            f'<td style="padding:8px 12px;font-size:12px;color:#64748b;">{", ".join(sorted(d["resources"]))}</td></tr>'
        )
    if not support_rows:
        support_rows = ('<tr><td colspan="7" style="padding:12px;color:#94a3b8;font-size:13px;">'
                        'No support ticket activity last week.</td></tr>')

    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:720px;margin:0 auto;">

  <tr><td style="border-radius:20px 20px 0 0;overflow:hidden;background:linear-gradient(135deg,#0f2744 0%,#1a56a0 100%);padding:36px 32px;">
    <div style="color:#dbeafe;font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;">Weekly Ops Report &middot; {week_label}</div>
    <div style="color:#fff;font-size:28px;font-weight:800;">Weekly Report</div>
    <div style="color:rgba(255,255,255,0.7);font-size:14px;margin-top:4px;">Sent {today_str} &middot; last week only</div>
  </td></tr>
  <tr><td style="height:4px;background:linear-gradient(90deg,#6366F1,#A855F7,#EC4899);"></td></tr>

  {_slab('&#128188; Onboarding', '#7c3aed')}
  <tr><td style="background:#fff;padding:20px 32px;">
    {_section_title(f'Active Onboarding Clients — {week_label}')}
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

  {_slab('&#128100; Resource → Project Tracking', '#1a56a0')}
  <tr><td style="background:#fff;padding:20px 32px;">{_section_title(f'Last Week — {week_label}')}{tracking_html}</td></tr>

  {_slab('&#127919; Customer Support', '#166534')}
  <tr><td style="background:#fff;padding:20px 32px 32px;border-radius:0 0 20px 20px;">
    {_section_title(f'Tickets by Client — {week_label}')}
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
      <tr style="background:#f0fdf4;">
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Client</th>
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Ticket Type</th>
        <th style="padding:8px 12px;font-size:11px;color:#64748b;">Total</th>
        <th style="padding:8px 12px;font-size:11px;color:#64748b;">Resolved</th>
        <th style="padding:8px 12px;font-size:11px;color:#64748b;">Pending</th>
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Tracked</th>
        <th style="text-align:left;padding:8px 12px;font-size:11px;color:#64748b;">Resource(s)</th>
      </tr>
      {support_rows}
    </table>
    <div style="font-size:11px;color:#94a3b8;margin-top:10px;">
      Counts cover tickets created, updated, closed or time-tracked during {week_label} only.
    </div>
  </td></tr>

</table>
</body>
</html>'''


# ── Email sender ──────────────────────────────────────────────────────────────

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


def send_email(html: str, subject: str, cfg: dict) -> None:
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = cfg['from']
    to_list = _parse_addresses(cfg.get('to'))
    if not to_list:
        raise SystemExit('No recipients configured — check the EMAIL_TO secret.')
    # Never assign the raw secret to a header: a newline in it makes Python
    # refuse to write the message at all.
    msg['To'] = ', '.join(to_list)
    msg.attach(MIMEText(html, 'html'))

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

    start_ms, end_ms = _last_week_range_ms()
    print('=== Weekly ClickUp Ops Report ===')
    print(f'Reporting window: {_last_week_display()}')
    print(f'  {datetime.fromtimestamp(start_ms / 1000, tz=IST)} '
          f'-> {datetime.fromtimestamp(end_ms / 1000, tz=IST)}\n')

    print('Fetching ALL root tasks (open + closed, no subtasks) workspace-wide...')
    all_tasks = fetch_workspace_tasks(client, team_id)
    print(f'Total root tasks: {len(all_tasks)}\n')

    print('Fetching support tickets (open + closed, no subtasks)...')
    support_tickets = fetch_support_tickets(client)
    print(f'Total support tickets in list: {len(support_tickets)}\n')

    person_task_ms = fetch_time_by_person_task(client, team_id)
    task_ms = _flatten_task_ms(person_task_ms)
    print(f'Time entries found for {len(task_ms)} distinct task(s) across '
          f'{len(person_task_ms)} people\n')

    print('Applying strict last-week filter...')
    onboarding = build_onboarding(all_tasks, task_ms)
    resource_tracking = build_resource_tracking(client, all_tasks, person_task_ms)
    milestones = build_resource_milestones(all_tasks, task_ms)
    support = build_customer_support(support_tickets, task_ms)

    if _TICKET_TYPE_FIELDS_SEEN:
        print(f'  Ticket Type read from custom field(s): '
              f'{", ".join(sorted(_TICKET_TYPE_FIELDS_SEEN))}')
    else:
        print('  [warn] No "Ticket Type" custom field found on any ticket — '
              'the Ticket Type column will show "—". Check the field name in ClickUp.')

    print(f'\nSummary (last week only): {len(onboarding)} onboarding client(s), '
          f'{len(resource_tracking)} resource(s) with tracked time, '
          f'{len(support)} client(s) in support')

    report_date = datetime.now(IST).date()
    html = build_email_html(onboarding, resource_tracking, support, report_date,
                            milestones=milestones)
    subject = f'Weekly Ops Report — {_last_week_display()}'

    send_email(html, subject, {
        'from': gmail_from,
        'app_password': gmail_password,
        'to': email_to,
    })


if __name__ == '__main__':
    main()
