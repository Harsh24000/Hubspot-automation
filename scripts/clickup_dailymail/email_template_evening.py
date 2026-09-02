from datetime import date
from typing import Dict
import hashlib


MODULE_PALETTES = [
    {"bg": "#EDE9FE", "text": "#5B21B6"},
    {"bg": "#CFFAFE", "text": "#0E7490"},
    {"bg": "#FEF3C7", "text": "#92400E"},
    {"bg": "#D1FAE5", "text": "#065F46"},
    {"bg": "#FCE7F3", "text": "#9D174D"},
    {"bg": "#DBEAFE", "text": "#1E40AF"},
    {"bg": "#FFEDD5", "text": "#9A3412"},
    {"bg": "#E0E7FF", "text": "#3730A3"},
]


def _module_color(module_name: str) -> dict:
    if not module_name or module_name == "N/A":
        return {"bg": "#F1F5F9", "text": "#64748B"}
    idx = int(hashlib.md5(module_name.encode()).hexdigest(), 16) % len(MODULE_PALETTES)
    return MODULE_PALETTES[idx]


def _initials(name: str) -> str:
    parts = name.strip().split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return name[:2].upper() if name else "??"


def _avatar_html(name: str, avatar_url: str = None) -> str:
    initials = _initials(name)
    bg_colors = ["#6366F1", "#8B5CF6", "#EC4899", "#14B8A6", "#F59E0B", "#10B981"]
    color = bg_colors[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(bg_colors)]
    if avatar_url:
        return (
            f'<img src="{avatar_url}" width="44" height="44" '
            f'style="border-radius:50%;object-fit:cover;border:2px solid #E2E8F0;" />'
        )
    return (
        f'<div style="width:44px;height:44px;border-radius:50%;background:{color};'
        f'display:flex;align-items:center;justify-content:center;'
        f'font-family:Arial,sans-serif;font-size:15px;font-weight:700;'
        f'color:#ffffff;text-align:center;line-height:44px;flex-shrink:0;">'
        f'{initials}</div>'
    )


def _client_badge_html(task: dict) -> str:
    """Client Name pill. Returns '' when the task has no client, so tasks
    without one look exactly as they did before."""
    client = (task.get("client_name") or "").strip()
    if not client:
        return ""
    return (
        f'<span style="font-size:11px;font-weight:600;font-family:Arial,sans-serif;'
        f'background:#EEF2FF;color:#3730A3;padding:3px 10px;border-radius:12px;'
        f'white-space:nowrap;border:1px solid #C7D2FE;">'
        f'&#128100;&nbsp;{client}</span>'
    )


def _planned_row_html(task: dict) -> str:
    module = task.get("product_module", "N/A") or "N/A"
    col = _module_color(module)
    task_url = task.get("task_url", "#")
    task_num = task.get("task_number", "")
    task_name = task.get("task_name", "Untitled")
    comment = task.get("comment", "")
    logged = task.get("logged", "0h")

    task_id_badge = (
        f'<a href="{task_url}" style="text-decoration:none;">'
        f'<span style="font-family:\'Courier New\',monospace;font-size:11px;'
        f'background:#F1F5F9;color:#64748B;padding:3px 8px;border-radius:4px;'
        f'border:1px solid #E2E8F0;white-space:nowrap;">{task_num}</span></a>'
    ) if task_url != "#" else (
        f'<span style="font-family:\'Courier New\',monospace;font-size:11px;'
        f'background:#F1F5F9;color:#64748B;padding:3px 8px;border-radius:4px;'
        f'border:1px solid #E2E8F0;white-space:nowrap;">{task_num}</span>'
    )

    module_badge = (
        f'<span style="font-size:11px;font-weight:600;font-family:Arial,sans-serif;'
        f'background:{col["bg"]};color:{col["text"]};padding:3px 10px;'
        f'border-radius:12px;white-space:nowrap;">{module}</span>'
    )

    # Empty string for tasks with no client, so those rows render unchanged.
    client_badge = _client_badge_html(task)
    if client_badge:
        client_badge += "&nbsp;"

    return f"""
    <tr>
      <td style="padding:0 0 10px 0;">
        <table cellpadding="0" cellspacing="0" border="0" width="100%"
               style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:10px;overflow:hidden;">
          <tr>
            <td style="padding:12px 16px;">
              <table cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td style="vertical-align:middle;">{task_id_badge}</td>
                  <td align="right" style="vertical-align:middle;">{client_badge}{module_badge}</td>
                </tr>
              </table>
              <p style="margin:8px 0 6px;font-family:Arial,sans-serif;font-size:14px;
                         font-weight:700;color:#1E293B;line-height:1.4;">{task_name}</p>
              <table cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="padding-right:16px;">
                    <span style="font-family:Arial,sans-serif;font-size:12px;color:#94A3B8;">
                      Estimated:&nbsp;<strong style="color:#6366F1;">{comment}</strong>
                    </span>
                  </td>
                  <td>
                    <span style="font-family:Arial,sans-serif;font-size:12px;color:#94A3B8;">
                      Logged:&nbsp;<strong style="color:#10B981;">{logged}</strong>
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>"""


def _unplanned_row_html(task: dict, have_snapshot: bool = True) -> str:
    module = task.get("product_module", "N/A") or "N/A"
    col = _module_color(module)
    task_url = task.get("task_url", "#")
    task_num = task.get("task_number", "")
    task_name = task.get("task_name", "Untitled")
    logged = task.get("logged", "—")

    task_id_badge = (
        f'<a href="{task_url}" style="text-decoration:none;">'
        f'<span style="font-family:\'Courier New\',monospace;font-size:11px;'
        f'background:#F1F5F9;color:#64748B;padding:3px 8px;border-radius:4px;'
        f'border:1px solid #E2E8F0;white-space:nowrap;">{task_num}</span></a>'
    ) if task_url != "#" else (
        f'<span style="font-family:\'Courier New\',monospace;font-size:11px;'
        f'background:#F1F5F9;color:#64748B;padding:3px 8px;border-radius:4px;'
        f'border:1px solid #E2E8F0;white-space:nowrap;">{task_num}</span>'
    )

    module_badge = (
        f'<span style="font-size:11px;font-weight:600;font-family:Arial,sans-serif;'
        f'background:{col["bg"]};color:{col["text"]};padding:3px 10px;'
        f'border-radius:12px;white-space:nowrap;">{module}</span>'
    )

    # Empty string for tasks with no client, so those rows render unchanged.
    client_badge = _client_badge_html(task)
    if client_badge:
        client_badge += "&nbsp;"

    return f"""
    <tr>
      <td style="padding:0 0 10px 0;">
        <table cellpadding="0" cellspacing="0" border="0" width="100%"
               style="background:#FFFFFF;border:1px solid #FED7AA;border-radius:10px;overflow:hidden;">
          <tr>
            <td style="padding:12px 16px;">
              <table cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td style="vertical-align:middle;">{task_id_badge}</td>
                  <td align="right" style="vertical-align:middle;">
                    <table cellpadding="0" cellspacing="0" border="0">
                      <tr>
                        <td style="padding-right:6px;">{client_badge}{module_badge}</td>
                        <td>
                          <span style="font-size:11px;font-weight:600;font-family:Arial,sans-serif;
                                        background:#FFF7ED;color:#EA580C;padding:3px 10px;
                                        border-radius:12px;border:1px solid #FED7AA;">{"&#9889; Unplanned" if have_snapshot else "&#9203; Logged"}</span>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
              <p style="margin:8px 0 4px;font-family:Arial,sans-serif;font-size:14px;
                         font-weight:700;color:#1E293B;line-height:1.4;">{task_name}</p>
              <span style="font-family:Arial,sans-serif;font-size:12px;color:#94A3B8;">
                Logged:&nbsp;<strong style="color:#10B981;">{logged}</strong>
              </span>
            </td>
          </tr>
        </table>
      </td>
    </tr>"""


def _section_header(label: str, count: int, color: str) -> str:
    return f"""
    <tr>
      <td style="padding:0 0 8px 2px;">
        <span style="font-family:Arial,sans-serif;font-size:11px;font-weight:700;
                      letter-spacing:1.5px;text-transform:uppercase;color:{color};">
          {label} &nbsp;·&nbsp; {count}
        </span>
      </td>
    </tr>"""


def _fmt_hours_from_ms(ms: int) -> str:
    hours = ms / 3600000.0
    if hours <= 0:
        return "0h"
    if hours < 1:
        return f"{round(hours * 60)}m"
    if hours == int(hours):
        return f"{int(hours)}h"
    return f"{hours:.1f}h"


def _person_section_html(name: str, data: dict, have_snapshot: bool = True) -> str:
    avatar = _avatar_html(name, data.get("avatar"))
    planned = data.get("planned", [])
    unplanned = data.get("unplanned", [])
    total = len(planned) + len(unplanned)
    task_word = "task" if total == 1 else "tasks"
    total_logged_ms = data.get("total_logged_ms", 0)

    planned_rows = ""
    if planned:
        planned_rows = _section_header("Planned", len(planned), "#6366F1")
        planned_rows += "".join(_planned_row_html(t) for t in planned)

    unplanned_rows = ""
    if unplanned:
        unplanned_rows = _section_header(
            "Unplanned" if have_snapshot else "Logged Today", len(unplanned), "#EA580C")
        unplanned_rows += "".join(_unplanned_row_html(t, have_snapshot) for t in unplanned)

    return f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="margin-bottom:20px;background:#FFFFFF;border:1px solid #E2E8F0;
                  border-radius:16px;overflow:hidden;">
      <tr>
        <td style="padding:18px 22px 14px 22px;border-bottom:1px solid #F1F5F9;">
          <table cellpadding="0" cellspacing="0" border="0" width="100%">
            <tr>
              <td style="vertical-align:middle;padding-right:14px;width:44px;">{avatar}</td>
              <td style="vertical-align:middle;">
                <p style="margin:0;font-family:Arial,sans-serif;font-size:16px;
                           font-weight:700;color:#1E293B;">{name}</p>
                <p style="margin:3px 0 0;font-family:Arial,sans-serif;font-size:12px;color:#94A3B8;">
                  {total} {task_word}{" &nbsp;·&nbsp; " + f'<span style="color:#6366F1;">{len(planned)} planned</span> &nbsp;·&nbsp; <span style="color:#EA580C;">{len(unplanned)} unplanned</span>' if have_snapshot else ""}
                </p>
              </td>
              <td style="vertical-align:middle;text-align:right;white-space:nowrap;">
                <span style="display:inline-block;padding:5px 12px;border-radius:12px;
                             background:#ECFDF5;color:#059669;font-size:13px;font-weight:700;
                             font-family:Arial,sans-serif;white-space:nowrap;">
                  &#9203; {_fmt_hours_from_ms(total_logged_ms)} logged
                </span>
              </td>
            </tr>
          </table>
        </td>
      </tr>
      <tr>
        <td style="padding:16px 18px 6px 18px;">
          <table cellpadding="0" cellspacing="0" border="0" width="100%">
            {planned_rows}
            {unplanned_rows}
          </table>
        </td>
      </tr>
    </table>"""


def generate_email_html(report: Dict[str, dict], report_date: date,
                        have_snapshot: bool = True) -> str:
    today_str = report_date.strftime("%A, %B %d %Y")
    day_label = report_date.strftime("%A").upper()
    total_people = len(report)
    total_planned = sum(len(d["planned"]) for d in report.values())
    total_unplanned = sum(len(d["unplanned"]) for d in report.values())
    total_logged_ms = sum(d.get("total_logged_ms", 0) for d in report.values())

    no_plan_banner = "" if have_snapshot else (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="margin-bottom:18px;"><tr><td style="background:#FFF7ED;'
        'border:1px solid #FED7AA;border-radius:12px;padding:12px 16px;">'
        '<p style="margin:0;font-family:Arial,sans-serif;font-size:13px;color:#9A3412;">'
        '<strong>No morning plan was recorded today.</strong> Showing time logged in '
        'ClickUp only &mdash; work is not split into planned vs unplanned.</p>'
        '</td></tr></table>')

    person_sections = "".join(
        _person_section_html(name, data, have_snapshot)
        for name, data in sorted(report.items())
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Evening Report — {today_str}</title>
</head>
<body style="margin:0;padding:0;background:#F1F5F9;">

  <table cellpadding="0" cellspacing="0" border="0" width="100%"
         style="background:#F1F5F9;min-height:100vh;">
    <tr>
      <td align="center" style="padding:32px 16px;">

        <table cellpadding="0" cellspacing="0" border="0" width="640"
               style="max-width:640px;width:100%;">

          <!-- HEADER -->
          <tr>
            <td style="border-radius:16px 16px 0 0;background:#FFFFFF;
                        border:1px solid #E2E8F0;border-bottom:none;padding:36px 36px 32px;">

              <table cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="background:#FFF7ED;border-radius:20px;padding:5px 14px;">
                    <span style="font-family:Arial,sans-serif;font-size:11px;font-weight:700;
                                  letter-spacing:2px;color:#EA580C;">{day_label} &nbsp;·&nbsp; EVENING</span>
                  </td>
                </tr>
              </table>

              <h1 style="margin:14px 0 4px;font-family:Arial,sans-serif;font-size:30px;
                          font-weight:800;color:#1E293B;letter-spacing:-0.5px;line-height:1.1;">
                End of Day Report
              </h1>
              <p style="margin:0 0 24px;font-family:Arial,sans-serif;font-size:14px;color:#94A3B8;">
                {today_str}
              </p>

              <table cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="padding-right:28px;">
                    <p style="margin:0;font-family:Arial,sans-serif;font-size:26px;font-weight:800;color:#6366F1;">{total_people}</p>
                    <p style="margin:2px 0 0;font-family:Arial,sans-serif;font-size:11px;font-weight:600;
                               letter-spacing:1px;color:#94A3B8;text-transform:uppercase;">Members</p>
                  </td>
                  <td style="width:1px;background:#E2E8F0;">&nbsp;</td>
                  <td style="padding-left:28px;padding-right:28px;">
                    <p style="margin:0;font-family:Arial,sans-serif;font-size:26px;font-weight:800;color:#6366F1;">{total_planned}</p>
                    <p style="margin:2px 0 0;font-family:Arial,sans-serif;font-size:11px;font-weight:600;
                               letter-spacing:1px;color:#94A3B8;text-transform:uppercase;">Planned</p>
                  </td>
                  <td style="width:1px;background:#E2E8F0;">&nbsp;</td>
                  <td style="padding-left:28px;">
                    <p style="margin:0;font-family:Arial,sans-serif;font-size:26px;font-weight:800;color:#EA580C;">{total_unplanned}</p>
                    <p style="margin:2px 0 0;font-family:Arial,sans-serif;font-size:11px;font-weight:600;
                               letter-spacing:1px;color:#94A3B8;text-transform:uppercase;">{"Unplanned" if have_snapshot else "Tasks"}</p>
                  </td>
                  <td style="width:1px;background:#E2E8F0;">&nbsp;</td>
                  <td style="padding-left:28px;">
                    <p style="margin:0;font-family:Arial,sans-serif;font-size:26px;font-weight:800;color:#059669;">{_fmt_hours_from_ms(total_logged_ms)}</p>
                    <p style="margin:2px 0 0;font-family:Arial,sans-serif;font-size:11px;font-weight:600;
                               letter-spacing:1px;color:#94A3B8;text-transform:uppercase;">Logged</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Accent bar -->
          <tr>
            <td style="height:3px;background:linear-gradient(90deg,#6366F1,#EA580C,#EC4899);
                        border-left:1px solid #E2E8F0;border-right:1px solid #E2E8F0;"></td>
          </tr>

          <!-- BODY -->
          <tr>
            <td style="background:#F8FAFC;border:1px solid #E2E8F0;border-top:none;
                        border-bottom:none;padding:24px 20px 8px;">
              {no_plan_banner}
              {person_sections}
            </td>
          </tr>

          <!-- FOOTER -->
          <tr>
            <td style="border-radius:0 0 16px 16px;background:#FFFFFF;
                        border:1px solid #E2E8F0;border-top:1px solid #F1F5F9;
                        padding:18px 24px 20px;">
              <table cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td>
                    <p style="margin:0;font-family:Arial,sans-serif;font-size:12px;color:#CBD5E1;">
                      Auto-generated from
                      <span style="color:#6366F1;font-weight:600;">ClickUp</span>
                      &nbsp;&middot;&nbsp; Time entries vs morning estimates
                    </p>
                  </td>
                  <td align="right">
                    <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:#CBD5E1;">{today_str}</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>

</body>
</html>"""
