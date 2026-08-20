#!/usr/bin/env python3
"""
Monthly ClickUp Ops Report
Runs on the 1st of each month and reports the CALENDAR MONTH that just ended.
(1 Sep -> August; 1 Aug -> July.)

Output mirrors the team's reference spreadsheet exactly:

    Sl No | Activity | TS by <Name> | ... | Total
      1   | New Dev  |    47        | ... | 130.25
                       ...
          | Total    |   181.5      | ... | 553.85

  - "Activity"  = the task's Product Module custom field.
  - "Resource"  = the person who logged the time (ClickUp time-entry owner).
  - "TS"        = Time Spent, in decimal hours (78, 9.5, 6.75, 46.6 ...),
                  exactly how the reference sheet writes it. Zero renders as "-".

Plus a team-wide donut of time spent per project, and per-resource totals.

ASSUMPTIONS
- "Product Module" = a custom field whose name contains "module" or "product".
  A task with none set is reported under "Unspecified" rather than dropped, so
  the grand total always reconciles with ClickUp.
- Subtasks inherit their parent's Product Module when they don't set one
  themselves (same rule the weekly report uses).
- Activity rows and resource columns are BOTH derived from the data and sorted
  by total hours descending. Nothing is hardcoded, so a new module in ClickUp
  shows up automatically.
- Reporting window is IST (UTC+5:30): 1st 00:00:00.000 -> last day 23:59:59.999.

ENV
  CLICKUP_TOKEN              required
  CLICKUP_WORKSPACE          optional (auto-detected)
  GMAIL_ADDRESS              required
  GMAIL_APP_PASSWORD         required
  EMAIL_TO                   required, comma-separated
  EMAIL_CC                   optional, comma-separated
  REPORT_MONTH               optional 'YYYY-MM' override for backfills
                             (e.g. REPORT_MONTH=2026-07 to re-send July)
"""

import os
import re
import sys
import math
import html
import smtplib
import calendar
import colorsys
from io import BytesIO
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.mime.text import MIMEText
from typing import Dict, List, Tuple

from clickup_client import ClickUpClient

IST = timezone(timedelta(hours=5, minutes=30))
UNSPECIFIED = 'Unspecified'

# Categorical palette, fixed order, never cycled. Validated for colour-vision
# deficiency separation on a light surface (worst adjacent CVD dE 9.1,
# normal-vision dE 19.6). Three of these sit under 3:1 contrast on white, which
# is why every segment also carries a visible label + value in the legend.
SERIES_COLORS = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300']

OTHER_COLOR = '#8a8a85'
MAX_DONUT_SEGMENTS = 6   # part-to-whole reads at a glance only up to ~6 slices

# Billing rate per hour, in rupees, keyed by lowercased ClickUp username.
# A resource with NO rate here is left out of the report entirely — that is how
# people who have left the team drop off (Akshar). Anyone excluded is named in
# the run log, so it can never happen silently.
RESOURCE_RATES = {
    'sai ram': 198,
    'pushpendra singh sikarwar': 234,
    'umesh chandra dani': 391,
    'ashok mehra': 357,
    'vikas sharma': 130,
}

# Expected working hours in a month. Each resource's bar and percentage are
# measured against this, and anything above it is flagged as overflow.
MONTHLY_HOURS_BASELINE = 192


def rate_for(resource: str):
    """Hourly rate for a ClickUp username, or None if they have no rate.

    Matches the full name first, then falls back to the first name, so
    'Pushpendra Singh' and 'pushpendra singh sikarwar' both resolve.
    """
    key = ' '.join((resource or '').lower().split())
    if key in RESOURCE_RATES:
        return RESOURCE_RATES[key]
    parts = key.split()
    if parts:
        for known, rate in RESOURCE_RATES.items():
            if known.split()[0] == parts[0]:
                return rate
    return None


def fmt_inr(amount) -> str:
    """9767.34 -> '9,767'  ·  215000 -> '2,15,000' (Indian grouping)."""
    if not amount:
        return '-'
    whole = f'{int(round(amount)):d}'
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ','.join(groups) + ',' + tail


# -- Period ------------------------------------------------------------------

def report_month() -> Tuple[int, int]:
    """(year, month) to report on. Previous calendar month, or REPORT_MONTH."""
    override = os.environ.get('REPORT_MONTH', '').strip()
    if override:
        try:
            year_s, month_s = override.split('-')
            year, month = int(year_s), int(month_s)
            if not 1 <= month <= 12:
                raise ValueError
            return year, month
        except (ValueError, IndexError):
            raise SystemExit(f"REPORT_MONTH must look like '2026-07', got '{override}'")
    today = datetime.now(IST).date()
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    return last_of_prev_month.year, last_of_prev_month.month


def month_range_ms(year: int, month: int) -> Tuple[int, int]:
    """1st 00:00:00.000 IST through last-day 23:59:59.999 IST, as epoch ms."""
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=IST)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, 999000, tzinfo=IST)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def month_label(year: int, month: int) -> str:
    return f'{calendar.month_name[month]} {year}'


def month_range_label(year: int, month: int) -> str:
    last_day = calendar.monthrange(year, month)[1]
    abbr = calendar.month_abbr[month]
    return f'1 {abbr} - {last_day} {abbr} {year}'


# -- Formatting --------------------------------------------------------------

def hours(ms: int) -> float:
    """Milliseconds -> decimal hours, rounded to 2dp like the reference sheet."""
    return round(int(ms) / 3600000.0, 2)


def fmt_hours(value: float, dash_on_zero: bool = True) -> str:
    """78.0 -> '78', 9.50 -> '9.5', 553.85 -> '553.85', 0 -> '-'."""
    if not value:
        return '-' if dash_on_zero else '0'
    text = f'{value:.2f}'.rstrip('0').rstrip('.')
    return text or '0'


def esc(text) -> str:
    return html.escape(str(text if text is not None else ''), quote=True)


# -- Product module extraction ----------------------------------------------

def product_module_name(task: dict) -> str:
    """Read 'Product Module' off a custom field (drop_down / labels / text)."""
    for field in (task.get('custom_fields') or []):
        name_lower = (field.get('name') or '').lower()
        if 'module' not in name_lower and 'product' not in name_lower:
            continue
        value = field.get('value')
        if value is None or value == '':
            continue
        field_type = field.get('type', '')
        options = (field.get('type_config') or {}).get('options', [])
        if field_type == 'drop_down':
            for opt in options:
                if str(opt.get('orderindex', '')) == str(value) or opt.get('id') == str(value):
                    return (opt.get('name') or str(value)).strip()
            try:
                return options[int(value)]['name'].strip()
            except (IndexError, ValueError, TypeError, KeyError):
                return str(value).strip()
        if field_type == 'labels':
            matched = [o.get('name') for o in options if o.get('id') in (value or [])]
            matched = [m for m in matched if m]
            if matched:
                return ', '.join(matched)
            continue
        text = str(value).strip()
        if text:
            return text
    return ''


# -- Data collection ---------------------------------------------------------

def fetch_time_by_person_task(client: ClickUpClient, team_id: str,
                              start_ms: int, end_ms: int) -> Dict[str, Dict[str, int]]:
    """resource username -> {task_id: total_ms} for the reporting month."""
    members = client.get_workspace_members(team_id)
    print(f'Fetching time entries for {len(members)} workspace member(s)...')

    person_task_ms: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    seen_entry_ids = set()   # a member can appear twice; never double-count an entry

    for member in members:
        uid = member.get('id')
        username = (member.get('username') or member.get('email') or '').strip()
        if not uid or not username:
            continue
        try:
            entries = client.get_time_entries(team_id, start_ms, end_ms, assignee_id=uid)
        except Exception as exc:
            print(f'  [warn] Could not fetch time entries for {username}: {exc}')
            continue

        for entry in entries:
            entry_id = entry.get('id')
            if entry_id and entry_id in seen_entry_ids:
                continue
            if entry_id:
                seen_entry_ids.add(entry_id)
            task_id = (entry.get('task') or {}).get('id')
            try:
                duration = int(entry.get('duration', 0))
            except (TypeError, ValueError):
                duration = 0
            if task_id and duration > 0:
                person_task_ms[username][task_id] += duration

        logged = sum(person_task_ms[username].values())
        if logged:
            print(f'  {username}: {fmt_hours(hours(logged))}h across '
                  f'{len(person_task_ms[username])} task(s)')

    # Reading person_task_ms[username] above inserts an empty entry for members
    # who logged nothing (defaultdict). Drop them, so "no time tracked at all"
    # is an empty dict rather than a dict full of empty dicts.
    return {person: tasks for person, tasks in person_task_ms.items() if tasks}


def fetch_workspace_tasks(client: ClickUpClient, team_id: str) -> list:
    """All root tasks (open + closed) workspace-wide — the bulk module source.

    Far cheaper than looking every task up one at a time: a handful of list
    calls returns thousands of tasks, and only the leftovers (subtasks) then
    need an individual lookup.
    """
    all_tasks = []
    spaces = client.get_spaces(team_id)
    print(f'Scanning {len(spaces)} space(s) for task metadata...')
    for space in spaces:
        lists = client.get_all_lists(space['id'])
        print(f'  Space: {space.get("name", "?")} - {len(lists)} list(s)')
        for lst in lists:
            try:
                tasks = client.get_tasks(lst['id'], include_closed=True, include_subtasks=False)
            except Exception as exc:
                print(f'    [warn] Could not fetch list {lst.get("name", lst["id"])}: {exc}')
                continue
            all_tasks.extend(tasks)
    return all_tasks


def build_module_resolver(client: ClickUpClient, all_tasks: list):
    """task_id -> project name, with a parent fallback for subtasks."""
    task_by_id = {t['id']: t for t in all_tasks}
    lookup_cache: Dict[str, dict] = {}

    def resolve_task(task_id: str):
        if task_id in task_by_id:
            return task_by_id[task_id]
        if task_id in lookup_cache:
            return lookup_cache[task_id]
        try:
            fetched = client._get(f'task/{task_id}')
        except Exception as exc:
            print(f'  [warn] Could not look up task {task_id}: {exc}')
            fetched = None
        lookup_cache[task_id] = fetched
        return fetched

    def resolve(task_id: str) -> str:
        task = resolve_task(task_id)
        if not task:
            return UNSPECIFIED
        module = product_module_name(task)
        if module:
            return module
        parent_id = task.get('parent')
        if parent_id:
            parent = resolve_task(parent_id)
            if parent:
                parent_module = product_module_name(parent)
                if parent_module:
                    return parent_module
        return UNSPECIFIED

    return resolve


def build_matrix(person_task_ms: Dict[str, Dict[str, int]], resolve_module) -> dict:
    """Pivot into the Project x Resource grid the reference sheet uses."""
    cell_ms: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    project_ms: Dict[str, int] = defaultdict(int)
    resource_ms: Dict[str, int] = defaultdict(int)

    distinct_tasks = {tid for tasks in person_task_ms.values() for tid in tasks}
    print(f'Resolving project (Product Module) for {len(distinct_tasks)} distinct task(s)...')

    for person, task_times in person_task_ms.items():
        for task_id, ms in task_times.items():
            project = resolve_module(task_id)
            cell_ms[project][person] += ms
            project_ms[project] += ms
            resource_ms[person] += ms

    # Both axes dynamic, sorted by total time descending; ties broken by name so
    # the layout is stable when two rows are equal.
    projects = sorted(project_ms, key=lambda p: (-project_ms[p], p.lower()))
    resources = sorted(resource_ms, key=lambda r: (-resource_ms[r], r.lower()))
    grand_ms = sum(resource_ms.values())

    cell_hours = {p: {r: hours(cell_ms[p].get(r, 0)) for r in resources} for p in projects}
    rates = {r: (rate_for(r) or 0) for r in resources}

    # Cost = hours x that person's hourly rate, summed per cell / row / column.
    cell_cost = {p: {r: cell_hours[p][r] * rates[r] for r in resources} for p in projects}
    project_cost = {p: sum(cell_cost[p].values()) for p in projects}
    resource_cost = {r: sum(cell_cost[p][r] for p in projects) for r in resources}

    return {
        'projects': projects,
        'resources': resources,
        'cell_hours': cell_hours,
        'project_hours': {p: hours(project_ms[p]) for p in projects},
        'resource_hours': {r: hours(resource_ms[r]) for r in resources},
        'grand_hours': hours(grand_ms),
        'rates': rates,
        'cell_cost': cell_cost,
        'project_cost': project_cost,
        'resource_cost': resource_cost,
        'grand_cost': sum(resource_cost.values()),
    }


# -- Chart + chrome ----------------------------------------------------------

def project_colors(count: int) -> List[str]:
    """A distinct colour for every project.

    The first six come from the validated categorical palette — those go to the
    projects that dominate the month, and they are the ones the legend names.
    Beyond that, hues are walked in golden-angle steps with the lightness tier
    rotating each round, spreading any number of extra colours around the wheel
    without two neighbours landing on the same shade.
    """
    if count <= len(SERIES_COLORS):
        return SERIES_COLORS[:count]
    colors = list(SERIES_COLORS)
    golden_ratio = 0.618033988749895
    tiers = [(0.60, 0.50), (0.48, 0.65), (0.70, 0.38), (0.38, 0.57)]
    hue, index = 0.11, 0
    while len(colors) < count:
        hue = (hue + golden_ratio) % 1.0
        saturation, lightness = tiers[index % len(tiers)]
        red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
        colors.append('#%02x%02x%02x' % (round(red * 255), round(green * 255), round(blue * 255)))
        index += 1
    return colors


def chart_segments(matrix: dict) -> List[Tuple[str, float, str]]:
    """EVERY project, one slice each — this is what the donut draws."""
    project_hours = matrix['project_hours']
    ranked = [(p, project_hours[p]) for p in matrix['projects'] if project_hours[p] > 0]
    palette = project_colors(len(ranked))
    return [(name, hrs, palette[i]) for i, (name, hrs) in enumerate(ranked)]


def donut_segments(matrix: dict) -> List[Tuple[str, float, str]]:
    """Top projects by hours, tail folded into 'Other'. Never cycles hues."""
    projects = matrix['projects']
    project_hours = matrix['project_hours']
    ranked = [(p, project_hours[p]) for p in projects if project_hours[p] > 0]

    if len(ranked) <= MAX_DONUT_SEGMENTS:
        head, tail = ranked, []
    else:
        head, tail = ranked[:MAX_DONUT_SEGMENTS - 1], ranked[MAX_DONUT_SEGMENTS - 1:]

    segments = [(name, hrs, SERIES_COLORS[i]) for i, (name, hrs) in enumerate(head)]
    if tail:
        segments.append((f'Other ({len(tail)} projects)',
                         round(sum(h for _, h in tail), 2), OTHER_COLOR))
    return segments


def donut_png(segments: List[Tuple[str, float, str]], total: float,
              size: int = 420, thickness: int = 105, supersample: int = 3):
    """Render the donut as a PNG, returned as raw bytes (or None).

    It has to be a real image, not inline SVG: Gmail strips <svg> entirely,
    which left the chart missing and its <text> labels dumped into the page as
    stray words. A PNG attached with a Content-ID renders in Gmail, Outlook,
    Apple Mail and every mobile client.

    No text is drawn inside the image — the legend beside it carries every
    name, hour figure and percentage, so the output never depends on a font
    being installed on the build runner.
    """
    if not segments or total <= 0:
        return None
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print('  [warn] Pillow not installed — donut image skipped '
              '(the legend still carries the full breakdown).')
        return None

    canvas = size * supersample
    ring = thickness * supersample
    if len(segments) <= 1:
        gap_degrees = 0.0
    elif len(segments) <= 8:
        gap_degrees = 1.2
    else:
        gap_degrees = 0.5

    def rgb(hex_color: str):
        """'#2a78d6' -> (42, 120, 214). Passed as a tuple rather than a string
        so no Pillow build can mis-parse the colour."""
        h = hex_color.lstrip('#')
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    image = Image.new('RGB', (canvas, canvas), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    box = [0, 0, canvas - 1, canvas - 1]

    # Track ring, so rounding gaps never show as white wedges.
    draw.ellipse(box, fill=(238, 241, 245))

    angle = -90.0
    for _, value, color in segments:
        if value <= 0:
            continue
        sweep = value / total * 360.0

        # THE GREEN-DONUT BUG. A tiny project (0.5h of 880h is 0.2 degrees)
        # is narrower than the separator gap, so `start + gap/2` lands AFTER
        # `end - gap/2`. Pillow reads start > end as "sweep clockwise the long
        # way round" and paints ~78% of the canvas in that project's colour,
        # burying every slice drawn before it. Shrink the gap to fit the slice,
        # and never let the end angle fall behind the start.
        gap = min(gap_degrees, sweep * 0.5)
        start = angle + gap / 2
        end = angle + sweep - gap / 2
        if end <= start:
            end = start + 0.02          # hairline, but never a full circle

        draw.pieslice(box, start, end, fill=rgb(color))
        angle += sweep

    draw.ellipse([ring, ring, canvas - 1 - ring, canvas - 1 - ring], fill=(255, 255, 255))

    image = image.resize((size, size), Image.LANCZOS)

    # Prove, server-side, what actually landed in the pixels. If this reports a
    # single colour the fault is here; if it reports many but the email shows
    # one, the fault is in the mail client rendering the image.
    try:
        sampled = image.convert('RGB').getcolors(maxcolors=200000) or []
        sampled = [c for c in sampled
                   if c[1] not in ((255, 255, 255), (238, 241, 245))]
        sampled.sort(reverse=True)
        summary = ', '.join('#%02x%02x%02x(%d)' % (c[1][0], c[1][1], c[1][2], c[0])
                            for c in sampled[:6])
        print(f'  chart pixels: {len(sampled)} distinct colour(s); top: {summary}')
    except Exception as exc:
        print(f'  [warn] could not sample chart pixels: {exc}')

    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def share_bar_rows(segments: List[Tuple[str, float, str]], total: float) -> str:
    """Donut legend: colour, name, hours, percent, then the bar.

    Column order is name, bar, hours, percent.

    The bar carries a FIXED width rather than width:100%. When it was elastic
    it expanded to fill the row and pushed the hours and percent columns off
    the right edge in narrower mail windows — the figures were in the HTML but
    invisible. Fixing the bar keeps the layout identical while making that
    impossible.
    """
    rows = ''
    for name, value, color in segments:
        pct = (value / total * 100) if total else 0
        # Bar length is the share of the month, matching the percent column.
        width = max(2, round(pct))
        rows += (
            f'<tr>'
            # nowrap on the CELL, not just the text — otherwise the browser is
            # free to break between the colour swatch and the label, dropping
            # longer project names onto a second line.
            f'<td style="padding:7px 18px 7px 0;white-space:nowrap;width:1%;">'
            f'<span style="display:inline-block;width:11px;height:11px;border-radius:3px;'
            f'background:{color};margin-right:9px;"></span>'
            f'<span style="font-size:13px;color:#0f172a;">{esc(name)}</span></td>'
            # Elastic track: it takes the leftover width of the row so the bar
            # runs the full span between the project name and the figures.
            f'<td style="padding:7px 0;width:100%;min-width:90px;">'
            f'<div style="background:#eef1f5;border-radius:4px;height:10px;">'
            f'<div style="width:{width}%;background:{color};height:10px;border-radius:4px;"></div>'
            f'</div></td>'
            f'<td width="66" style="padding:7px 0 7px 12px;white-space:nowrap;text-align:right;'
            f'font-size:13px;color:#0f172a;font-weight:700;">{esc(fmt_hours(value))}h</td>'
            f'<td width="48" style="padding:7px 0 7px 8px;white-space:nowrap;text-align:right;'
            f'font-size:12px;color:#64748b;">{pct:.1f}%</td>'
            f'</tr>'
        )
    return rows


def _slab(label: str, color: str) -> str:
    return (f'<tr><td style="background:{color};padding:10px 32px;">'
            f'<div style="color:#fff;font-size:11px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:2px;">{label}</div></td></tr>')


def _section_title(label: str) -> str:
    return (f'<div style="font-size:12px;font-weight:700;color:#0f2744;text-transform:uppercase;'
            f'letter-spacing:1.5px;margin-bottom:14px;">&#9632; {esc(label)}</div>')


def _stat_tile(value: str, label: str) -> str:
    return (f'<td width="25%" align="center" style="padding:16px 8px;background:#f8fafc;'
            f'border-right:1px solid #e8edf3;">'
            f'<div style="font-size:22px;font-weight:800;color:#0f2744;">{esc(value)}</div>'
            f'<div style="font-size:10px;font-weight:700;color:#64748b;text-transform:uppercase;'
            f'letter-spacing:1px;margin-top:4px;">{esc(label)}</div></td>')


# -- HTML --------------------------------------------------------------------

def build_email_html(matrix: dict, year: int, month: int, sent_on: datetime,
                     has_chart: bool = False) -> str:
    projects = matrix['projects']
    resources = matrix['resources']
    grand = matrix['grand_hours']
    label = month_label(year, month)
    range_label = month_range_label(year, month)

    if not projects:
        body = ('<tr><td style="background:#fff;padding:32px;border-radius:0 0 20px 20px;'
                'color:#94a3b8;font-size:14px;">No time was tracked in ClickUp for this '
                'period.</td></tr>')
    else:
        # -- Matrix -------------------------------------------------------
        head = ('<tr style="background:#eef4fc;">'
                '<th style="text-align:left;padding:9px 10px;font-size:11px;color:#475569;'
                'border-bottom:1px solid #dbe4ef;">S. No</th>'
                '<th style="text-align:left;padding:9px 10px;font-size:11px;color:#475569;'
                'border-bottom:1px solid #dbe4ef;">Project</th>')
        for resource in resources:
            head += (f'<th style="text-align:right;padding:9px 10px;font-size:11px;color:#475569;'
                     f'border-bottom:1px solid #dbe4ef;white-space:nowrap;">'
                     f'{esc(resource)}</th>')
        head += ('<th style="text-align:right;padding:9px 10px;font-size:11px;color:#0f2744;'
                 'border-bottom:1px solid #dbe4ef;background:#e3ecf8;">Total</th>'
                 '<th style="text-align:right;padding:9px 10px;font-size:11px;color:#0f2744;'
                 'border-bottom:1px solid #dbe4ef;background:#dce8f7;white-space:nowrap;">'
                 'Total Cost (&#8377;)</th></tr>')

        rows = ''
        for index, project in enumerate(projects, start=1):
            stripe = '#ffffff' if index % 2 else '#fbfcfe'
            rows += (f'<tr style="background:{stripe};">'
                     f'<td style="padding:8px 10px;font-size:12px;color:#94a3b8;'
                     f'border-bottom:1px solid #f1f5f9;">{index}</td>'
                     f'<td style="padding:8px 10px;font-size:13px;color:#0f172a;font-weight:600;'
                     f'border-bottom:1px solid #f1f5f9;">{esc(project)}</td>')
            for resource in resources:
                value = matrix['cell_hours'][project].get(resource, 0)
                cost = matrix['cell_cost'][project].get(resource, 0)
                color = '#0f172a' if value else '#cbd5e1'
                # hours on top, that cell's cost in brackets underneath
                cost_line = (f'<div style="font-size:11px;color:#64748b;margin-top:2px;">'
                             f'(&#8377;{esc(fmt_inr(cost))})</div>') if value else ''
                rows += (f'<td style="padding:8px 10px;font-size:13px;text-align:right;'
                         f'color:{color};border-bottom:1px solid #f1f5f9;">'
                         f'{esc(fmt_hours(value))}{cost_line}</td>')
            rows += (f'<td style="padding:8px 10px;font-size:13px;text-align:right;'
                     f'font-weight:700;color:#0f2744;background:#f5f9ff;'
                     f'border-bottom:1px solid #f1f5f9;">'
                     f'{esc(fmt_hours(matrix["project_hours"][project]))}</td>'
                     f'<td style="padding:8px 10px;font-size:13px;text-align:right;'
                     f'font-weight:700;color:#0f2744;background:#eef4fc;'
                     f'border-bottom:1px solid #f1f5f9;white-space:nowrap;">'
                     f'&#8377;{esc(fmt_inr(matrix["project_cost"][project]))}</td></tr>')

        total_row = ('<tr style="background:#eef4fc;">'
                     '<td style="padding:10px;"></td>'
                     '<td style="padding:10px;font-size:13px;font-weight:800;color:#0f2744;'
                     'text-align:right;">Total</td>')
        for resource in resources:
            total_row += (f'<td style="padding:10px;font-size:13px;text-align:right;'
                          f'font-weight:800;color:#0f2744;">'
                          f'{esc(fmt_hours(matrix["resource_hours"][resource]))}'
                          f'<div style="font-size:11px;font-weight:700;color:#475569;'
                          f'margin-top:2px;">(&#8377;'
                          f'{esc(fmt_inr(matrix["resource_cost"][resource]))})</div></td>')
        total_row += (f'<td style="padding:10px;font-size:13px;text-align:right;font-weight:800;'
                      f'color:#fff;background:#1a56a0;">{esc(fmt_hours(grand))}</td>'
                      f'<td style="padding:10px;font-size:13px;text-align:right;font-weight:800;'
                      f'color:#fff;background:#0f2744;white-space:nowrap;">'
                      f'&#8377;{esc(fmt_inr(matrix["grand_cost"]))}</td></tr>')

        matrix_html = (
            '<div style="overflow-x:auto;">'
            '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="border:1px solid #dbe4ef;border-radius:8px;overflow:hidden;">'
            f'{head}{rows}{total_row}</table></div>'
            '<div style="font-size:11px;color:#94a3b8;margin-top:10px;">'
            'All figures are time spent, in hours. &ldquo;-&rdquo; means no time logged. '
            'Cost in brackets = hours &times; that resource&rsquo;s hourly rate.</div>'
        )

        # -- Donut --------------------------------------------------------
        segments = donut_segments(matrix)      # top 5 + Other, for the legend
        # The chart is a real PNG attached with a Content-ID. Inline SVG is not
        # an option: Gmail strips <svg> and leaves its text nodes loose on the
        # page. If the image could not be built, the share bars alone still
        # carry the whole breakdown.
        chart_cell = ''
        if has_chart:
            chart_cell = f'''
        <td width="216" valign="middle" align="center" style="padding-right:16px;">
          <img src="cid:donutchart" width="200" height="200" alt="Time spent by project"
               style="display:block;border:0;outline:none;text-decoration:none;">
          <div style="font-size:20px;font-weight:800;color:#0f172a;margin-top:8px;">
            {esc(fmt_hours(grand))}
          </div>
          <div style="font-size:10px;font-weight:700;color:#64748b;letter-spacing:1px;">
            TOTAL HOURS
          </div>
        </td>'''
        donut_html = f'''
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>{chart_cell}
        <td valign="middle">
          <table width="100%" cellpadding="0" cellspacing="0">{share_bar_rows(segments, grand)}</table>
        </td>
      </tr>
    </table>'''

        # -- Resource totals ----------------------------------------------
        # Bars are measured against the monthly baseline, not against each other.
        # The track represents whichever is larger: the baseline, or the highest
        # actual figure — so an overflow has somewhere to be drawn. Hours up to
        # the baseline are blue; anything beyond it is red.
        baseline = MONTHLY_HOURS_BASELINE
        highest = max(matrix['resource_hours'].values(), default=0)
        scale = max(baseline, highest) or 1
        resource_rows = ''
        for resource in resources:
            value = matrix['resource_hours'][resource]
            pct_of_baseline = (value / baseline * 100) if baseline else 0
            within = min(value, baseline)
            over = max(0.0, round(value - baseline, 2))
            blue_width = max(1, round(within / scale * 100))
            red_width = round(over / scale * 100) if over else 0
            over_note = (f'<div style="font-size:11px;color:#dc2626;font-weight:700;'
                         f'margin-top:2px;">+{esc(fmt_hours(over))}h over</div>') if over else ''
            pct_color = '#dc2626' if over else '#64748b'
            resource_rows += (
                f'<tr>'
                f'<td style="padding:7px 12px 7px 0;white-space:nowrap;font-size:13px;'
                f'color:#0f172a;font-weight:600;vertical-align:top;">{esc(resource)}'
                f'<div style="font-size:11px;color:#64748b;font-weight:400;margin-top:2px;">'
                f'&#8377;{esc(fmt_inr(matrix["resource_cost"][resource]))}</div></td>'
                f'<td style="padding:7px 0;width:100%;min-width:80px;vertical-align:top;">'
                f'<div style="background:#eef1f5;border-radius:4px;height:10px;'
                f'font-size:0;line-height:0;">'
                f'<div style="display:inline-block;width:{blue_width}%;background:#2a78d6;'
                f'height:10px;border-radius:4px 0 0 4px;"></div>'
                + (f'<div style="display:inline-block;width:{red_width}%;background:#dc2626;'
                   f'height:10px;border-radius:0 4px 4px 0;"></div>' if red_width else '')
                + f'</div></td>'
                f'<td width="76" style="padding:7px 0 7px 14px;text-align:right;white-space:nowrap;'
                f'font-size:13px;font-weight:700;color:#0f172a;vertical-align:top;">'
                f'{esc(fmt_hours(value))}h{over_note}</td>'
                f'<td width="56" style="padding:7px 0 7px 10px;text-align:right;white-space:nowrap;'
                f'font-size:12px;font-weight:700;color:{pct_color};vertical-align:top;">'
                f'{pct_of_baseline:.1f}%</td>'
                f'</tr>'
            )

        stats = (f'<tr>{_stat_tile(fmt_hours(grand), "Total Hours")}'
                 f'{_stat_tile(str(len(resources)), "Resources")}'
                 f'{_stat_tile(str(len(projects)), "Projects")}'
                 f'{_stat_tile("₹" + fmt_inr(matrix["grand_cost"]), "Total Cost")}</tr>')

        body = f'''
  <tr><td style="background:#fff;padding:0;"><table width="100%" cellpadding="0" cellspacing="0">{stats}</table></td></tr>

  {_slab('&#128202; Project &times; Resource', '#1a56a0')}
  <tr><td style="background:#fff;padding:20px 32px;">
    {_section_title(f'Time spent - {label}')}
    {matrix_html}
  </td></tr>

  {_slab('&#9201; Time Spent by Project', '#7c3aed')}
  <tr><td style="background:#fff;padding:20px 32px;">
    {_section_title('Share of total hours')}
    {donut_html}
  </td></tr>

  {_slab('&#128100; Hours by Resource', '#166534')}
  <tr><td style="background:#fff;padding:20px 32px 32px;border-radius:0 0 20px 20px;">
    {_section_title(f'Total logged per person — against a {MONTHLY_HOURS_BASELINE}h baseline')}
    <table width="100%" cellpadding="0" cellspacing="0">{resource_rows}</table>
  </td></tr>'''

    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:760px;margin:0 auto;">

  <tr><td style="border-radius:20px 20px 0 0;overflow:hidden;background:linear-gradient(135deg,#0f2744 0%,#1a56a0 100%);padding:36px 32px;">
    <div style="color:#dbeafe;font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;">Monthly Ops Report &middot; {esc(range_label)}</div>
    <div style="color:#fff;font-size:28px;font-weight:800;">{esc(label)}</div>
    <div style="color:rgba(255,255,255,0.7);font-size:14px;margin-top:4px;">Sent {esc(sent_on.strftime('%A, %d %B %Y'))}</div>
  </td></tr>
  <tr><td style="height:4px;background:linear-gradient(90deg,#6366F1,#A855F7,#EC4899);"></td></tr>
{body}

</table>
</body>
</html>'''


# -- Email -------------------------------------------------------------------

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


def send_email(html_body: str, subject: str, cfg: dict, chart_png=None) -> None:
    # 'related' wraps the HTML plus any cid: images it references. Without it
    # the <img src="cid:donutchart"> would render as a broken image.
    msg = MIMEMultipart('related')
    msg['Subject'] = subject
    msg['From'] = cfg['from']

    to_list = _parse_addresses(cfg.get('to'))
    cc_list = _parse_addresses(cfg.get('cc'))
    if not to_list:
        raise SystemExit('No recipients configured — check the EMAIL_TO secret.')

    # Never assign the raw secret to a header: a newline in it makes Python
    # refuse to write the message at all.
    msg['To'] = ', '.join(to_list)
    if cc_list:
        msg['Cc'] = ', '.join(cc_list)

    alternative = MIMEMultipart('alternative')
    alternative.attach(MIMEText(html_body, 'html'))
    msg.attach(alternative)

    if chart_png:
        inline = MIMEImage(chart_png, _subtype='png')
        inline.add_header('Content-ID', '<donutchart>')
        inline.add_header('Content-Disposition', 'inline', filename='time-by-project.png')
        msg.attach(inline)

        # Same bytes again as a normal attachment. Some clients recolour or fail
        # to render inline CID images; the attached copy can always be opened to
        # see the real chart.
        download = MIMEImage(chart_png, _subtype='png')
        download.add_header('Content-Disposition', 'attachment',
                            filename='time-by-project.png')
        msg.attach(download)

    recipients = to_list + cc_list

    print(f'Sending email via Gmail SMTP to: {", ".join(recipients)}')
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(cfg['from'], cfg['app_password'])
        server.sendmail(cfg['from'], recipients, msg.as_string())
    print('Email sent successfully!')


# -- Entry point -------------------------------------------------------------

def main() -> None:
    api_token = os.environ['CLICKUP_TOKEN']
    team_id = os.environ.get('CLICKUP_WORKSPACE', '').strip()
    gmail_from = os.environ['GMAIL_ADDRESS']
    gmail_password = os.environ['GMAIL_APP_PASSWORD']
    email_to = os.environ['EMAIL_TO']
    email_cc = os.environ.get('EMAIL_CC', '').strip()

    year, month = report_month()
    start_ms, end_ms = month_range_ms(year, month)

    print(f'=== Monthly ClickUp Ops Report - {month_label(year, month)} ===')
    print(f'Window: {datetime.fromtimestamp(start_ms / 1000, tz=IST)} '
          f'-> {datetime.fromtimestamp(end_ms / 1000, tz=IST)}\n')

    client = ClickUpClient(api_token)
    if not team_id:
        team_id = client.get_team_id()
        print(f'Auto-detected team ID: {team_id}')

    person_task_ms = fetch_time_by_person_task(client, team_id, start_ms, end_ms)

    # Only resources with a configured hourly rate are reported. People who
    # have left the team simply have no rate, so they drop out here — and are
    # named in the log so this is never a silent omission.
    unrated = {p: t for p, t in person_task_ms.items() if rate_for(p) is None}
    if unrated:
        for person, tasks in unrated.items():
            dropped = hours(sum(tasks.values()))
            print(f'  [skip] {person}: no hourly rate configured — '
                  f'{fmt_hours(dropped)}h excluded from this report')
        person_task_ms = {p: t for p, t in person_task_ms.items() if p not in unrated}

    if not person_task_ms:
        print('No time entries found for this month — nothing to report.')

    all_tasks = fetch_workspace_tasks(client, team_id) if person_task_ms else []
    print(f'Task metadata cached for {len(all_tasks)} root task(s)\n')

    resolve_module = build_module_resolver(client, all_tasks)
    matrix = build_matrix(person_task_ms, resolve_module)

    print(f'\nSummary: {len(matrix["projects"])} project(s), '
          f'{len(matrix["resources"])} resource(s), '
          f'{fmt_hours(matrix["grand_hours"])}h total')

    sent_on = datetime.now(IST)
    # The ring shows every project; the legend names only the top few.
    # The donut draws EVERY project, each with its own colour.
    # The legend beside it still lists only the top 5 + Other — unchanged.
    chart = chart_segments(matrix)
    chart_png = donut_png(chart, matrix['grand_hours'])
    if chart_png:
        preview = ', '.join(f'{n} {c}' for n, _v, c in chart[:6])
        print(f'Donut chart: built — {len(chart)} slice(s), {len(chart_png):,} bytes')
        print(f'  colours: {preview}{" ..." if len(chart) > 6 else ""}')
    else:
        print('Donut chart: skipped (Pillow unavailable)')
    html_body = build_email_html(matrix, year, month, sent_on, has_chart=bool(chart_png))
    subject = f'Monthly Ops Report - {month_label(year, month)}'

    send_email(html_body, subject, {
        'from': gmail_from,
        'app_password': gmail_password,
        'to': email_to,
        'cc': email_cc,
    }, chart_png=chart_png)


if __name__ == '__main__':
    main()
