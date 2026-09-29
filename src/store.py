"""快照历史与备注的持久化。

历史结构 ``data/state/history.json``::

    {
      "version": 1,
      "updated": "2026-09-29T20:40:00",
      "runs": [
        {
          "date": "2026-09-29",
          "collected_at": "...",
          "has_following": true,
          "has_fans": true,
          "following": ["<key>", ...],
          "fans": ["<key>", ...],
          "users": {"<key>": {...}},
          "counts": {"following": 0, "fans": 0, "mutual": 0},
          "sources": {"following": "douyin_following_2026-09-29.json", ...}
        }
      ],
      "events": [
        {"date": "...", "type": "unfollowed_me", "key": "...",
         "nickname": "...", "douyin_id": "...", "remark": "..."}
      ]
    }
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

HISTORY_VERSION = 1
MAX_RUNS = 60          # 保留最近 60 次采集
MAX_EVENTS = 5000      # 事件流水上限


# --------------------------------------------------------------- JSON 读写

def load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return default


def save_json(path, data) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------- 历史

def load_history(path) -> Dict[str, Any]:
    data = load_json(path, None)
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        return {"version": HISTORY_VERSION, "runs": [], "events": []}
    data.setdefault("version", HISTORY_VERSION)
    data.setdefault("events", [])
    return data


def save_history(path, history: Dict[str, Any]) -> None:
    history["runs"] = (history.get("runs") or [])[-MAX_RUNS:]
    history["events"] = (history.get("events") or [])[-MAX_EVENTS:]
    history["updated"] = datetime.now().isoformat(timespec="seconds")
    save_json(path, history)


def last_run(history: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    runs = history.get("runs") or []
    return runs[-1] if runs else None


def append_run(history: Dict[str, Any], run: Dict[str, Any]) -> None:
    """同一日期重复采集时覆盖当天记录，避免流水被同一天的多次运行刷屏。"""
    runs = history.setdefault("runs", [])
    if runs and runs[-1].get("date") == run.get("date"):
        runs[-1] = run
    else:
        runs.append(run)


def append_events(history: Dict[str, Any], events: List[Dict[str, Any]]) -> None:
    if events:
        history.setdefault("events", []).extend(events)


# ----------------------------------------------------------------- 备注

REMARK_HEADER = ["douyin_id", "remark"]


def load_remarks(path) -> Dict[str, str]:
    """读 ``remarks.csv`` -> ``{抖音号: 备注}``。"""
    p = Path(path)
    if not p.exists():
        return {}
    out: Dict[str, str] = {}
    try:
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                did = (row.get("douyin_id") or "").strip()
                remark = (row.get("remark") or "").strip()
                if did and remark:
                    out[did] = remark
    except OSError:
        return {}
    return out


def save_remarks(path, remarks: Dict[str, str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(REMARK_HEADER)
        for did in sorted(remarks):
            writer.writerow([did, remarks[did]])
