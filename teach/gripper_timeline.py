#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read and write gripper event timelines bound to a Piper trajectory."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from teach.common_slots import slot_key


def timeline_path(
    row: int,
    col: int,
    root: str | Path = "teach/gripper_timelines",
) -> Path:
    return Path(root) / f"{slot_key(row, col)}.json"


def save_timeline(
    path: Path,
    *,
    row: int,
    col: int,
    trajectory_file: Path,
    trajectory_duration_s: float,
    events: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "piper_diy_gripper_timeline_v1",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "row": row,
        "col": col,
        "slot": slot_key(row, col),
        "trajectory_file": str(trajectory_file),
        "trajectory_duration_s": trajectory_duration_s,
        "events": sorted(events, key=lambda item: float(item["time_s"])),
        "metadata": metadata or {},
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_timeline(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"ERROR: gripper timeline file not found: {path}") from exc
    if data.get("format") != "piper_diy_gripper_timeline_v1":
        raise SystemExit(f"ERROR: unsupported gripper timeline format: {path}")
    events = data.get("events")
    if not isinstance(events, list):
        raise SystemExit(f"ERROR: gripper timeline has no event list: {path}")
    for index, event in enumerate(events, start=1):
        if event.get("action") not in ("open", "close", "grip", "release"):
            raise SystemExit(f"ERROR: invalid gripper action at event {index}: {event}")
        try:
            event["time_s"] = float(event["time_s"])
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"ERROR: invalid event time at event {index}: {event}") from exc
    data["events"] = sorted(events, key=lambda item: item["time_s"])
    return data
