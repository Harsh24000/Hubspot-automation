from datetime import date
from typing import Dict, List
import hashlib


# Palette of vivid badge colors for Product Modules (cycles via hash)
MODULE_PALETTES = [
    {"bg": "#7C3AED", "text": "#EDE9FE"},  # violet
    {"bg": "#0E7490", "text": "#CFFAFE"},  # cyan
    {"bg": "#B45309", "text": "#FEF3C7"},  # amber
    {"bg": "#047857", "text": "#D1FAE5"},  # emerald
    {"bg": "#BE185D", "text": "#FCE7F3"},  # pink
    {"bg": "#1D4ED8", "text": "#DBEAFE"},  # blue
    {"bg": "#C2410C", "text": "#FFEDD5"},  # orange
    {"bg": "#4F46E5", "text": "#E0E7FF"},  # indigo
]


def _module_color(module_name: str) -> dict:
    if not module_name or module_name == "N/A":
        return {"bg": "#374151", "text": "#D1D5DB"}
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
            f'style="border-radius:50%;object-fit:cover;border:2px solid #2D3748;" />'
        )
    return (
        f'<div style="width:44px;height:44px;border-radius:50%;background:{color};'
        f'display:flex;align-items:center;justify-content:center;'
        f'font-family:Arial,sans-serif;font-size:15px;font-weight:700;'
        f'color:#ffffff;text-align:center;line-height:44px;'
        f'border:2px solid rgba(255,255,255,0.15);flex-shrink:0;">'
        f'{initials}</div>'
    )


def _task_row_html(task: dict) -> str:
    module = task.get("product_module", "N/A") or "N/A"
    col = _module_color(module)
    task_num = task.get("task_number", "")
    task_name = task.get("task_name", "Untitled")
    comment = task.get("comment", "")
    task_url = task.get("task_url", "#")

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

    return f"""
    <tr>
      <td style="padding:0 0 12px 0;">
        <table cellpadding="0" cellspacing="0" border="0" width="100%"
               style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:10px;overflow:hidden;">
          <tr>
            <td style="padding:14px 18px;">
              <!-- Top row: task id + module badge -->
              <table cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td style="vertical-align:middle;">{task_id_badge}</td>
                  <td align="right" style="vertical-align:middle;">{module_badge}</td>
                </tr>
              </table>
              <!-- Task name -->
              <p style="margin:8px 0 4px 0;font-family:Arial,sans-serif;font-size:14px;
                         font-weight:700;color:#0F172A;line-height:1.4;">{task_name}</p>
              <!-- Comment -->
              <p style="margin:0;font-family:Arial,sans-serif;font-size:13px;
                         color:#64748B;line-height:1.5;">
                <span style="color:#4F46E5;font-weight:600;">&#9673;</span>&nbsp;{comment}
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>"""


def _person_section_html(name: str, tasks: List[dict]) -> str:
    avatar = _avatar_html(name, tasks[0].get("avatar") if tasks else None)
    task_rows = "".join(_task_row_html(t) for t in tasks)
    task_word = "task" if len(tasks) == 1 else "tasks"

    return f"""
    <!-- Person card -->
    <table cellpadding="0" cellspacing="0" border="0" width="100%"
           style="margin-bottom:28px;background:#FFFFFF;
                  border:1px solid #E2E8F0;border-radius:16px;overflow:hidden;
                  box-shadow:0 1px 3px rgba(15,23,42,0.06);">
      <!-- Person header -->
      <tr>
        <td style="padding:20px 24px 16px 24px;border-bottom:1px solid #E2E8F0;">
          <table cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="vertical-align:middle;padding-right:14px;">{avatar}</td>
              <td style="vertical-align:middle;">
                <p style="margin:0;font-family:Arial,sans-serif;font-size:17px;
                           font-weight:700;color:#0F172A;">{name}</p>
                <p style="margin:3px 0 0 0;font-family:Arial,sans-serif;font-size:12px;
                           color:#64748B;">{len(tasks)} {task_word} scheduled today</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
      <!-- Tasks -->
      <tr>
        <td style="padding:18px 20px 6px 20px;background:#F8FAFC;">
          <table cellpadding="0" cellspacing="0" border="0" width="100%">
            {task_rows}
          </table>
        </td>
      </tr>
    </table>"""


def generate_email_html(person_tasks: Dict[str, List[dict]], report_date: date) -> str:
    today_str = report_date.strftime("%A, %B %d %Y")
    day_label = report_date.strftime("%A").upper()
    total_people = len(person_tasks)
    total_tasks = sum(len(t) for t in person_tasks.values())

    person_sections = "".join(
        _person_section_html(name, tasks)
        for name, tasks in sorted(person_tasks.items())
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Daily Updates — {today_str}</title>
</head>
<body style="margin:0;padding:0;background:#F1F5F9;">

  <!-- Outer wrapper -->
  <table cellpadding="0" cellspacing="0" border="0" width="100%"
         style="background:#F1F5F9;min-height:100vh;">
    <tr>
      <td align="center" style="padding:32px 16px;">

        <!-- Inner card (max-width 640px) -->
        <table cellpadding="0" cellspacing="0" border="0" width="640"
               style="max-width:640px;width:100%;">

          <!-- ── HEADER ─────────────────────────────────────────────── -->
          <tr>
            <td style="border-radius:20px 20px 0 0;overflow:hidden;
                        background:linear-gradient(135deg,#0f2744 0%,#1a56a0 100%);
                        padding:40px 36px 36px;">

              <!-- Day pill -->
              <table cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="background:rgba(255,255,255,0.15);border:1px solid rgba(255,255,255,0.3);
                               border-radius:20px;padding:5px 14px;">
                    <span style="font-family:Arial,sans-serif;font-size:11px;font-weight:700;
                                  letter-spacing:2px;color:#DBEAFE;">{day_label}</span>
                  </td>
                </tr>
              </table>

              <!-- Title -->
              <h1 style="margin:16px 0 6px;font-family:Arial,sans-serif;font-size:32px;
                          font-weight:800;color:#FFFFFF;letter-spacing:-0.5px;line-height:1.1;">
                Daily Updates
              </h1>
              <p style="margin:0 0 24px;font-family:Arial,sans-serif;font-size:15px;
                          color:rgba(255,255,255,0.7);">{today_str}</p>

              <!-- Stats row -->
              <table cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="padding-right:24px;">
                    <p style="margin:0;font-family:Arial,sans-serif;font-size:26px;
                               font-weight:800;color:#FFFFFF;">{total_people}</p>
                    <p style="margin:2px 0 0;font-family:Arial,sans-serif;font-size:11px;
                               font-weight:600;letter-spacing:1px;color:rgba(255,255,255,0.6);text-transform:uppercase;">
                      Team Members
                    </p>
                  </td>
                  <td style="width:1px;background:rgba(255,255,255,0.2);">&nbsp;</td>
                  <td style="padding-left:24px;">
                    <p style="margin:0;font-family:Arial,sans-serif;font-size:26px;
                               font-weight:800;color:#FFFFFF;">{total_tasks}</p>
                    <p style="margin:2px 0 0;font-family:Arial,sans-serif;font-size:11px;
                               font-weight:600;letter-spacing:1px;color:rgba(255,255,255,0.6);text-transform:uppercase;">
                      Tasks Scheduled
                    </p>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Gradient accent bar -->
          <tr>
            <td style="height:4px;background:linear-gradient(90deg,#6366F1,#A855F7,#EC4899);">
            </td>
          </tr>

          <!-- ── BODY ──────────────────────────────────────────────── -->
          <tr>
            <td style="background:#FFFFFF;border-left:1px solid #E2E8F0;border-right:1px solid #E2E8F0;
                        padding:28px 24px 12px;">
              {person_sections}
            </td>
          </tr>

          <!-- ── FOOTER ────────────────────────────────────────────── -->
          <tr>
            <td style="border-radius:0 0 20px 20px;background:#FFFFFF;
                        border:1px solid #E2E8F0;border-top:1px solid #E2E8F0;
                        padding:20px 28px 24px;">
              <table cellpadding="0" cellspacing="0" border="0" width="100%">
                <tr>
                  <td>
                    <p style="margin:0;font-family:Arial,sans-serif;font-size:12px;color:#94A3B8;">
                      Auto-generated from
                      <span style="color:#4F46E5;font-weight:600;">ClickUp</span>
                      &nbsp;&middot;&nbsp;
                      <span style="color:#94A3B8;">Comments matching <code
                        style="background:#F1F5F9;color:#4F46E5;padding:1px 5px;border-radius:3px;
                               font-size:11px;">est Xhr</code></span>
                    </p>
                  </td>
                  <td align="right">
                    <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:#CBD5E1;">
                      {today_str}
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

        </table>
        <!-- /inner card -->

      </td>
    </tr>
  </table>

</body>
</html>"""
