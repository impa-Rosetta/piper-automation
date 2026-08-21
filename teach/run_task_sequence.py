#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Preflight and run multiple field tasks without returning Home between tasks."""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from teach.production_stream import (
    PreparedTask,
    ProductionStream,
    ReplaySettings,
    prepare_task,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASK_ROOT = PROJECT_ROOT / "teach" / "production_tasks"


def production_cycles(cycles: int, infinite: bool) -> Iterable[int]:
    """Yield one-based production cycle numbers."""
    if cycles < 1:
        raise ValueError("production cycles must be at least 1")
    if infinite:
        return itertools.count(1)
    return range(1, cycles + 1)


@dataclass(frozen=True)
class Task:
    task_id: str
    directory: Path
    manifest: dict[str, Any]
    trajectory: Path
    timeline: Path
    first_joints: tuple[float, ...]
    last_joints: tuple[float, ...]
    rows: int
    duration_s: float
    event_count: int


def read_trajectory(path: Path) -> tuple[tuple[float, ...], tuple[float, ...], int, float]:
    first: tuple[float, ...] | None = None
    last: tuple[float, ...] | None = None
    rows = 0
    duration = 0.0
    with path.open("r", encoding="utf-8", newline="") as stream:
        for row in csv.reader(stream):
            if not row:
                continue
            if len(row) < 7:
                raise ValueError(f"{path}: row {rows + 1} has fewer than 7 columns")
            joints = tuple(float(value) for value in row[1:7])
            if first is None:
                first = joints
            last = joints
            duration += float(row[0])
            rows += 1
    if first is None or last is None:
        raise ValueError(f"{path}: trajectory is empty")
    return first, last, rows, duration


def load_task(task_root: Path, task_id: str) -> Task:
    directory = task_root / task_id
    manifest_path = directory / "task.json"
    trajectory = directory / "trajectory.csv"
    timeline = directory / "gripper_timeline.json"
    missing = [
        str(path)
        for path in (manifest_path, trajectory, timeline)
        if not path.exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"task {task_id!r} is incomplete; missing: {', '.join(missing)}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first, last, rows, duration = read_trajectory(trajectory)
    timeline_data = json.loads(timeline.read_text(encoding="utf-8"))
    events = timeline_data.get("events")
    if not isinstance(events, list):
        raise ValueError(f"{timeline}: missing events list")
    return Task(
        task_id=task_id,
        directory=directory,
        manifest=manifest,
        trajectory=trajectory,
        timeline=timeline,
        first_joints=first,
        last_joints=last,
        rows=rows,
        duration_s=duration,
        event_count=len(events),
    )


def max_error(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return max(abs(float(left) - float(right)) for left, right in zip(a, b))


def rad_to_deg(value: float) -> float:
    return value * 180.0 / math.pi


def resolve_task_ids(args: argparse.Namespace, task_root: Path) -> list[str]:
    if args.tasks:
        return list(args.tasks)
    if args.layer is None:
        raise ValueError("provide --tasks or --layer")
    if args.from_slot > args.to_slot:
        raise ValueError("--from-slot must not exceed --to-slot")
    ids = [
        f"layer_{args.layer:02d}_slot_{slot:02d}"
        for slot in range(args.from_slot, args.to_slot + 1)
    ]
    existing = [task_id for task_id in ids if (task_root / task_id).is_dir()]
    if not existing:
        raise FileNotFoundError(
            f"no tasks found for layer {args.layer}, slots "
            f"{args.from_slot}..{args.to_slot}"
        )
    if not args.allow_gaps and existing != ids:
        missing = [task_id for task_id in ids if task_id not in existing]
        raise FileNotFoundError(
            "sequence has missing tasks: " + ", ".join(missing)
        )
    return existing


def preflight(
    tasks: list[Task],
    *,
    home_start_limit: float,
    boundary_limit: float,
    anchor_joints: tuple[float, ...] | None = None,
    anchor_limit: float = 0.035,
) -> list[dict[str, Any]]:
    if not tasks:
        raise ValueError("task sequence is empty")
    report: list[dict[str, Any]] = []
    if anchor_joints is None:
        home_error = max(abs(value) for value in tasks[0].first_joints)
        report.append(
            {
                "kind": "home_to_first",
                "to": tasks[0].task_id,
                "max_error_rad": home_error,
                "max_error_deg": rad_to_deg(home_error),
                "limit_rad": home_start_limit,
                "ok": home_error <= home_start_limit,
            }
        )
    else:
        for task in tasks:
            for endpoint, joints in (
                ("start", task.first_joints),
                ("end", task.last_joints),
            ):
                error = max_error(anchor_joints, joints)
                report.append(
                    {
                        "kind": "anchor_endpoint",
                        "task": task.task_id,
                        "endpoint": endpoint,
                        "max_error_rad": error,
                        "max_error_deg": rad_to_deg(error),
                        "limit_rad": anchor_limit,
                        "ok": error <= anchor_limit,
                    }
                )
    for previous, current in zip(tasks, tasks[1:]):
        error = max_error(previous.last_joints, current.first_joints)
        report.append(
            {
                "kind": "task_boundary",
                "from": previous.task_id,
                "to": current.task_id,
                "max_error_rad": error,
                "max_error_deg": rad_to_deg(error),
                "limit_rad": boundary_limit,
                "ok": error <= boundary_limit,
            }
        )
    return report


def print_report(tasks: list[Task], report: list[dict[str, Any]]) -> None:
    print("=" * 78)
    print("Piper continuous production task sequence")
    print("=" * 78)
    total_duration = sum(task.duration_s for task in tasks)
    print(
        f"tasks={len(tasks)} trajectory_duration={total_duration:.3f}s "
        f"gripper_events={sum(task.event_count for task in tasks)}"
    )
    for index, task in enumerate(tasks, start=1):
        print(
            f"  {index}. {task.task_id}: samples={task.rows}, "
            f"duration={task.duration_s:.3f}s, events={task.event_count}"
        )
    print("-" * 78)
    for item in report:
        state = "PASS" if item["ok"] else "FAIL"
        if item["kind"] == "home_to_first":
            name = f"zero_home -> {item['to']}"
        elif item["kind"] == "anchor_endpoint":
            name = (
                f"feeder_above <-> {item['task']} "
                f"{item['endpoint']}"
            )
        else:
            name = f"{item['from']} -> {item['to']}"
        print(
            f"  [{state}] {name}: {item['max_error_rad']:.6f} rad "
            f"({item['max_error_deg']:.3f} deg), "
            f"limit={item['limit_rad']:.6f} rad"
        )
    print("=" * 78)


def go_zero_home(args: argparse.Namespace) -> None:
    command = [
        sys.executable,
        "-m",
        "teach.go_zero_home",
        "--can-port",
        args.can_port,
        "--speed",
        str(args.home_speed),
        "--timeout",
        str(args.home_timeout),
        "--tolerance-deg",
        str(args.home_tolerance_deg),
        "--yes",
    ]
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"zero Home failed with returncode={result.returncode}"
        )


def load_anchor(path: Path) -> tuple[float, ...]:
    if not path.exists():
        raise FileNotFoundError(
            f"feeder-above point not found: {path}. "
            "Record it in the field workstation first."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    joints = data.get("joint")
    if not isinstance(joints, list) or len(joints) != 6:
        raise ValueError(f"{path}: expected six joint values in 'joint'")
    return tuple(float(value) for value in joints)


def go_anchor(args: argparse.Namespace, path: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "teach.go_home",
        "--can-port",
        args.can_port,
        "--home",
        str(path),
        "--speed",
        str(args.anchor_speed),
        "--timeout",
        str(args.home_timeout),
        "--tolerance",
        str(args.anchor_limit),
        "--no-gripper",
        "--yes",
    ]
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"move to feeder-above failed with returncode={result.returncode}"
        )


def replay_settings(task: Task, args: argparse.Namespace) -> ReplaySettings:
    replay = (
        {}
        if args.uniform_replay_settings
        else task.manifest.get("replay", {})
    )
    speed = int(replay.get("speed_percent", args.speed))
    play_speed = float(replay.get("play_speed", args.play_speed))
    stream_dt = float(replay.get("stream_dt_s", args.stream_dt))
    clock = str(replay.get("clock", args.clock))
    event_sync = str(replay.get("event_sync", args.event_sync))
    gripper_action_hold = float(
        replay.get("gripper_action_hold_s", args.gripper_action_hold)
    )
    gripper_event_offset = float(
        replay.get("gripper_event_offset_s", args.gripper_event_offset)
    )
    if clock not in {"recorded", "resample"}:
        raise RuntimeError(f"{task.task_id}: invalid replay clock {clock!r}")
    if event_sync not in {"actual", "planned"}:
        raise RuntimeError(f"{task.task_id}: invalid event sync {event_sync!r}")
    return ReplaySettings(
        speed=speed,
        play_speed=play_speed,
        stream_dt=stream_dt,
        clock=clock,
        event_sync=event_sync,
        gripper_action_hold=gripper_action_hold,
        gripper_event_offset=gripper_event_offset,
    )


def replay_task(task: Task, args: argparse.Namespace) -> None:
    settings = replay_settings(task, args)
    print(
        f"[SETTINGS] {task.task_id}: speed={settings.speed}% "
        f"play_speed={settings.play_speed} clock={settings.clock} "
        f"event_sync={settings.event_sync} "
        f"hold={settings.gripper_action_hold:.3f}s "
        f"offset={settings.gripper_event_offset:+.3f}s"
    )
    command = [
        sys.executable,
        "-m",
        "teach.play_slot_with_gripper",
        "--can-port",
        args.can_port,
        "--row",
        "1",
        "--col",
        "1",
        "--trajectory-file",
        str(task.trajectory),
        "--timeline",
        str(task.timeline),
        "--gripper-port",
        args.gripper_port,
        "--speed",
        str(settings.speed),
        "--play-speed",
        str(settings.play_speed),
        "--stream-dt",
        str(settings.stream_dt),
        "--clock",
        settings.clock,
        "--tracking-error-limit",
        str(args.tracking_error_limit),
        "--event-sync",
        settings.event_sync,
        "--gripper-action-hold",
        str(settings.gripper_action_hold),
        "--gripper-event-offset",
        str(settings.gripper_event_offset),
        "--gripper-startup-delay",
        str(args.gripper_startup_delay),
        "--skip-zero-home",
        "--yes",
    ]
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"task {task.task_id} failed with returncode={result.returncode}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run recorded field tasks continuously. Zero Home is executed once; "
            "trajectory boundaries are checked before any motion."
        )
    )
    parser.add_argument("--tasks", nargs="+", default=None)
    parser.add_argument("--layer", type=int, default=None)
    parser.add_argument("--from-slot", type=int, default=1)
    parser.add_argument("--to-slot", type=int, default=27)
    parser.add_argument("--allow-gaps", action="store_true")
    parser.add_argument("--task-root", default=str(DEFAULT_TASK_ROOT))
    parser.add_argument("--can-port", default="can0")
    parser.add_argument("--gripper-port", default="/dev/ttyACM0")
    parser.add_argument("--speed", type=int, default=10)
    parser.add_argument("--play-speed", type=float, default=1.0)
    parser.add_argument("--stream-dt", type=float, default=0.005)
    parser.add_argument("--clock", choices=("recorded", "resample"), default="recorded")
    parser.add_argument("--event-sync", choices=("actual", "planned"), default="actual")
    parser.add_argument("--gripper-action-hold", type=float, default=0.30)
    parser.add_argument("--gripper-event-offset", type=float, default=0.0)
    parser.add_argument("--gripper-startup-delay", type=float, default=0.3)
    parser.add_argument("--gripper-baudrate", type=int, default=9600)
    parser.add_argument("--gripper-timeout", type=float, default=0.3)
    parser.add_argument("--gripper-feedback", action="store_true")
    parser.add_argument("--dry-run-gripper", action="store_true")
    parser.add_argument("--tracking-error-limit", type=float, default=0.50)
    parser.add_argument("--tracking-timeout", type=float, default=2.0)
    parser.add_argument(
        "--uniform-replay-settings",
        action="store_true",
        help=(
            "Ignore each task.json replay settings and apply the command-line "
            "speed/timing settings uniformly. Default uses each task's tested settings."
        ),
    )
    parser.add_argument("--home-speed", type=int, default=10)
    parser.add_argument("--home-timeout", type=float, default=30.0)
    parser.add_argument("--home-tolerance-deg", type=float, default=1.0)
    parser.add_argument(
        "--anchor",
        default=None,
        help=(
            "Optional saved joint point used as the common start/end of every "
            "task. The GUI uses teach/feeder_above.json."
        ),
    )
    parser.add_argument("--anchor-speed", type=int, default=30)
    parser.add_argument(
        "--anchor-limit",
        type=float,
        default=0.035,
        help="Maximum joint error in rad between a CSV endpoint and the anchor.",
    )
    parser.add_argument(
        "--home-start-limit",
        type=float,
        default=0.035,
        help="Maximum radian difference between zero Home and task 1 start.",
    )
    parser.add_argument(
        "--boundary-limit",
        type=float,
        default=0.035,
        help="Maximum radian difference at each adjacent trajectory boundary.",
    )
    parser.add_argument("--between-task-delay", type=float, default=0.20)
    parser.add_argument(
        "--cycles",
        type=int,
        default=1,
        help="Number of complete task-sequence cycles to run (default: 1).",
    )
    parser.add_argument(
        "--infinite",
        action="store_true",
        help="Repeat the complete task sequence until the operator stops it.",
    )
    parser.add_argument(
        "--no-auto-trim",
        action="store_true",
        help="Keep every recorded idle sample instead of trimming endpoint dwell.",
    )
    parser.add_argument(
        "--idle-trim-threshold",
        type=float,
        default=0.003,
        help="Joint-motion threshold in rad used to detect recorded endpoint dwell.",
    )
    parser.add_argument(
        "--leading-settle",
        type=float,
        default=0.20,
        help="Seconds of recorded stillness retained before each task starts moving.",
    )
    parser.add_argument(
        "--trailing-settle",
        type=float,
        default=0.10,
        help="Seconds of recorded stillness retained after each task returns.",
    )
    parser.add_argument("--continuous-final-tolerance", type=float, default=0.015)
    parser.add_argument("--continuous-final-timeout", type=float, default=2.0)
    parser.add_argument("--continuous-final-settle", type=float, default=0.05)
    parser.add_argument(
        "--event-position-tolerance",
        type=float,
        default=0.025,
        help=(
            "For actual event sync, pause at the recorded gripper-event joint "
            "position until max joint error is within this radian tolerance."
        ),
    )
    parser.add_argument("--event-wait-timeout", type=float, default=2.0)
    parser.add_argument(
        "--legacy-subprocess",
        action="store_true",
        help="Use the old one-Python-process-per-task sequence implementation.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.anchor_speed <= 30:
        parser.error("--anchor-speed must be 1..30")
    if args.anchor_limit <= 0:
        parser.error("--anchor-limit must be positive")
    if args.idle_trim_threshold < 0:
        parser.error("--idle-trim-threshold cannot be negative")
    if args.leading_settle < 0 or args.trailing_settle < 0:
        parser.error("trim settle times cannot be negative")
    if args.between_task_delay < 0:
        parser.error("--between-task-delay cannot be negative")
    if args.cycles < 1:
        parser.error("--cycles must be at least 1")
    if args.event_position_tolerance <= 0 or args.event_wait_timeout <= 0:
        parser.error("event position tolerance and timeout must be positive")

    task_root = Path(args.task_root)
    try:
        task_ids = resolve_task_ids(args, task_root)
        tasks = [load_task(task_root, task_id) for task_id in task_ids]
        anchor_path = Path(args.anchor).resolve() if args.anchor else None
        anchor_joints = load_anchor(anchor_path) if anchor_path else None
        report = preflight(
            tasks,
            home_start_limit=args.home_start_limit,
            boundary_limit=args.boundary_limit,
            anchor_joints=anchor_joints,
            anchor_limit=args.anchor_limit,
        )
        prepared: list[PreparedTask] = [
            prepare_task(
                task_id=task.task_id,
                trajectory=task.trajectory,
                timeline=task.timeline,
                settings=replay_settings(task, args),
                auto_trim=not args.no_auto_trim,
                idle_threshold=args.idle_trim_threshold,
                leading_keep=args.leading_settle,
                trailing_keep=args.trailing_settle,
            )
            for task in tasks
        ]
    except (FileNotFoundError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print_report(tasks, report)
    cycle_label = (
        "infinite (stop with S/Q or Ctrl+C)"
        if args.infinite
        else str(args.cycles)
    )
    print(f"Production cycles: {cycle_label}")
    print("Production stream preparation:")
    for item in prepared:
        print(
            f"  {item.task_id}: {item.original_duration_s:.3f}s -> "
            f"{item.duration_s:.3f}s, trim_start={item.trim_start_s:.3f}s, "
            f"trim_end={item.trim_end_s:.3f}s, saved={item.saved_s:.3f}s"
        )
        print(
            f"    speed={item.settings.speed}% "
            f"play_speed={item.settings.play_speed} "
            f"event_sync={item.settings.event_sync}"
        )
        if (
            item.annotation_speed is not None
            and item.annotation_speed != item.settings.speed
        ):
            print(
                f"    WARNING: gripper annotation used "
                f"{item.annotation_speed}% but production is configured for "
                f"{item.settings.speed}%"
            )
        if (
            item.annotation_play_speed is not None
            and abs(item.annotation_play_speed - item.settings.play_speed) > 1e-9
        ):
            print(
                f"    WARNING: gripper annotation used play_speed="
                f"{item.annotation_play_speed} but production uses "
                f"{item.settings.play_speed}"
            )
    failed = [item for item in report if not item["ok"]]
    if failed:
        if anchor_path:
            print(
                "ERROR: sequence preflight failed. Re-record every failed CSV "
                "so both its first and last frame are the saved feeder-above point.",
                file=sys.stderr,
            )
        else:
            print(
                "ERROR: sequence preflight failed. Re-record the boundary so the "
                "previous endpoint and next start point are the same.",
                file=sys.stderr,
            )
        return 3
    if args.dry_run:
        print("Dry run complete. No CAN, robot, or gripper command was sent.")
        return 0

    if anchor_path:
        print(
            "The arm will move to the saved feeder-above point once, then run "
            "every task continuously. It will not return Home between cycles."
        )
    else:
        print(
            "The arm will return to zero Home once, then run every task "
            "continuously. It will not return Home between cycles."
        )
    print("Clear the workspace and keep the physical E-stop ready.")
    if not args.yes and input("Type RUN to start the complete sequence: ").strip() != "RUN":
        print("Cancelled.")
        return 1

    try:
        if anchor_path:
            print("\n[START] Moving to feeder-above once ...")
            go_anchor(args, anchor_path)
        else:
            print("\n[START] Moving to zero Home once ...")
            go_zero_home(args)
        if args.legacy_subprocess:
            for cycle in production_cycles(args.cycles, args.infinite):
                cycle_total = "infinite" if args.infinite else str(args.cycles)
                print(f"\n[CYCLE {cycle}/{cycle_total}] START (legacy mode)")
                for index, task in enumerate(tasks, start=1):
                    print(
                        f"\n[START cycle={cycle} task={index}/{len(tasks)}] "
                        f"{task.task_id}"
                    )
                    replay_task(task, args)
                    print(f"[DONE] {task.task_id}")
                    if args.between_task_delay > 0:
                        time.sleep(args.between_task_delay)
                print(f"[CYCLE {cycle}/{cycle_total}] DONE")
        else:
            stream = ProductionStream(
                can_port=args.can_port,
                gripper_port=args.gripper_port,
                gripper_baudrate=args.gripper_baudrate,
                gripper_timeout=args.gripper_timeout,
                gripper_startup_delay=args.gripper_startup_delay,
                gripper_feedback=args.gripper_feedback,
                dry_run_gripper=args.dry_run_gripper,
                tracking_error_limit=args.tracking_error_limit,
                tracking_timeout=args.tracking_timeout,
                boundary_limit=args.boundary_limit,
                final_tolerance=args.continuous_final_tolerance,
                final_timeout=args.continuous_final_timeout,
                final_settle=args.continuous_final_settle,
                between_task_delay=args.between_task_delay,
                event_position_tolerance=args.event_position_tolerance,
                event_wait_timeout=args.event_wait_timeout,
            )
            with stream:
                stream.initialize_motion(prepared[0].settings.speed)
                first_replay = True
                completed_cycles = 0
                for cycle in production_cycles(args.cycles, args.infinite):
                    cycle_total = "infinite" if args.infinite else str(args.cycles)
                    print(f"\n[CYCLE {cycle}/{cycle_total}] START")
                    for index, task in enumerate(prepared, start=1):
                        print(
                            f"\n[START cycle={cycle} task={index}/{len(prepared)}] "
                            f"{task.task_id}"
                        )
                        if not stream.replay(task, first_task=first_replay):
                            print(
                                f"Sequence stopped by operator after "
                                f"{completed_cycles} complete cycle(s)."
                            )
                            return 1
                        first_replay = False
                    completed_cycles = cycle
                    print(f"[CYCLE {cycle}/{cycle_total}] DONE")
    except KeyboardInterrupt:
        print("\nSTOP: Ctrl+C received. Use the physical E-stop if motion continues.")
        return 130
    except SystemExit as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print("Sequence stopped; the next task was not started.", file=sys.stderr)
        return 4
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print("Sequence stopped; the next task was not started.", file=sys.stderr)
        return 4

    print(f"\nAll tasks completed successfully for {args.cycles} cycle(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
