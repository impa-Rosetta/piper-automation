#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Replay a Piper slot trajectory and execute recorded DIY gripper events."""

from __future__ import annotations

import argparse
import csv
import select
import sys
import time
from pathlib import Path
from typing import Any

try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from teach.common_slots import slot_key, slot_path, validate_slot
from teach.gripper_serial import DiyGripper
from teach.gripper_timeline import load_timeline, timeline_path
from teach.play_trajectory import enable_official, ensure_can_mode, import_official_piper
from teach.play_trajectory_precise import (
    converge,
    current_joints,
    send_target,
    wait_next,
)
from teach.trajectory_math import (
    TrajectorySample,
    max_abs_joint_error,
    rows_to_samples,
    sample_linear,
)


def load_piper_only_samples(path: Path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            rows = [[float(value) for value in row] for row in csv.reader(stream) if row]
    except FileNotFoundError as exc:
        raise SystemExit(f"ERROR: trajectory file not found: {path}") from exc
    return rows_to_samples(rows, have_gripper=False)


def recorded_period_ns(
    samples: list[TrajectorySample],
    index: int,
    *,
    play_speed: float,
    fallback_dt: float,
) -> int:
    """Use the original CSV delta time after the current row."""
    if index + 1 < len(samples):
        dt = samples[index + 1].time_from_start - samples[index].time_from_start
    else:
        dt = fallback_dt
    if dt <= 0:
        dt = fallback_dt
    return max(1, round((dt / play_speed) * 1_000_000_000))


def estimate_actual_progress_time(
    samples: list[Any],
    actual_joints: tuple[float, ...],
    previous_time: float,
    commanded_time: float,
) -> float:
    """Estimate feedback progress without jumping to a later repeated posture."""
    best_time = previous_time
    best_error = float("inf")
    upper_time = max(previous_time, commanded_time + 0.25)
    # The feeder posture appears at both ends of every task. Restrict matching to
    # the local commanded-time neighborhood so the start cannot match the return
    # segment and fire later gripper events early.
    for sample in samples:
        if sample.time_from_start + 0.25 < previous_time:
            continue
        if sample.time_from_start > upper_time:
            break
        error = sum(
            (float(target) - float(actual)) ** 2
            for target, actual in zip(sample.joints, actual_joints)
        )
        if error < best_error:
            best_error = error
            best_time = sample.time_from_start
    return min(upper_time, max(previous_time, best_time))


def read_key() -> str | None:
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    return sys.stdin.read(1).lower() if ready else None


def enter_raw_terminal() -> Any | None:
    if sys.stdin.isatty() and termios is not None and tty is not None:
        old = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        return old
    return None


def restore_terminal(old: Any | None) -> None:
    if old is not None and termios is not None:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)


def move_zero_home(args: argparse.Namespace) -> None:
    from teach.go_zero_home import main as zero_home_main

    old_argv = sys.argv
    try:
        sys.argv = [
            "go_zero_home.py",
            "--can-port",
            args.can_port,
            "--speed",
            str(args.home_speed),
            "--timeout",
            str(args.home_timeout),
            "--tolerance-deg",
            str(args.zero_home_tolerance_deg),
            "--yes",
        ]
        zero_home_main()
    finally:
        sys.argv = old_argv


def move_saved_start(args: argparse.Namespace) -> None:
    from teach.go_home import main as go_home_main

    old_argv = sys.argv
    try:
        sys.argv = [
            "go_home.py",
            "--can-port",
            args.can_port,
            "--home",
            args.start_point,
            "--speed",
            str(args.home_speed),
            "--timeout",
            str(args.home_timeout),
            "--tolerance",
            str(args.start_point_tolerance),
            "--no-gripper",
            "--yes",
        ]
        result = go_home_main()
        if result != 0:
            raise SystemExit(
                f"ERROR: move to saved start point failed, returncode={result}"
            )
    finally:
        sys.argv = old_argv


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay one Piper slot trajectory with recorded DIY gripper actions."
    )
    parser.add_argument("--row", type=int, required=True)
    parser.add_argument("--col", type=int, required=True)
    parser.add_argument("--can-port", default="can0")
    parser.add_argument("--root", default="teach/trajectories")
    parser.add_argument("--trajectory-file", default=None)
    parser.add_argument("--timeline", default=None)
    parser.add_argument("--speed", type=int, default=10)
    parser.add_argument("--play-speed", type=float, default=1.0)
    parser.add_argument("--stream-dt", type=float, default=0.005)
    parser.add_argument(
        "--clock",
        choices=("recorded", "resample"),
        default="recorded",
        help=(
            "recorded sends original CSV rows using recorded delta times; "
            "resample interpolates the CSV at --stream-dt."
        ),
    )
    parser.add_argument("--start-tolerance", type=float, default=0.008)
    parser.add_argument("--start-timeout", type=float, default=30.0)
    parser.add_argument("--max-start-error", type=float, default=3.2)
    parser.add_argument("--final-tolerance", type=float, default=0.006)
    parser.add_argument("--final-timeout", type=float, default=5.0)
    parser.add_argument("--settle", type=float, default=0.30)
    parser.add_argument("--tracking-error-limit", type=float, default=0.30)
    parser.add_argument("--tracking-timeout", type=float, default=2.0)
    parser.add_argument("--home-speed", type=int, default=10)
    parser.add_argument("--home-timeout", type=float, default=30.0)
    parser.add_argument("--zero-home-tolerance-deg", type=float, default=1.0)
    parser.add_argument(
        "--start-point",
        default=None,
        help=(
            "Optional saved joint point to use instead of all-zero Home, "
            "for example teach/feeder_above.json."
        ),
    )
    parser.add_argument("--start-point-tolerance", type=float, default=0.01)
    parser.add_argument("--gripper-port", default=None)
    parser.add_argument("--gripper-baudrate", type=int, default=9600)
    parser.add_argument("--gripper-timeout", type=float, default=0.3)
    parser.add_argument("--gripper-startup-delay", type=float, default=2.0)
    parser.add_argument("--gripper-feedback", action="store_true")
    parser.add_argument(
        "--event-sync",
        choices=("planned", "actual"),
        default="planned",
        help=(
            "planned fires gripper by commanded trajectory time; actual fires "
            "by nearest actual joint feedback progress, better for fast replay."
        ),
    )
    parser.add_argument(
        "--gripper-action-hold",
        type=float,
        default=0.0,
        help="Pause trajectory advancement for this many seconds after each gripper action.",
    )
    parser.add_argument(
        "--gripper-event-offset",
        type=float,
        default=0.0,
        help=(
            "Shift all recorded gripper events on the trajectory timeline. "
            "Positive delays actions; negative triggers actions earlier."
        ),
    )
    parser.add_argument("--dry-run-gripper", action="store_true")
    parser.add_argument(
        "--skip-zero-home",
        action="store_true",
        help=(
            "Do not move to all-zero Home before replay. This is intended for "
            "a preflight-checked task sequence whose previous trajectory ends "
            "at this trajectory's start."
        ),
    )
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.start_point_tolerance <= 0:
        parser.error("--start-point-tolerance must be positive")
    if args.start_point and args.skip_zero_home:
        parser.error("--start-point and --skip-zero-home cannot be used together")

    validate_slot(args.row, args.col)
    trajectory_file = (
        Path(args.trajectory_file)
        if args.trajectory_file
        else slot_path(args.row, args.col, args.root)
    )
    event_file = Path(args.timeline) if args.timeline else timeline_path(args.row, args.col)
    timeline = load_timeline(event_file)
    events = []
    for event in timeline["events"]:
        shifted = dict(event)
        shifted["time_s"] = max(0.0, float(event["time_s"]) + args.gripper_event_offset)
        events.append(shifted)
    samples = load_piper_only_samples(trajectory_file)
    duration = samples[-1].time_from_start

    print("=" * 72)
    print(f"Replay Piper + DIY gripper for slot {slot_key(args.row, args.col)}")
    print("=" * 72)
    print(f"Trajectory: {trajectory_file}")
    print(f"Timeline:   {event_file}")
    print(f"Samples: {len(samples)}, duration={duration:.3f}s")
    print(f"Replay clock: {args.clock}")
    print(f"Gripper event sync: {args.event_sync}")
    if args.gripper_event_offset:
        print(f"Gripper event offset: {args.gripper_event_offset:+.3f}s")
    if args.gripper_action_hold > 0:
        print(f"Hold after each gripper action: {args.gripper_action_hold:.3f}s")
    if args.skip_zero_home:
        print("Sequence start: keep current posture; zero Home is skipped.")
    elif args.start_point:
        print(f"Start point: {args.start_point}")
    else:
        print("Default start: all-zero joint Home.")
    print("Recorded gripper events:")
    for index, event in enumerate(events, start=1):
        print(f"  {index}. t={event['time_s']:.3f}s action={event['action']}")
    print("Controls during replay: P=pause, C=continue, S/Q=stop.")
    print("=" * 72)
    if not args.yes:
        if args.skip_zero_home:
            input("Input Enter to keep current posture and start linked replay.")
        elif args.start_point:
            input("Input Enter to move to feeder-above and start linked replay.")
        else:
            input("Input Enter to move zero Home and start linked replay.")

    if args.skip_zero_home:
        print("Keeping current posture for sequence continuation.")
    elif args.start_point:
        print(f"Moving to saved start point: {args.start_point} ...")
        move_saved_start(args)
    else:
        print("Moving to all-zero joint Home ...")
        move_zero_home(args)

    piper_sdk, Piper = import_official_piper()
    piper = Piper(args.can_port)
    interface = piper.init()
    piper.connect()
    time.sleep(0.1)
    ensure_can_mode(interface, piper, False, args.speed, 5.0)
    enable_official(interface, piper, False, args.speed)

    old_terminal = enter_raw_terminal()
    fired = 0
    try:
        with DiyGripper(
            port=args.gripper_port,
            baudrate=args.gripper_baudrate,
            timeout=args.gripper_timeout,
            startup_delay=args.gripper_startup_delay,
            wait_feedback=args.gripper_feedback,
            dry_run=args.dry_run_gripper,
        ) as gripper:
            converge(
                interface,
                piper,
                samples[0],
                label="trajectory start",
                speed=args.speed,
                have_gripper=False,
                tolerance=args.start_tolerance,
                timeout=args.start_timeout,
                settle=args.settle,
                stream_dt=args.stream_dt,
                maximum_initial_error=args.max_start_error,
            )
            fixed_period_ns = max(1, round(args.stream_dt * 1_000_000_000))
            period_ns = fixed_period_ns
            deadline_ns = time.monotonic_ns()
            trajectory_time = 0.0
            target_index = 0
            target = samples[0]
            actual_progress_time = 0.0
            hold_until_ns = 0
            paused = False
            recovery_since_ns: int | None = None
            next_print_ns = deadline_ns

            while trajectory_time < duration:
                if args.clock == "recorded":
                    period_ns = recorded_period_ns(
                        samples,
                        target_index,
                        play_speed=args.play_speed,
                        fallback_dt=args.stream_dt,
                    )
                else:
                    period_ns = fixed_period_ns
                now_ns, deadline_ns, _ = wait_next(deadline_ns, period_ns)
                key = read_key()
                if key == "p":
                    paused = True
                    print("\nPAUSED. Press C=continue or S/Q=stop.")
                elif key == "c":
                    paused = False
                    recovery_since_ns = None
                    print("\nCONTINUE")
                elif key in ("s", "q", "\x03"):
                    print("\nSTOP requested")
                    break

                actual = current_joints(piper)
                if args.event_sync == "actual":
                    actual_progress_time = estimate_actual_progress_time(
                        samples,
                        actual,
                        actual_progress_time,
                        trajectory_time,
                    )
                event_clock = (
                    actual_progress_time if args.event_sync == "actual" else trajectory_time
                )

                while fired < len(events) and events[fired]["time_s"] <= event_clock:
                    event = events[fired]
                    event_joints = event.get("joint_rad")
                    if event_joints is None:
                        event_joints = sample_linear(
                            samples, float(event["time_s"])
                        ).joints
                    event_pose_error = max_abs_joint_error(event_joints, actual)
                    gripper.send(str(event["action"]))
                    print(
                        f"\nAuto gripper {event['action']} at "
                        f"trajectory_time={trajectory_time:.3f}s "
                        f"event_clock={event_clock:.3f}s "
                        f"event_pose_error={event_pose_error:.6f}rad"
                    )
                    fired += 1
                    if args.gripper_action_hold > 0:
                        hold_until_ns = max(
                            hold_until_ns,
                            time.monotonic_ns()
                            + round(args.gripper_action_hold * 1_000_000_000),
                        )

                send_target(
                    interface,
                    piper,
                    target,
                    speed=args.speed,
                    have_gripper=False,
                )
                error = max_abs_joint_error(target.joints, actual)
                blocked = (
                    args.tracking_error_limit > 0
                    and error > args.tracking_error_limit * 2.5
                )
                if blocked:
                    if recovery_since_ns is None:
                        recovery_since_ns = now_ns
                        print(f"\nTRACKING HOLD: error={error:.6f} rad")
                    elif (
                        now_ns - recovery_since_ns
                        > round(args.tracking_timeout * 1_000_000_000)
                    ):
                        raise SystemExit("ERROR: tracking error did not recover.")
                else:
                    recovery_since_ns = None
                    if not paused and now_ns >= hold_until_ns:
                        if args.clock == "recorded":
                            if target_index + 1 < len(samples):
                                target_index += 1
                                target = samples[target_index]
                                trajectory_time = target.time_from_start
                            else:
                                trajectory_time = duration
                        else:
                            trajectory_time = min(
                                duration,
                                trajectory_time + args.stream_dt * args.play_speed,
                            )
                            target = sample_linear(samples, trajectory_time)

                if now_ns >= next_print_ns:
                    state = "PAUSED" if paused else "RUN"
                    print(
                        f"INFO: {state} t={trajectory_time:.3f}/{duration:.3f}s "
                        f"event_clock={event_clock:.3f}s "
                        f"events={fired}/{len(events)} error={error:.6f} rad"
                    )
                    next_print_ns = now_ns + 500_000_000

            converge(
                interface,
                piper,
                samples[-1],
                label="trajectory final",
                speed=args.speed,
                have_gripper=False,
                tolerance=args.final_tolerance,
                timeout=args.final_timeout,
                settle=args.settle,
                stream_dt=args.stream_dt,
            )
    finally:
        restore_terminal(old_terminal)
        disconnect = getattr(interface, "DisconnectPort", None)
        if callable(disconnect):
            disconnect()

    print(f"Linked replay complete. Fired gripper events: {fired}/{len(events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
