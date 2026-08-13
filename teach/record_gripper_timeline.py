#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Replay a slot trajectory and record DIY gripper actions at paused times."""

from __future__ import annotations

import argparse
import csv
import json
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
from teach.gripper_serial import ACTION_TO_PROTOCOL, DiyGripper
from teach.gripper_timeline import save_timeline, timeline_path
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
        description=(
            "Replay one taught Piper slot trajectory, pause with P, press G/O "
            "to record DIY gripper close/open events, then save a timeline JSON."
        )
    )
    parser.add_argument("--row", type=int, required=True)
    parser.add_argument("--col", type=int, required=True)
    parser.add_argument("--can-port", default="can0")
    parser.add_argument("--root", default="teach/trajectories")
    parser.add_argument("--trajectory-file", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--overwrite", action="store_true")
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
    parser.add_argument("--final-tolerance", type=float, default=0.02)
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
    parser.add_argument("--dry-run-gripper", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.start_point_tolerance <= 0:
        parser.error("--start-point-tolerance must be positive")

    validate_slot(args.row, args.col)
    trajectory_file = (
        Path(args.trajectory_file)
        if args.trajectory_file
        else slot_path(args.row, args.col, args.root)
    )
    output = (
        Path(args.output)
        if args.output
        else timeline_path(args.row, args.col)
    )
    if output.exists() and not args.overwrite:
        raise SystemExit(f"ERROR: timeline exists: {output}. Add --overwrite.")

    samples = load_piper_only_samples(trajectory_file)
    duration = samples[-1].time_from_start

    print("=" * 72)
    print(f"Record DIY gripper timeline for slot {slot_key(args.row, args.col)}")
    print("=" * 72)
    print(f"Trajectory: {trajectory_file}")
    print(f"Timeline:   {output}")
    print(f"Samples: {len(samples)}, duration={duration:.3f}s")
    print(f"Replay clock: {args.clock}")
    if args.start_point:
        print(f"Start point: {args.start_point}")
    else:
        print("Default start: all-zero joint Home.")
    print("Controls during replay:")
    print("  P: pause/resume prompt")
    print("  C: continue after pause")
    print("  G: close/grip and record this trajectory time")
    print("  O: open/release and record this trajectory time")
    print("  S/Q: stop and save")
    print("Use G/O while PAUSED so the timing is exact and repeatable.")
    print("=" * 72)
    if not args.yes:
        if args.start_point:
            input(
                "Input Enter to move to feeder-above and start annotation replay."
            )
        else:
            input("Input Enter to move zero Home and start annotation replay.")

    if args.start_point:
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

    events: list[dict[str, Any]] = []
    old_terminal = enter_raw_terminal()
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
            paused = False
            stopped = False
            recovery_since_ns: int | None = None
            next_print_ns = deadline_ns
            replay_start = time.monotonic()

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
                    print(
                        "\nPAUSED. Press G=close, O=open, C=continue, S/Q=save-stop."
                    )
                elif key == "c":
                    paused = False
                    recovery_since_ns = None
                    print("\nCONTINUE")
                elif key in ("g", "o"):
                    if not paused:
                        print("\nPause first with P, then press G/O to mark gripper.")
                    else:
                        action = "close" if key == "g" else "open"
                        gripper.send(action)
                        event_joints = current_joints(piper)
                        event = {
                            "time_s": round(float(trajectory_time), 6),
                            "action": action,
                            "protocol": ACTION_TO_PROTOCOL[action],
                            "joint_rad": [
                                round(float(value), 6) for value in event_joints
                            ],
                            "replay_elapsed_s": round(time.monotonic() - replay_start, 6),
                            "epoch_s": round(time.time(), 6),
                        }
                        events.append(event)
                        print(
                            f"\nRecorded gripper {action} at "
                            f"trajectory_time={trajectory_time:.3f}s"
                        )
                elif key in ("s", "q", "\x03"):
                    print("\nSTOP requested; saving timeline.")
                    stopped = True
                    break

                send_target(
                    interface,
                    piper,
                    target,
                    speed=args.speed,
                    have_gripper=False,
                )
                actual = current_joints(piper)
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
                    if not paused:
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
                        f"events={len(events)} error={error:.6f} rad"
                    )
                    next_print_ns = now_ns + 500_000_000

            if not stopped:
                try:
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
                except SystemExit as exc:
                    print(
                        "\nWARNING: final convergence check did not pass; "
                        "gripper timeline will still be saved."
                    )
                    print(str(exc))
    finally:
        restore_terminal(old_terminal)
        disconnect = getattr(interface, "DisconnectPort", None)
        if callable(disconnect):
            disconnect()

    save_timeline(
        output,
        row=args.row,
        col=args.col,
        trajectory_file=trajectory_file,
        trajectory_duration_s=duration,
        events=events,
        metadata={
            "speed": args.speed,
            "play_speed": args.play_speed,
            "stream_dt": args.stream_dt,
            "start_home": args.start_point or "zero_joint_home",
        },
    )
    print(f"Saved gripper timeline: {output}")
    print(json.dumps(events, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
