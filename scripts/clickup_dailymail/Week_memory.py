#!/usr/bin/env python3
"""Week-to-date memory for the evening ClickUp report.

Purpose
-------
Remember which tasks have already turned up as UNPLANNED earlier in the same
week, so a task that keeps getting logged but never planned can be flagged
("3rd time this week") instead of looking like a one-off each evening.

Storage
-------
A small JSON file kept between runs by the GitHub Actions cache — see the
"week memory" steps in .github/workflows/clickup_evening_report.yml.

That cache is best-effort by design: entries can be evicted, and the first run
of a new week has nothing to restore. Every function here treats a missing or
unreadable file as "no history yet" and returns empty state, so the evening
report renders exactly as it did before, just without the week badges. Nothing
in this module can fail the run.

The week rolls over on Monday (ISO week), and history from a previous week is
discarded rather than merged.
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Dict


DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "week_memory.json")


def week_key(day: date = None) -> str:
    """ISO week label, e.g. '2026-W36'. Weeks start Monday."""
    day = day or date.today()
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _empty(week: str) -> Dict:
    return {"week": week, "unplanned": {}}


def load(path: str = DEFAULT_PATH, day: date = None) -> Dict:
    """Read this week's memory, or a fresh empty one.

    Returns empty state (never raises) when the file is absent, unreadable,
    malformed, or left over from a previous week.
    """
    week = week_key(day)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _empty(week)

    if not isinstance(data, dict) or data.get("week") != week:
        # New week (or junk) — start clean rather than mixing weeks.
        return _empty(week)

    unplanned = data.get("unplanned")
    if not isinstance(unplanned, dict):
        return _empty(week)

    # Defensive: drop anything that isn't shaped like a record we wrote.
    clean = {}
    for task_id, rec in unplanned.items():
        if isinstance(rec, dict) and isinstance(rec.get("dates"), list):
            clean[str(task_id)] = {
                "dates": [str(d) for d in rec["dates"]],
                "task_name": str(rec.get("task_name", "")),
                "people": [str(p) for p in rec.get("people", []) if p],
            }
    return {"week": week, "unplanned": clean}


def record_unplanned(memory: Dict, task_id: str, task_name: str,
                     person: str, day: date = None) -> int:
    """Log that `task_id` was unplanned today; return how many DAYS this week
    it has now been unplanned (1 = first time this week).

    Recording the same task twice on the same day does not inflate the count —
    two people logging the same unplanned task still counts as one day.
    """
    if not task_id:
        return 1

    day_iso = (day or date.today()).isoformat()
    rec = memory.setdefault("unplanned", {}).setdefault(
        str(task_id), {"dates": [], "task_name": "", "people": []}
    )

    if day_iso not in rec["dates"]:
        rec["dates"].append(day_iso)
    if task_name:
        rec["task_name"] = task_name
    if person and person not in rec["people"]:
        rec["people"].append(person)

    return len(rec["dates"])


def occurrences(memory: Dict, task_id: str) -> int:
    """Days this week `task_id` has been unplanned. 0 when never seen."""
    rec = (memory.get("unplanned") or {}).get(str(task_id))
    return len(rec["dates"]) if rec else 0


def save(memory: Dict, path: str = DEFAULT_PATH) -> bool:
    """Persist memory. Returns False on failure — never raises.

    A failed write only costs the week badges on the next run, so it must not
    take the report down with it.
    """
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(memory, fh, indent=2, sort_keys=True)
        return True
    except OSError as exc:
        print(f"  [warn] Could not save week memory: {exc}")
        return False


def ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 11 -> '11th'."""
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
