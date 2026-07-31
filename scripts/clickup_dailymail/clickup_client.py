import time
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple

CLICKUP_BASE = "https://api.clickup.com/api/v2"


def this_week_ms() -> Tuple[int, int]:
    """Return (start_ms, end_ms) covering this week Mon 00:00 through next week Sun 23:59:59 UTC."""
    now = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    next_sunday = monday + timedelta(days=13, hours=23, minutes=59, seconds=59)
    return int(monday.timestamp() * 1000), int(next_sunday.timestamp() * 1000)


_MIN_CALL_INTERVAL = 0.68  # ~88 calls/min, safely under ClickUp's 100/min limit


class ClickUpClient:
    def __init__(self, api_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": api_token,
            "Content-Type": "application/json",
        })
        self._last_call_at: float = 0.0

    def _get(self, path: str, params: dict = None) -> dict:
        # Proactive throttle — never hit the rate limit in the first place
        wait = _MIN_CALL_INTERVAL - (time.time() - self._last_call_at)
        if wait > 0:
            time.sleep(wait)

        url = f"{CLICKUP_BASE}/{path}"
        for attempt in range(3):
            self._last_call_at = time.time()
            resp = self.session.get(url, params=params)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                print(f"  Rate limited — waiting {retry_after}s...")
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Failed after 3 attempts: GET {path}")

    # ── Workspace ────────────────────────────────────────────────────────────

    def get_spaces(self, team_id: str) -> List[Dict]:
        return self._get(f"team/{team_id}/space", {"archived": False})["spaces"]

    def get_team_id(self) -> str:
        teams = self._get("team")["teams"]
        if not teams:
            raise RuntimeError("No ClickUp workspace found for this token.")
        return teams[0]["id"]

    # ── Hierarchy ────────────────────────────────────────────────────────────

    def get_folders(self, space_id: str) -> List[Dict]:
        return self._get(f"space/{space_id}/folder", {"archived": False}).get("folders", [])

    def get_folderless_lists(self, space_id: str) -> List[Dict]:
        return self._get(f"space/{space_id}/list", {"archived": False}).get("lists", [])

    def get_lists_in_folder(self, folder_id: str) -> List[Dict]:
        return self._get(f"folder/{folder_id}/list", {"archived": False}).get("lists", [])

    def get_all_lists(self, space_id: str) -> List[Dict]:
        lists = self.get_folderless_lists(space_id)
        for folder in self.get_folders(space_id):
            lists.extend(self.get_lists_in_folder(folder["id"]))
        return lists

    # ── Tasks ────────────────────────────────────────────────────────────────

    def get_tasks(self, list_id: str, week_start_ms: int, week_end_ms: int) -> List[Dict]:
        """
        Phase 1 — fetch open root tasks due THIS week.
        Phase 2 — for each root task, explicitly fetch its open subtasks.
                   (ClickUp's subtasks=true silently drops subtasks created via
                   the task detail view; ?parent= is the only reliable route.)
        """
        tasks: List[Dict] = []
        seen: set = set()

        def _add(t: Dict) -> None:
            if t["id"] not in seen:
                seen.add(t["id"])
                tasks.append(t)

        # Phase 1 — open root tasks due this week
        root_ids: List[str] = []
        page = 0
        while True:
            data = self._get(f"list/{list_id}/task", {
                "include_closed": False,
                "due_date_gt": week_start_ms - 1,
                "due_date_lt": week_end_ms,
                "page": page,
            })
            batch = data.get("tasks", [])
            for t in batch:
                _add(t)
                if not t.get("parent"):
                    root_ids.append(t["id"])
                for sub in t.get("subtasks", []):   # handle if ClickUp does return them
                    _add(sub)
            if not batch or data.get("last_page", True):
                break
            page += 1

        # Phase 2 — recursively fetch subtasks at all depths
        def _fetch_children(parent_id: str) -> None:
            try:
                data = self._get(f"list/{list_id}/task", {
                    "parent": parent_id,
                    "include_closed": False,
                })
                for sub in data.get("tasks", []):
                    if sub["id"] not in seen:
                        _add(sub)
                        _fetch_children(sub["id"])
            except Exception:
                pass

        for parent_id in root_ids:
            _fetch_children(parent_id)

        return tasks

    # ── Comments ─────────────────────────────────────────────────────────────

    def get_comments(self, task_id: str) -> List[Dict]:
        data = self._get(f"task/{task_id}/comment")
        return data.get("comments", [])

    # ── Time tracking ────────────────────────────────────────────────────────

    def get_time_entries(self, team_id: str, start_ms: int, end_ms: int, assignee_id: int = None) -> List[Dict]:
        params = {"start_date": start_ms, "end_date": end_ms}
        if assignee_id:
            params["assignee"] = assignee_id
        data = self._get(f"team/{team_id}/time_entries", params)
        return data.get("data", [])

    def get_workspace_members(self, team_id: str) -> List[Dict]:
        data = self._get(f"team/{team_id}")
        members = data.get("team", {}).get("members", [])
        return [m.get("user", {}) for m in members]
