#!/usr/bin/env python3
"""Key-takeaway sentence for the top summary block of the ClickUp reports.

Uses Groq when GROQ_API_KEY is present (same secret the other reports in this
repo already use), and falls back to a computed sentence otherwise. The
fallback is not a degraded error path — it is a correct, if plainer, summary,
so a missing key or a Groq outage never costs you the block or the email.

The model is only ever asked to PHRASE figures that are handed to it. It is
told not to invent numbers, and anything it returns is length-capped and
HTML-escaped before it reaches the email.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Dict, List

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TIMEOUT = 20
MAX_TAKEAWAY_CHARS = 400


# ── Formatting helpers ───────────────────────────────────────────────────────

def fmt_hours(hours: float) -> str:
    """4.0 -> '4h'  ·  0.33 -> '20m'  ·  7.5 -> '7.5h'"""
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        return "0h"
    if hours <= 0:
        return "0h"
    if hours < 1:
        return f"{round(hours * 60)}m"
    if abs(hours - int(hours)) < 0.05:
        return f"{int(hours)}h"
    return f"{hours:.1f}h"


def _top_themes(labels: List[str], limit: int = 4) -> List[str]:
    """Most common non-empty labels, most frequent first."""
    counts: Dict[str, int] = {}
    for label in labels:
        label = (label or "").strip()
        if not label or label.upper() == "N/A":
            continue
        counts[label] = counts.get(label, 0) + 1
    return [name for name, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:limit]


def _join(items: List[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


# ── Fact gathering ───────────────────────────────────────────────────────────

def morning_facts(person_tasks: Dict[str, List[dict]],
                  person_totals: Dict[str, float],
                  person_logged_hours: Dict[str, float]) -> dict:
    people = []
    modules: List[str] = []
    for name, tasks in sorted(person_tasks.items()):
        modules += [t.get("product_module", "") for t in tasks]
        people.append({
            "name": name,
            "tasks": len(tasks),
            "estimated": person_totals.get(name, 0.0),
            "logged": person_logged_hours.get(name, 0.0),
            "task_names": [t.get("task_name", "Untitled") for t in tasks],
        })
    return {
        "kind": "morning",
        "people": people,
        "total_people": len(people),
        "total_tasks": sum(p["tasks"] for p in people),
        "total_estimated": sum(person_totals.values()),
        "total_logged": sum(person_logged_hours.values()),
        "themes": _top_themes(modules),
    }


def evening_facts(report: Dict[str, dict], have_snapshot: bool = True) -> dict:
    people = []
    modules: List[str] = []
    repeats: List[str] = []
    for name, data in sorted(report.items()):
        planned = data.get("planned", []) or []
        unplanned = data.get("unplanned", []) or []
        modules += [t.get("product_module", "") for t in planned + unplanned]
        for task in unplanned:
            try:
                if int(task.get("week_occurrences", 1) or 1) >= 2:
                    repeats.append(task.get("task_name", "Untitled"))
            except (TypeError, ValueError):
                pass
        people.append({
            "name": name,
            "tasks": len(planned) + len(unplanned),
            "planned": len(planned),
            "unplanned": len(unplanned),
            "logged": data.get("total_logged_ms", 0) / 3600000.0,
            "task_names": [t.get("task_name", "Untitled") for t in planned + unplanned],
        })
    return {
        "kind": "evening",
        "have_snapshot": have_snapshot,
        "people": people,
        "total_people": len(people),
        "total_tasks": sum(p["tasks"] for p in people),
        "total_planned": sum(p["planned"] for p in people),
        "total_unplanned": sum(p["unplanned"] for p in people),
        "total_logged": sum(p["logged"] for p in people),
        "themes": _top_themes(modules),
        "repeat_tasks": sorted(set(repeats)),
    }


# ── Groq ─────────────────────────────────────────────────────────────────────

def _facts_block(facts: dict) -> str:
    lines = []
    if facts["kind"] == "morning":
        lines.append(f"Report: morning plan for the day")
        lines.append(f"Team: {facts['total_people']} people, {facts['total_tasks']} tasks")
        lines.append(f"Estimated: {fmt_hours(facts['total_estimated'])}")
        lines.append(f"Logged so far: {fmt_hours(facts['total_logged'])}")
        for p in facts["people"]:
            lines.append(f"- {p['name']}: {p['tasks']} tasks, "
                         f"{fmt_hours(p['estimated'])} estimated, "
                         f"{fmt_hours(p['logged'])} logged")
    else:
        lines.append("Report: end of day actuals")
        lines.append(f"Team: {facts['total_people']} people, {facts['total_tasks']} tasks")
        lines.append(f"Planned: {facts['total_planned']}, unplanned: {facts['total_unplanned']}")
        lines.append(f"Logged: {fmt_hours(facts['total_logged'])}")
        for p in facts["people"]:
            lines.append(f"- {p['name']}: {p['planned']} planned, {p['unplanned']} unplanned, "
                         f"{fmt_hours(p['logged'])} logged")
        if facts.get("repeat_tasks"):
            lines.append("Tasks unplanned more than once this week: "
                         + ", ".join(facts["repeat_tasks"][:5]))
    if facts.get("themes"):
        lines.append("Work areas: " + ", ".join(facts["themes"]))
    return "\n".join(lines)


def _groq_takeaway(facts: dict) -> str:
    api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not api_key:
        return ""

    prompt = (
        "You write one short paragraph summarising a team's day for an internal "
        "email. Rules:\n"
        "- 1 to 2 sentences, maximum 45 words.\n"
        "- Use ONLY the figures below. Never invent numbers, names or tasks.\n"
        "- Wrap the two or three most important figures in **double asterisks**.\n"
        "- Plain prose. No bullet points, no headings, no preamble like "
        "'Here is a summary'.\n"
        "- State what is notable (a gap between planned and logged, one person "
        "carrying most of the work, repeated unplanned work) rather than "
        "listing everything.\n\n"
        f"{_facts_block(facts)}"
    )

    body = json.dumps({
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 160,
        "temperature": 0.3,
    }).encode()

    req = urllib.request.Request(
        GROQ_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=GROQ_TIMEOUT) as resp:
            data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            KeyError, IndexError, ValueError, TypeError) as exc:
        print(f"  [warn] Groq summary unavailable ({exc}) — using computed takeaway.")
        return ""

    # Strip anything list-like or multi-paragraph the model may still emit.
    text = " ".join(line.strip(" -*•") for line in text.splitlines() if line.strip())
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_TAKEAWAY_CHARS]


# ── Deterministic fallback ───────────────────────────────────────────────────

def _fallback_takeaway(facts: dict) -> str:
    themes = _join(facts.get("themes", [])[:3])

    if facts["kind"] == "morning":
        est = fmt_hours(facts["total_estimated"])
        logged = fmt_hours(facts["total_logged"])
        parts = [
            f"The team has **{est} of planned work** across "
            f"**{facts['total_tasks']} tasks**, with **{logged} logged** so far."
        ]
        if themes:
            parts.append(f"Most of today's work sits in {themes}.")
        return " ".join(parts)

    logged = fmt_hours(facts["total_logged"])
    if facts.get("have_snapshot"):
        parts = [
            f"**{logged} logged** today across **{facts['total_planned']} planned** "
            f"and **{facts['total_unplanned']} unplanned** tasks."
        ]
    else:
        parts = [f"**{logged} logged** today across "
                 f"**{facts['total_tasks']} tasks** (no morning plan to compare against)."]
    if facts.get("repeat_tasks"):
        n = len(facts["repeat_tasks"])
        parts.append(f"**{n} task{'s' if n != 1 else ''}** came up unplanned again this week.")
    if themes:
        parts.append(f"Work concentrated in {themes}.")
    return " ".join(parts)


def key_takeaway(facts: dict) -> str:
    """One short paragraph. May contain **bold** markers for the template."""
    try:
        text = _groq_takeaway(facts)
    except Exception as exc:                       # pragma: no cover - defensive
        print(f"  [warn] Groq summary failed ({exc}) — using computed takeaway.")
        text = ""
    return text or _fallback_takeaway(facts)
