#!/usr/bin/env python3
"""Replay a position trajectory with interpolation and feedback convergence."""

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
except ImportError:  # Windows can still parse --help and run check-only.
    termios = None
    tty = None

from teach.play_trajectory import (
    enable_official,
    ensure_can_mode,
    import_official_piper,
    print_sdk_info,
)
from teach.trajectory_math import (
    TrajectorySample,
    joint_error,
    max_abs_joint_error,
    rows_to_samples,
    sample_linear,
)

JOINT_LIMITS_RAD = (
    (-2.6179, 2.6179),
    (-0.0349066, 3.14),
    (-2.967, 0.0349066),
    (-1.745, 1.745),
    (-1.22, 1.22),
    (-2.09439, 2.09439),
)

def load_samples(path: Path, have_gripper: bool) -> list[TrajectorySample]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            rows = [
                [float(value) for value in row]
                for row in csv.reader(stream)
                if row
            ]
    except FileNotFoundError as exc:
        raise SystemExit(f"ERROR: Trajectory file not found: {path}") from exc
    try:
        return rows_to_samples(rows, have_gripper=have_gripper)
    except ValueError as exc:
        raise SystemExit(f"ERROR: Invalid trajectory {path}: {exc}") from exc


def validate_joint_limits(
    samples: list[TrajectorySample],
    margin_rad: float = 0.001,
) -> None:
    """Reject teach poses that cannot be sent through official JointCtrl."""
    violations = []
    for joint_index, (lower, upper) in enumerate(JOINT_LIMITS_RAD):
        values = [sample.joints[joint_index] for sample in samples]
        first = next(
            (
                (row_index, sample)
                for row_index, sample in enumerate(samples, start=1)
                if sample.joints[joint_index] < lower - margin_rad
                or sample.joints[joint_index] > upper + margin_rad
            ),
            None,
        )
        if first is not None:
            row_index, sample = first
            violations.append(
                f"  J{joint_index + 1}: recorded "
                f"[{min(values):.6f}, {max(values):.6f}] rad; "
                f"allowed [{lower:.6f}, {upper:.6f}] rad; "
                f"check margin {margin_rad:.6f} rad; "
                f"first invalid row/time {row_index}/"
                f"{sample.time_from_start:.3f}s"
            )
    if violations:
        raise SystemExit(
            "ERROR: Trajectory exceeds official JointCtrl limits:\n"
            + "\n".join(violations)
            + "\nRe-record with every joint inside its command range. "
            "Automatic clipping is disabled because it would change the "
            "taught gripper pose."
        )

def current_joints(piper: Any) -> tuple[float, ...]:
    return tuple(float(value) for value in piper.get_joint_states()[0])


def send_target(
    interface: Any,
    piper: Any,
    sample: TrajectorySample,
    *,
    speed: int,
    have_gripper: bool,
) -> None:
    # The official MoveJ demo refreshes mode and joint targets every cycle.
    interface.ModeCtrl(0x01, 0x01, speed, 0x00)
    piper.move_j(list(sample.joints), speed)
    if have_gripper and sample.gripper is not None:
        piper.move_gripper(sample.gripper, 1)


def write_telemetry(
    args: argparse.Namespace,
    *,
    now_ns: int,
    replay_index: int,
    phase: str,
    trajectory_time: float,
    target: TrajectorySample,
    actual: tuple[float, ...],
    paused: bool = False,
    tracking_hold: bool = False,
) -> None:
    writer = getattr(args, "_telemetry_writer", None)
    if writer is None:
        return
    errors = joint_error(target.joints, actual)
    writer.writerow(
        [
            time.time_ns(),
            now_ns,
            replay_index,
            phase,
            f"{trajectory_time:.9f}",
            *[f"{value:.9f}" for value in target.joints],
            *[f"{value:.9f}" for value in actual],
            *[f"{value:.9f}" for value in errors],
            f"{max(abs(value) for value in errors):.9f}",
            int(paused),
            int(tracking_hold),
        ]
    )


def wait_next(deadline_ns: int, period_ns: int) -> tuple[int, int, int]:
    remaining_ns = deadline_ns - time.monotonic_ns()
    if remaining_ns > 0:
        time.sleep(remaining_ns / 1_000_000_000)
    now_ns = time.monotonic_ns()
    lateness_ns = max(0, now_ns - deadline_ns)
    if lateness_ns > period_ns * 4:
        return now_ns, now_ns + period_ns, 1
    return now_ns, deadline_ns + period_ns, int(lateness_ns >= period_ns)


def converge(
    interface: Any,
    piper: Any,
    sample: TrajectorySample,
    *,
    label: str,
    speed: int,
    have_gripper: bool,
    tolerance: float,
    timeout: float,
    settle: float,
    stream_dt: float,
    maximum_initial_error: float | None = None,
    telemetry_args: argparse.Namespace | None = None,
    telemetry_phase: str = "converge",
    replay_index: int = 0,
) -> float:
    actual = current_joints(piper)
    initial_error = max_abs_joint_error(sample.joints, actual)
    print(f"INFO: {label} initial max error={initial_error:.6f} rad")
    if (
        maximum_initial_error is not None
        and initial_error > maximum_initial_error
    ):
        raise SystemExit(
            f"ERROR: {label} error {initial_error:.6f} rad exceeds "
            f"safety limit {maximum_initial_error:.6f} rad. "
            "Move to the program-defined home first."
        )

    period_ns = max(1, round(stream_dt * 1_000_000_000))
    deadline_ns = time.monotonic_ns()
    end_ns = deadline_ns + round(timeout * 1_000_000_000)
    settled_since_ns: int | None = None
    next_print_ns = deadline_ns
    latest_error = initial_error
    while time.monotonic_ns() < end_ns:
        now_ns, deadline_ns, _ = wait_next(deadline_ns, period_ns)
        send_target(
            interface,
            piper,
            sample,
            speed=speed,
            have_gripper=have_gripper,
        )
        actual = current_joints(piper)
        latest_error = max_abs_joint_error(sample.joints, actual)
        if telemetry_args is not None:
            write_telemetry(
                telemetry_args,
                now_ns=now_ns,
                replay_index=replay_index,
                phase=telemetry_phase,
                trajectory_time=sample.time_from_start,
                target=sample,
                actual=actual,
            )
        if latest_error <= tolerance:
            if settled_since_ns is None:
                settled_since_ns = now_ns
            if now_ns - settled_since_ns >= round(settle * 1_000_000_000):
                print(
                    f"INFO: {label} converged: max_error={latest_error:.6f} rad"
                )
                return latest_error
        else:
            settled_since_ns = None
        if now_ns >= next_print_ns:
            print(f"INFO: {label} max_error={latest_error:.6f} rad")
            next_print_ns = now_ns + 500_000_000
    raise SystemExit(
        f"ERROR: {label} did not converge within {timeout:.2f}s; "
        f"last max error={latest_error:.6f} rad"
    )


def read_key() -> str | None:
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    return sys.stdin.read(1).lower() if ready else None


def replay_once(
    interface: Any,
    piper: Any,
    samples: list[TrajectorySample],
    args: argparse.Namespace,
    have_gripper: bool,
) -> dict[str, float | int | bool]:
    period_ns = max(1, round(args.stream_dt * 1_000_000_000))
    deadline_ns = time.monotonic_ns()
    trajectory_time = 0.0
    duration = samples[-1].time_from_start
    target = samples[0]
    paused = False
    stopped = False
    recovery_since_ns: int | None = None
    missed = 0
    max_lateness_ns = 0
    max_tracking_error = 0.0
    next_print_ns = deadline_ns

    while trajectory_time < duration:
        now_ns, next_deadline_ns, missed_one = wait_next(deadline_ns, period_ns)
        max_lateness_ns = max(max_lateness_ns, max(0, now_ns - deadline_ns))
        missed += missed_one
        deadline_ns = next_deadline_ns

        key = read_key()
        if key == "p":
            paused = True
            print("\nPAUSED. Press c to continue, s/q to stop.")
        elif key == "c":
            paused = False
            recovery_since_ns = None
            print("\nCONTINUE")
        elif key in ("s", "q", "\x03"):
            print("\nSTOP requested")
            stopped = True
            break

        commanded_target = target
        commanded_trajectory_time = trajectory_time
        send_target(
            interface,
            piper,
            commanded_target,
            speed=args.speed,
            have_gripper=have_gripper,
        )
        actual = current_joints(piper)
        error = max_abs_joint_error(commanded_target.joints, actual)
        max_tracking_error = max(max_tracking_error, error)

        tracking_blocked = (
            args.tracking_error_limit > 0
            and error > args.tracking_error_limit * 2.5
        )
        if tracking_blocked:
            if recovery_since_ns is None:
                recovery_since_ns = now_ns
                print(
                    f"\nTRACKING HOLD: error={error:.6f} rad exceeds "
                    f"{args.tracking_error_limit * 2.5:.6f} rad"
                )
            elif (
                now_ns - recovery_since_ns
                > round(args.tracking_timeout * 1_000_000_000)
            ):
                raise SystemExit(
                    "ERROR: Tracking error did not recover; playback stopped."
                )
        else:
            recovery_since_ns = None
            if not paused:
                trajectory_time = min(
                    duration,
                    trajectory_time + args.stream_dt * args.play_speed,
                )
                target = sample_linear(samples, trajectory_time)

        write_telemetry(
            args,
            now_ns=now_ns,
            replay_index=getattr(args, "_replay_index", 0),
            phase="replay",
            trajectory_time=commanded_trajectory_time,
            target=commanded_target,
            actual=actual,
            paused=paused,
            tracking_hold=tracking_blocked,
        )

        if now_ns >= next_print_ns:
            print(
                f"INFO: trajectory={trajectory_time:.3f}/{duration:.3f}s "
                f"tracking_error={error:.6f} rad"
            )
            next_print_ns = now_ns + round(1_000_000_000 / args.progress_hz)

    return {
        "stopped": stopped,
        "missed_deadlines": missed,
        "max_lateness_s": max_lateness_ns / 1_000_000_000,
        "max_tracking_error_rad": max_tracking_error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Precise Piper trajectory player.")
    parser.add_argument("--can-port", default="can0")
    parser.add_argument("--input", default="teach/trajectory.csv")
    parser.add_argument("--speed", type=int, default=20)
    parser.add_argument("--play-speed", type=float, default=1.0)
    parser.add_argument("--times", type=int, default=1)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--no-gripper", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--stream-dt", type=float, default=0.005)
    parser.add_argument("--print-sdk", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--start-tolerance", type=float, default=0.008)
    parser.add_argument("--start-timeout", type=float, default=8.0)
    parser.add_argument("--max-start-error", type=float, default=0.20)
    parser.add_argument("--final-tolerance", type=float, default=0.006)
    parser.add_argument("--final-timeout", type=float, default=5.0)
    parser.add_argument("--settle", type=float, default=0.30)
    parser.add_argument("--tracking-error-limit", type=float, default=0.12)
    parser.add_argument("--tracking-timeout", type=float, default=2.0)
    parser.add_argument("--progress-hz", type=float, default=2.0)
    parser.add_argument("--report", default=None)
    parser.add_argument("--home", default="teach/home.json")
    parser.add_argument("--home-speed", type=int, default=10)
    parser.add_argument("--home-tolerance", type=float, default=0.01)
    parser.add_argument("--home-timeout", type=float, default=15.0)
    parser.add_argument("--staged-home", action="store_true")
    parser.add_argument("--safe-j5", type=float, default=None)
    parser.add_argument(
        "--zero-home",
        action="store_true",
        help="Move to all-zero joint Home before replay instead of teach/home.json.",
    )
    parser.add_argument(
        "--zero-home-tolerance-deg",
        type=float,
        default=1.0,
        help="Tolerance used by --zero-home.",
    )
    parser.add_argument("--no-auto-home", action="store_true")
    parser.add_argument(
        "--telemetry",
        default=None,
        help=(
            "Optional CSV containing timestamped target and actual joint "
            "states for every replay control cycle."
        ),
    )
    args = parser.parse_args()

    if not 1 <= args.speed <= 100:
        parser.error("--speed must be 1..100")
    if args.play_speed <= 0 or args.stream_dt <= 0:
        parser.error("--play-speed and --stream-dt must be positive")
    if args.progress_hz <= 0:
        parser.error("--progress-hz must be positive")
    if args.speed > 20 and not args.yes:
        raise SystemExit("For speed above 20, add --yes after a guarded test.")

    have_gripper = not args.no_gripper
    path = Path(args.input)
    samples = load_samples(path, have_gripper)
    duration = samples[-1].time_from_start
    print(
        f"INFO: loaded {len(samples)} samples, duration={duration:.6f}s, "
        f"output_dt={args.stream_dt:.6f}s"
    )

    if args.check_only:
        print(
            "INFO: trajectory format check passed; nominal PC-side joint "
            "limit check is disabled; no CAN command sent."
        )
        return 0

    if not args.yes:
        input(
            "Press Enter to move to the unified Home and replay. "
            "During replay: p=pause, c=continue, s/q=stop."
        )

    if not args.no_auto_home:
        if args.zero_home:
            print("INFO: Moving to zero Home: all six joints = 0 deg")
            module_name = "go_zero_home.py"
            from teach.go_zero_home import main as home_main

            home_argv = [
                module_name,
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
        else:
            print(f"INFO: Moving to unified Home: {args.home}")
            module_name = "go_home.py"
            from teach.go_home import main as home_main

            home_argv = [
                module_name,
                "--can-port",
                args.can_port,
                "--home",
                args.home,
                "--speed",
                str(args.home_speed),
                "--tolerance",
                str(args.home_tolerance),
                "--timeout",
                str(args.home_timeout),
                "--yes",
            ]
            if args.no_gripper:
                home_argv.append("--no-gripper")
            if args.staged_home:
                home_argv.append("--staged")
            if args.safe_j5 is not None:
                home_argv.extend(["--safe-j5", str(args.safe_j5)])
        old_argv = sys.argv
        try:
            sys.argv = home_argv
            home_main()
        finally:
            sys.argv = old_argv

    piper_sdk, Piper = import_official_piper()
    piper = Piper(args.can_port)
    interface = piper.init()
    piper.connect()
    time.sleep(0.1)
    if args.print_sdk:
        print_sdk_info(piper_sdk, piper, interface)
    ensure_can_mode(interface, piper, have_gripper, args.speed, args.timeout)
    enable_official(interface, piper, have_gripper, args.speed)

    old_terminal = None
    if sys.stdin.isatty() and termios is not None and tty is not None:
        old_terminal = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())

    reports: list[dict[str, float | int | bool]] = []
    telemetry_stream = None
    if args.telemetry:
        telemetry_path = Path(args.telemetry)
        telemetry_path.parent.mkdir(parents=True, exist_ok=True)
        telemetry_stream = telemetry_path.open(
            "w", newline="", encoding="utf-8", buffering=1
        )
        telemetry_writer = csv.writer(telemetry_stream)
        telemetry_writer.writerow(
            [
                "epoch_ns",
                "monotonic_ns",
                "replay_index",
                "phase",
                "trajectory_time_s",
                *[f"target_j{i}_rad" for i in range(1, 7)],
                *[f"actual_j{i}_rad" for i in range(1, 7)],
                *[f"error_j{i}_rad" for i in range(1, 7)],
                "max_abs_error_rad",
                "paused",
                "tracking_hold",
            ]
        )
        args._telemetry_writer = telemetry_writer
        print(f"INFO: Replay telemetry: {telemetry_path}")
    try:
        count = 0
        while args.times == 0 or count < args.times:
            converge(
                interface,
                piper,
                samples[0],
                label="trajectory start",
                speed=args.speed,
                have_gripper=have_gripper,
                tolerance=args.start_tolerance,
                timeout=args.start_timeout,
                settle=args.settle,
                stream_dt=args.stream_dt,
                maximum_initial_error=args.max_start_error,
                telemetry_args=args,
                telemetry_phase="start_align",
                replay_index=count,
            )
            args._replay_index = count
            report = replay_once(interface, piper, samples, args, have_gripper)
            reports.append(report)
            if report["stopped"]:
                break
            converge(
                interface,
                piper,
                samples[-1],
                label="trajectory final",
                speed=args.speed,
                have_gripper=have_gripper,
                tolerance=args.final_tolerance,
                timeout=args.final_timeout,
                settle=args.settle,
                stream_dt=args.stream_dt,
                telemetry_args=args,
                telemetry_phase="final_settle",
                replay_index=count,
            )
            count += 1
            if args.interval > 0:
                time.sleep(args.interval)
    finally:
        if old_terminal is not None:
            termios.tcsetattr(
                sys.stdin.fileno(),
                termios.TCSADRAIN,
                old_terminal,
            )
        if telemetry_stream is not None:
            telemetry_stream.close()

    report_path = (
        Path(args.report)
        if args.report
        else path.with_suffix(path.suffix + ".replay.json")
    )
    report_path.write_text(
        json.dumps(reports, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"INFO: Replay complete. Report: {report_path}")
    print(json.dumps(reports, indent=2, ensure_ascii=False))
    disconnect = getattr(interface, "DisconnectPort", None)
    if callable(disconnect):
        disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
