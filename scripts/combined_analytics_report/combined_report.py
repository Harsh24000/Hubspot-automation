#!/usr/bin/env python3
"""
Combined Analytics Report — NirogGyan.com + Brochure
Fetches Clarity + GA4 for both sites and sends one HTML email.
"""

import os
import json
import math
import time
import urllib.request
import urllib.parse
import base64
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── CONFIG (from environment / GitHub secrets) ────────────────────────────────
CLARITY_TOKEN = os.environ['CLARITY_TOKEN']

SITES = {
    "niroggyan": {
        "label":        "NirogGyan.com",
        "clarity_id":   "vbyvwvqc2v",
        "ga4_id":       "289155994",
        "base_url":     "https://www.niroggyan.com",
        "accent":       "#1D4ED8",
        "header_grad":  "linear-gradient(135deg,#0C2D5A 0%,#1A56A0 60%,#2563EB 100%)",
    },
    "brochure": {
        "label":        "Brochure",
        "clarity_id":   "vxjf93aaxo",
        "ga4_id":       "349814384",
        "base_url":     "",
        "accent":       "#7C3AED",
        "header_grad":  "linear-gradient(135deg,#2E1065 0%,#5B21B6 60%,#7C3AED 100%)",
    },
}

GA4_SA_JSON = json.loads(os.environ['GA4_SERVICE_ACCOUNT_JSON'])

EMAIL_FROM     = os.environ['EMAIL_FROM']
EMAIL_PASSWORD = os.environ['EMAIL_PASSWORD']
EMAIL_TO       = [e.strip() for e in os.environ['EMAIL_TO'].split(',') if e.strip()]
EMAIL_BCC      = [e.strip() for e in os.environ.get('EMAIL_BCC', '').split(',') if e.strip()]

NUM_DAYS = 3
# ─────────────────────────────────────────────────────────────────────────────


# ── CLARITY ──────────────────────────────────────────────────────────────────
def fetch_clarity(project_id):
    url = (
        f"https://www.clarity.ms/export-data/api/v1/project-live-insights"
        f"?projectId={project_id}&numOfDays={NUM_DAYS}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {CLARITY_TOKEN}"})
    data = json.loads(urllib.request.urlopen(req).read())
    result = {}
    for item in data:
        result[item["metricName"]] = item.get("information", [])
    return result


# ── GA4 ──────────────────────────────────────────────────────────────────────
def get_ga4_token(sa):
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        raise SystemExit("Run: pip install cryptography")

    def b64url(d):
        if isinstance(d, str): d = d.encode()
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    now = int(time.time())
    header  = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}))
    payload = b64url(json.dumps({
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/analytics.readonly",
        "aud": "https://oauth2.googleapis.com/token",
        "exp": now + 3600, "iat": now,
    }))
    msg = f"{header}.{payload}".encode()
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    jwt = f"{header}.{payload}.{b64url(sig)}"

    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt,
    }).encode()
    resp = json.loads(urllib.request.urlopen(
        urllib.request.Request("https://oauth2.googleapis.com/token", data=body)
    ).read())
    return resp["access_token"]


def ga4_query(token, property_id, body):
    req = urllib.request.Request(
        f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except Exception as e:
        print(f"GA4 query error ({property_id}): {e}")
        return {}


def fetch_ga4(token, property_id):
    date_range = [{"startDate": f"{NUM_DAYS}daysAgo", "endDate": "today"}]

    new_ret = ga4_query(token, property_id, {
        "dateRanges": date_range,
        "dimensions": [{"name": "newVsReturning"}],
        "metrics": [{"name": "sessions"}, {"name": "totalUsers"}],
    })
    region = ga4_query(token, property_id, {
        "dateRanges": date_range,
        "dimensions": [{"name": "region"}, {"name": "city"}],
        "metrics": [{"name": "sessions"}],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": 10,
    })
    overview = ga4_query(token, property_id, {
        "dateRanges": date_range,
        "metrics": [{"name": "totalUsers"}],
    })
    return {"new_vs_returning": new_ret, "region": region, "overview": overview}


# ── HTML HELPERS ─────────────────────────────────────────────────────────────
def fmt_seconds(s):
    s = int(float(s))
    if s < 60: return f"{s}s"
    return f"{s // 60}m {s % 60}s"


def _kpi_card(value, label, sublabel, accent, bg):
    return f"""<td style="padding:5px;">
  <table cellpadding="0" cellspacing="0" width="100%"
         style="background:{bg};border-radius:12px;border-top:3px solid {accent};">
    <tr><td style="padding:16px 14px 14px;">
      <p style="margin:0;font-size:24px;font-weight:800;color:#111827;line-height:1.1;">{value}</p>
      <p style="margin:5px 0 2px;font-size:10px;font-weight:800;text-transform:uppercase;
                letter-spacing:.8px;color:{accent};">{label}</p>
      <p style="margin:0;font-size:10px;color:#9CA3AF;">{sublabel}</p>
    </td></tr>
  </table>
</td>"""


def _donut(segments, size=130):
    r, cx, cy = 46, size // 2, size // 2
    circ = 2 * math.pi * r
    total = sum(v for v, _ in segments) or 1
    parts = [f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#E9EEF4" stroke-width="14"/>']
    angle = -90.0
    for val, color in segments:
        if val <= 0:
            continue
        dash = (val / total) * circ
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
            f'stroke-width="14" stroke-dasharray="{dash:.2f} {circ:.2f}" '
            f'transform="rotate({angle:.2f} {cx} {cy})"/>'
        )
        angle += (val / total) * 360
    return (
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        f'xmlns="http://www.w3.org/2000/svg" style="display:block;margin:0 auto;">{"".join(parts)}</svg>'
    )


def _legend_item(color, label, value, total):
    p = f"{value / total * 100:.0f}%" if total > 0 else "—"
    return (
        f'<tr><td style="padding:3px 0;">'
        f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;'
        f'background:{color};margin-right:6px;vertical-align:middle;"></span>'
        f'<span style="font-size:12px;color:#4B5563;vertical-align:middle;">{label}</span></td>'
        f'<td style="padding:3px 0 3px 12px;font-size:12px;font-weight:700;color:#111827;">'
        f'{value} <span style="color:#9CA3AF;font-weight:400;">({p})</span></td></tr>'
    )


def _bar_row(label, value, max_val, color, max_px=165):
    bar_w = max(3, int((value / max_val) * max_px)) if max_val > 0 else 3
    empty_w = max_px - bar_w
    label_disp = (str(label)[:34] + "…") if len(str(label)) > 34 else str(label)
    r_css = "border-radius:4px;" if empty_w == 0 else "border-radius:4px 0 0 4px;"
    empty = (f'<td style="width:{empty_w}px;height:7px;background:#F1F5F9;'
             f'border-radius:0 4px 4px 0;"></td>') if empty_w > 0 else ""
    return (
        f'<tr><td style="padding:6px 10px 6px 0;font-size:12px;color:#4B5563;width:130px;">'
        f'{label_disp}</td>'
        f'<td style="padding:6px 3px;"><table cellpadding="0" cellspacing="0"><tr>'
        f'<td style="width:{bar_w}px;height:7px;background:{color};{r_css}"></td>'
        f'{empty}</tr></table></td>'
        f'<td style="padding:6px 0 6px 8px;font-size:12px;font-weight:700;'
        f'color:#111827;white-space:nowrap;">{value}</td></tr>'
    )


def _eng_row(label, pct_str, color, row_bg):
    try:
        v = float(pct_str.rstrip("%"))
    except Exception:
        v = 0
    bar_w = max(2, int(v / 100 * 140))
    empty_w = 140 - bar_w
    r_css = "border-radius:4px;" if empty_w == 0 else "border-radius:4px 0 0 4px;"
    empty = (f'<td style="width:{empty_w}px;height:7px;background:#F1F5F9;'
             f'border-radius:0 4px 4px 0;"></td>') if empty_w > 0 else ""
    if v == 0:   status, sc = "None",   "#059669"
    elif v < 5:  status, sc = "Low",    "#D97706"
    elif v < 15: status, sc = "Medium", "#EA580C"
    else:        status, sc = "High",   "#DC2626"
    badge = (f'<span style="font-size:10px;font-weight:700;color:{sc};'
             f'background:{sc}1A;padding:2px 7px;border-radius:8px;">{status}</span>')
    return (
        f'<tr style="background:{row_bg};">'
        f'<td style="padding:10px 12px;font-size:12px;color:#374151;width:160px;">{label}</td>'
        f'<td style="padding:10px 4px;"><table cellpadding="0" cellspacing="0"><tr>'
        f'<td style="width:{bar_w}px;height:7px;background:{color};{r_css}"></td>'
        f'{empty}</tr></table></td>'
        f'<td style="padding:10px 8px;font-size:12px;font-weight:700;color:#111827;">{pct_str}</td>'
        f'<td style="padding:10px 12px;">{badge}</td></tr>'
    )


def _sh(title, color="#1D4ED8"):
    return (
        f'<p style="margin:0 0 14px;font-size:10px;font-weight:800;letter-spacing:1.2px;'
        f'text-transform:uppercase;color:{color};border-left:3px solid {color};'
        f'padding-left:9px;">{title}</p>'
    )


# ── SITE SECTION BUILDER ──────────────────────────────────────────────────────
def build_site_section(site_cfg, clarity, ga4_data):
    accent       = site_cfg["accent"]
    header_grad  = site_cfg["header_grad"]
    site_label   = site_cfg["label"]
    base_url     = site_cfg["base_url"]

    # ── Parse Clarity ──
    traffic  = (clarity.get("Traffic")         or [{}])[0]
    engage   = (clarity.get("EngagementTime")  or [{}])[0]
    scroll_d = (clarity.get("ScrollDepth")     or [{}])[0]
    dead     = (clarity.get("DeadClickCount")  or [{}])[0]
    rage     = (clarity.get("RageClickCount")  or [{}])[0]
    qback    = (clarity.get("QuickbackClick")  or [{}])[0]
    exc_sc   = (clarity.get("ExcessiveScroll") or [{}])[0]

    total_raw     = int(traffic.get("totalSessionCount", 0))
    bot_ses       = int(traffic.get("totalBotSessionCount", 0))
    human_ses     = total_raw - bot_ses
    pages_per_ses = float(traffic.get("pagesPerSessionPercentage", 0))
    avg_dur       = engage.get("totalTime", "0")
    avg_active    = engage.get("activeTime", "0")
    avg_scroll    = float(scroll_d.get("averageScrollDepth", 0))

    devices   = clarity.get("Device",       [])
    browsers  = clarity.get("Browser",      [])
    countries = clarity.get("Country",      [])
    referrers = clarity.get("ReferrerUrl",  [])
    pages     = clarity.get("PopularPages", [])

    def hpct(metric):
        sub = int(metric.get("subTotal", 0))
        return f"{sub / human_ses * 100:.1f}%" if human_ses > 0 else "0.0%"

    # ── Parse GA4 ──
    ov = ga4_data["overview"].get("rows", [])
    ga4_users = ov[0]["metricValues"][0]["value"] if ov else "—"

    nvr_rows = ga4_data["new_vs_returning"].get("rows", [])
    new_ses = ret_ses = new_usr = ret_usr = 0
    for row in nvr_rows:
        lbl = row["dimensionValues"][0]["value"]
        s   = int(row["metricValues"][0]["value"])
        u   = int(row["metricValues"][1]["value"])
        if lbl == "new":         new_ses, new_usr = s, u
        elif lbl == "returning": ret_ses, ret_usr = s, u

    region_rows = ga4_data["region"].get("rows", [])

    # ── Derived ──
    pc_ses  = next((int(d["sessionsCount"]) for d in devices if d["name"] == "PC"),     0)
    mob_ses = next((int(d["sessionsCount"]) for d in devices if d["name"] == "Mobile"), 0)

    clean_pages = []
    for p in pages[:7]:
        url = p.get("url", "")
        if base_url:
            url = url.replace(base_url, "").replace(base_url.replace("www.", ""), "")
        url = url.replace("http://localhost:3000", "[local]")
        clean_pages.append((url or "/", int(p.get("visitsCount", 0))))

    clean_refs = []
    for r in referrers[:6]:
        name = (r.get("name") or "Direct").replace("https://","").replace("http://","").rstrip("/")
        clean_refs.append((name, int(r.get("sessionsCount", 0))))

    max_page    = max((v for _, v in clean_pages), default=1)
    max_ref     = max((v for _, v in clean_refs),  default=1)
    max_country = max((int(c.get("sessionsCount",0)) for c in countries), default=1)

    # ── KPI cards ──
    kpi_row = "".join([
        _kpi_card(human_ses,            "Sessions",     f"{bot_ses} bots filtered", accent,    "#EFF6FF"),
        _kpi_card(ga4_users,            "Unique Users", "Google Analytics 4",       "#7C3AED", "#F5F3FF"),
        _kpi_card(fmt_seconds(avg_dur), "Avg Duration", "per session",              "#059669", "#ECFDF5"),
        _kpi_card(f"{avg_scroll:.1f}%", "Scroll Depth", "average",                 "#D97706", "#FFFBEB"),
    ])

    # ── Donut charts ──
    dev_total = pc_ses + mob_ses
    dev_donut  = _donut([(pc_ses, accent), (mob_ses, "#10B981")])
    dev_legend = "<table cellpadding='0' cellspacing='0' style='margin:10px auto 0;'>" + \
        _legend_item(accent,    "Desktop", pc_ses,  dev_total) + \
        _legend_item("#10B981", "Mobile",  mob_ses, dev_total) + "</table>"

    nvr_total  = new_ses + ret_ses
    nvr_donut  = _donut([(new_ses, accent), (ret_ses, "#8B5CF6")])
    nvr_legend = "<table cellpadding='0' cellspacing='0' style='margin:10px auto 0;'>" + \
        _legend_item(accent,    "New",       new_ses, nvr_total) + \
        _legend_item("#8B5CF6", "Returning", ret_ses, nvr_total) + "</table>"

    # ── Engagement rows ──
    eng_html = (
        _eng_row("Dead Clicks",      hpct(dead),   "#3B82F6", "#F9FAFB") +
        _eng_row("Rage Clicks",      hpct(rage),   "#EF4444", "#FFFFFF") +
        _eng_row("Quick Back-Click", hpct(qback),  "#F59E0B", "#F9FAFB") +
        _eng_row("Excessive Scroll", hpct(exc_sc), "#8B5CF6", "#FFFFFF")
    )

    # ── Bar charts ──
    pages_html   = "".join(_bar_row(u, v, max_page,    accent)    for u, v in clean_pages)
    refs_html    = "".join(_bar_row(n, v, max_ref,     "#7C3AED") for n, v in clean_refs)
    country_html = "".join(_bar_row(c.get("name","—"), int(c.get("sessionsCount",0)),
                                    max_country, "#059669") for c in countries[:7])

    # ── Browser pills ──
    browser_pills = "".join(
        f'<span style="display:inline-block;background:#F1F5F9;border-radius:6px;'
        f'padding:5px 10px;margin:3px;font-size:11px;color:#374151;">'
        f'<b>{b["name"]}</b> &nbsp;{b["sessionsCount"]}</span>'
        for b in browsers[:6]
    )

    # ── Regions table ──
    region_html = "".join(
        f'<tr style="background:{"#F9FAFB" if i % 2 == 0 else "#FFF"};">'
        f'<td style="padding:7px 12px;font-size:12px;color:#4B5563;">'
        f'{row["dimensionValues"][0]["value"]} / {row["dimensionValues"][1]["value"]}</td>'
        f'<td style="padding:7px 12px;font-size:12px;font-weight:700;color:#111827;text-align:right;">'
        f'{row["metricValues"][0]["value"]}</td></tr>'
        for i, row in enumerate(region_rows[:8])
    )

    return f"""
  <!-- ══ {site_label.upper()} HEADER ══ -->
  <tr><td style="background:{header_grad};padding:22px 32px 20px;border-radius:14px 14px 0 0;margin-top:0;">
    <p style="margin:0 0 2px;font-size:10px;font-weight:700;letter-spacing:2px;
              color:#BFDBFE;text-transform:uppercase;">Analytics Report</p>
    <p style="margin:0;font-size:20px;font-weight:800;color:#FFFFFF;line-height:1.2;">{site_label}</p>
  </td></tr>

  <!-- KPI STRIP -->
  <tr><td style="background:#F0F4FA;padding:14px 10px 10px;">
    <table cellpadding="0" cellspacing="0" width="100%"><tr>{kpi_row}</tr></table>
  </td></tr>

  <!-- DONUT ROW -->
  <tr><td style="background:#F0F4FA;padding:4px 10px 14px;">
    <table cellpadding="0" cellspacing="0" width="100%"><tr>
      <td width="50%" style="padding:0 5px 0 0;">
        <table cellpadding="0" cellspacing="0" width="100%"
               style="background:#FFF;border-radius:12px;">
          <tr><td style="padding:20px 16px 18px;">
            {_sh("Device Split", accent)}
            {dev_donut}
            {dev_legend}
          </td></tr>
        </table>
      </td>
      <td width="50%" style="padding:0 0 0 5px;">
        <table cellpadding="0" cellspacing="0" width="100%"
               style="background:#FFF;border-radius:12px;">
          <tr><td style="padding:20px 16px 18px;">
            {_sh("New vs Returning", "#7C3AED")}
            {nvr_donut}
            {nvr_legend}
          </td></tr>
        </table>
      </td>
    </tr></table>
  </td></tr>

  <!-- MAIN BODY -->
  <tr><td style="background:#FFFFFF;padding:28px 26px 32px;border-radius:0 0 14px 14px;">

    <div style="margin-bottom:30px;">
      {_sh("Engagement Quality", accent)}
      <table cellpadding="0" cellspacing="0" width="100%"
             style="border:1px solid #E5E7EB;border-radius:10px;overflow:hidden;">
        {eng_html}
      </table>
    </div>

    <div style="margin-bottom:30px;background:#F8FAFF;border-radius:10px;padding:16px 18px;">
      <table cellpadding="0" cellspacing="0" width="100%"><tr>
        <td style="font-size:28px;font-weight:800;color:{accent};">{pages_per_ses:.2f}</td>
        <td style="padding-left:14px;">
          <p style="margin:0;font-size:12px;font-weight:700;color:#374151;">Pages per Session</p>
          <p style="margin:3px 0 0;font-size:11px;color:#9CA3AF;">avg across human sessions</p>
        </td>
        <td style="text-align:right;">
          <p style="margin:0;font-size:28px;font-weight:800;color:#059669;">{fmt_seconds(avg_active)}</p>
          <p style="margin:3px 0 0;font-size:11px;color:#9CA3AF;text-align:right;">avg active time</p>
        </td>
      </tr></table>
    </div>

    <div style="margin-bottom:30px;">
      {_sh("Top Pages", "#059669")}
      <table cellpadding="0" cellspacing="0" width="100%">{pages_html}</table>
    </div>

    <div style="margin-bottom:30px;">
      {_sh("Traffic Sources", "#7C3AED")}
      <table cellpadding="0" cellspacing="0" width="100%">{refs_html}</table>
    </div>

    <div style="margin-bottom:30px;">
      {_sh("Top Countries", "#D97706")}
      <table cellpadding="0" cellspacing="0" width="100%">{country_html}</table>
    </div>

    <div style="margin-bottom:30px;">
      {_sh("Browsers", "#64748B")}
      <div>{browser_pills}</div>
    </div>

    <div>
      {_sh("Top Regions & Cities (GA4)", "#DC2626")}
      <table cellpadding="0" cellspacing="0" width="100%"
             style="border:1px solid #E5E7EB;border-radius:10px;overflow:hidden;">
        {region_html}
      </table>
    </div>

  </td></tr>"""


# ── FULL EMAIL BUILDER ────────────────────────────────────────────────────────
def build_email(niroggyan_clarity, niroggyan_ga4, brochure_clarity, brochure_ga4):
    end   = datetime.now()
    start = end - timedelta(days=NUM_DAYS)
    date_label = f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"

    ng_section = build_site_section(SITES["niroggyan"], niroggyan_clarity, niroggyan_ga4)
    br_section = build_site_section(SITES["brochure"],  brochure_clarity,  brochure_ga4)

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#E8EDF5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#1F2937;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#E8EDF5;padding:28px 12px;">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

  <!-- TOP HEADER -->
  <tr><td style="background:linear-gradient(135deg,#0A0A23 0%,#1e1b4b 60%,#312e81 100%);
                 padding:28px 32px 22px;border-radius:14px 14px 0 0;">
    <p style="margin:0 0 6px;font-size:24px;font-weight:800;color:#FFFFFF;line-height:1.2;">
      Website &amp; Brochure Analytics</p>
    <p style="margin:0;font-size:13px;color:#C7D2FE;">
      {date_label} &nbsp;&#183;&nbsp; {NUM_DAYS}-day snapshot &nbsp;&#183;&nbsp; Clarity + GA4</p>
  </td></tr>

  <!-- SPACER -->
  <tr><td style="background:#E8EDF5;padding:10px 0;"></td></tr>

  <!-- NIROGGYAN.COM SECTION -->
  {ng_section}

  <!-- SPACER BETWEEN SITES -->
  <tr><td style="background:#E8EDF5;padding:16px 0;"></td></tr>

  <!-- BROCHURE SECTION -->
  {br_section}

  <!-- FOOTER -->
  <tr><td style="padding:18px 0 4px;">
    <p style="margin:0;font-size:11px;color:#94A3B8;text-align:center;">
      Auto-generated every {NUM_DAYS} days &nbsp;&#183;&nbsp; MS Clarity + Google Analytics 4<br>
      Niro Health / NirogGyan
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ── EMAIL SENDER ─────────────────────────────────────────────────────────────
def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = ", ".join(EMAIL_TO)
    if EMAIL_BCC:
        msg["Bcc"] = ", ".join(EMAIL_BCC)
    msg.attach(MIMEText(html_body, "html"))

    all_recipients = EMAIL_TO + EMAIL_BCC
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
        smtp.sendmail(EMAIL_FROM, all_recipients, msg.as_string())

    print(f"Email sent to {', '.join(EMAIL_TO)}")


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("Fetching Clarity — NirogGyan.com...")
    ng_clarity = fetch_clarity(SITES["niroggyan"]["clarity_id"])

    print("Fetching Clarity — Brochure...")
    br_clarity = fetch_clarity(SITES["brochure"]["clarity_id"])

    print("Getting GA4 token...")
    token = get_ga4_token(GA4_SA_JSON)

    print("Fetching GA4 — NirogGyan.com...")
    ng_ga4 = fetch_ga4(token, SITES["niroggyan"]["ga4_id"])

    print("Fetching GA4 — Brochure...")
    br_ga4 = fetch_ga4(token, SITES["brochure"]["ga4_id"])

    print("Building email...")
    end   = datetime.now()
    start = end - timedelta(days=NUM_DAYS)
    subject = f"NirogGyan + Brochure Report · {start.strftime('%b %d')}–{end.strftime('%b %d')}"
    html = build_email(ng_clarity, ng_ga4, br_clarity, br_ga4)

    print("Sending email...")
    send_email(subject, html)
    print("Done.")


if __name__ == "__main__":
    main()
