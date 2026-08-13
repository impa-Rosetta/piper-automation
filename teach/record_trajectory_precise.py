#!/usr/bin/env python3
"""Record Piper feedback using a monotonic deadline-based clock."""

from __future__ import annotations

import argparse
import csv
import json
import os
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

from teach.record_trajectory import (
    get_position,
    import_official_piper,
    print_sdk_info,
    wait_for_teach_mode,
)


def read_key() -> str | None:
    fd = sys.stdin.fileno()
    ready, _, _ = select.select([fd], [], [], 0)
    if not ready:
        return None
    raw = os.read(fd, 1)
    return raw.decode(errors="ignore").lower() if raw else None


def enter_raw_terminal() -> Any | None:
    if sys.stdin.isatty() and termios is not None and tty is not None:
        old = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        return old
    return None


def restore_terminal(old: Any | None) -> None:
    if old is not None and termios is not None:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)


def load_return_target(path: Path) -> list[float]:
    if not path.exists():
        raise SystemExit(
            f"return point not found: {path}. Record feeder-above in the GUI first."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    joints = data.get("joint")
    if not isinstance(joints, list) or len(joints) != 6:
        raise SystemExit(f"invalid return point: {path}")
    return [float(value) for value in joints]


def current_joints(piper: Any) -> list[float]:
    return [float(value) for value in piper.get_joint_states()[0]]


def max_joint_error(target: list[float], actual: list[float]) -> float:
    return max(abs(target_value - actual_value) for target_value, actual_value in zip(target, actual))


def switch_teach_to_can(
    interface: Any,
    piper: Any,
    *,
    speed: int,
    timeout: float,
) -> None:
    """Use official SDK commands to stop teach recording and enter CAN control."""
    print("\nINFO: H pressed; ending manual teach and switching to CAN control ...")
    interface.MotionCtrl_1(0x00, 0x00, 0x02)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        interface.MotionCtrl_2(0x01, 0x01, speed, 0x00)
        if piper.enable_arm():
            status = interface.GetArmStatus().arm_status
            if int(status.ctrl_mode) == 1:
                print("INFO: CAN control enabled; recording automatic return.")
                return
        time.sleep(0.02)
    raise RuntimeError("failed to switch from teach mode to CAN control")


def main() -> int:
    parser = argparse.ArgumentParser(description="Precise Piper trajectory recorder.")
    parser.add_argument("--can-port", default="can0")
    parser.add_argument("--output", default="teach/trajectory.csv")
    parser.add_argument(
        "--timestamps-output",
        default=None,
        help=(
            "Optional detailed timestamp CSV. By default it is written next "
            "to --output with suffix .timestamps.csv."
        ),
    )
    parser.add_argument("--record-time", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--sample-dt", type=float, default=0.01)
    parser.add_argument("--no-gripper", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-sdk", action="store_true")
    parser.add_argument("--home", default="teach/home.json")
    parser.add_argument("--home-speed", type=int, default=10)
    parser.add_argument("--home-tolerance", type=float, default=0.01)
    parser.add_argument("--home-timeout", type=float, default=15.0)
    parser.add_argument("--staged-home", action="store_true")
    parser.add_argument("--safe-j5", type=float, default=None)
    parser.add_argument("--no-auto-home", action="store_true")
    parser.add_argument(
        "--return-point",
        default=None,
        help="Saved joint point used by the H hotkey, for example teach/feeder_above.json.",
    )
    parser.add_argument("--return-key", default="h")
    parser.add_argument("--return-speed", type=int, default=30)
    parser.add_argument("--return-timeout", type=float, default=30.0)
    parser.add_argument("--return-tolerance", type=float, default=0.01)
    parser.add_argument("--return-settled-frames", type=int, default=20)
    args = parser.parse_args()

    if args.sample_dt <= 0:
        parser.error("--sample-dt must be positive")
    if len(args.return_key) != 1:
        parser.error("--return-key must be one character")
    if not 1 <= args.return_speed <= 30:
        parser.error("--return-speed must be 1..30")
    if args.return_timeout <= 0 or args.return_tolerance <= 0:
        parser.error("--return-timeout and --return-tolerance must be positive")
    if args.return_settled_frames < 1:
        parser.error("--return-settled-frames must be >= 1")
    return_target = (
        load_return_target(Path(args.return_point)) if args.return_point else None
    )
    output = Path(args.output)
    if output.exists() and not args.overwrite:
        raise SystemExit(f"trajectory file exists: {output}. Add --overwrite.")
    output.parent.mkdir(parents=True, exist_ok=True)
    timestamps_output = (
        Path(args.timestamps_output)
        if args.timestamps_output
        else output.with_suffix(output.suffix + ".timestamps.csv")
    )
    timestamps_output.parent.mkdir(parents=True, exist_ok=True)
    if timestamps_output.exists() and not args.overwrite:
        raise SystemExit(
            f"timestamp file exists: {timestamps_output}. Add --overwrite."
        )

    have_gripper = not args.no_gripper
    if not args.no_auto_home:
        from teach.go_home import main as go_home_main

        input(
            "Ensure the Teach light is OFF, clear the workspace, then press "
            "Enter to move to the unified Home."
        )
        old_argv = sys.argv
        try:
            sys.argv = [
                "go_home.py",
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
                sys.argv.append("--no-gripper")
            if args.staged_home:
                sys.argv.append("--staged")
            if args.safe_j5 is not None:
                sys.argv.extend(["--safe-j5", str(args.safe_j5)])
            go_home_main()
        finally:
            sys.argv = old_argv

    piper_sdk, Piper = import_official_piper()
    piper = Piper(args.can_port)
    interface = piper.init()
    piper.connect()
    time.sleep(0.1)
    if args.print_sdk:
        print_sdk_info(piper_sdk, piper, interface)

    wait_for_teach_mode(interface, args.timeout)
    input("step 2: Move arm to trajectory start, then press Enter to record.")

    period_ns = max(1, round(args.sample_dt * 1_000_000_000))
    start_ns = time.monotonic_ns()
    previous_ns = start_ns
    deadline_ns = start_ns + period_ns
    stop_ns = (
        start_ns + round(args.record_time * 1_000_000_000)
        if args.record_time > 0
        else None
    )
    missed = 0
    max_late_ns = 0
    rows = 1
    position = get_position(piper, have_gripper)
    next_progress_ns = start_ns + 500_000_000
    returning = False
    return_requested = False
    return_completed = False
    return_deadline = 0.0
    return_settled = 0
    recording_error: str | None = None

    print(f"INFO: Recording {output} with requested dt={args.sample_dt:.6f}s")
    if return_target is not None:
        print(
            f"Controls: press {args.return_key.upper()} once to record an automatic "
            "return to feeder-above and save; Ctrl+C saves without returning.",
            flush=True,
        )
    old_terminal = enter_raw_terminal()
    if return_target is not None:
        if old_terminal is None:
            raise SystemExit(
                "ERROR: H hotkey needs an interactive TTY. Start recording from "
                "the field workstation's independent terminal."
            )
        print(
            f"HOTKEY READY: press {args.return_key.upper()} once; no Enter required.",
            flush=True,
        )
    try:
        with (
            output.open("w", newline="", encoding="utf-8") as stream,
            timestamps_output.open(
                "w", newline="", encoding="utf-8"
            ) as timestamp_stream,
        ):
            writer = csv.writer(stream)
            timestamp_writer = csv.writer(timestamp_stream)
            timestamp_writer.writerow(
                [
                    "sample_index",
                    "epoch_ns",
                    "monotonic_ns",
                    "elapsed_s",
                    "delta_s",
                    *[f"joint_{index}_rad" for index in range(1, 7)],
                    *(["gripper"] if have_gripper else []),
                ]
            )
            writer.writerow([0.0, *position])
            timestamp_writer.writerow(
                [0, time.time_ns(), start_ns, "0.000000000", "0.000000000", *position]
            )
            while stop_ns is None or time.monotonic_ns() < stop_ns:
                key = read_key()
                if key == "\x03":
                    raise KeyboardInterrupt
                if (
                    return_target is not None
                    and key == args.return_key.lower()
                    and not returning
                ):
                    return_requested = True
                    print(
                        f"\nHOTKEY RECEIVED: {args.return_key.upper()}",
                        flush=True,
                    )
                    switch_teach_to_can(
                        interface,
                        piper,
                        speed=args.return_speed,
                        timeout=min(8.0, args.return_timeout),
                    )
                    returning = True
                    return_deadline = time.monotonic() + args.return_timeout
                    return_settled = 0
                    reset_ns = time.monotonic_ns()
                    previous_ns = reset_ns
                    deadline_ns = reset_ns + period_ns
                    continue

                remaining_ns = deadline_ns - time.monotonic_ns()
                if remaining_ns > 0:
                    time.sleep(remaining_ns / 1_000_000_000)
                sample_ns = time.monotonic_ns()
                late_ns = max(0, sample_ns - deadline_ns)
                max_late_ns = max(max_late_ns, late_ns)
                skipped = late_ns // period_ns
                missed += skipped
                deadline_ns += (skipped + 1) * period_ns

                arm_status = interface.GetArmStatus().arm_status
                ctrl_mode = int(arm_status.ctrl_mode)
                if returning:
                    if ctrl_mode != 1:
                        raise RuntimeError(
                            f"CAN control was lost during automatic return: ctrl_mode={ctrl_mode}"
                        )
                    if int(arm_status.arm_status) != 0:
                        raise RuntimeError(
                            f"arm_status={int(arm_status.arm_status)} during automatic return"
                        )
                    interface.MotionCtrl_2(
                        0x01, 0x01, args.return_speed, 0x00
                    )
                    piper.move_j(return_target, args.return_speed)
                elif ctrl_mode != 2:
                    print("INFO: Teach mode exited; stopping.")
                    break

                position = get_position(piper, have_gripper)
                delta_s = (sample_ns - previous_ns) / 1_000_000_000
                writer.writerow([f"{delta_s:.9f}", *position])
                timestamp_writer.writerow(
                    [
                        rows,
                        time.time_ns(),
                        sample_ns,
                        f"{(sample_ns - start_ns) / 1_000_000_000:.9f}",
                        f"{delta_s:.9f}",
                        *position,
                    ]
                )
                previous_ns = sample_ns
                rows += 1

                if returning:
                    error = max_joint_error(
                        return_target,
                        [float(value) for value in position[:6]],
                    )
                    return_settled = (
                        return_settled + 1
                        if error <= args.return_tolerance
                        else 0
                    )
                    if sample_ns >= next_progress_ns:
                        print(
                            f"INFO: AUTO RETURN max_error={error:.6f} rad "
                            f"settled={return_settled}/{args.return_settled_frames}"
                        )
                    if return_settled >= args.return_settled_frames:
                        return_completed = True
                        print(
                            "INFO: Feeder-above reached; trajectory tail recorded. "
                            "Saving automatically."
                        )
                        break
                    if time.monotonic() >= return_deadline:
                        raise RuntimeError(
                            "automatic return timeout before reaching tolerance"
                        )

                if sample_ns >= next_progress_ns:
                    elapsed = (sample_ns - start_ns) / 1_000_000_000
                    print(f"INFO: t={elapsed:.2f}s rows={rows} pos={position}")
                    next_progress_ns = sample_ns + 500_000_000
    except KeyboardInterrupt:
        print("\nINFO: Ctrl+C received; trajectory has been saved.")
    except RuntimeError as exc:
        recording_error = str(exc)
        print(f"\nERROR: {recording_error}")
        print("INFO: Existing trajectory samples will still be saved.")
        try:
            interface.MotionCtrl_1(0x01, 0x00, 0x00)
        except Exception as stop_exc:  # noqa: BLE001
            print(f"WARNING: emergency stop command failed: {stop_exc}")
    except Exception:
        restore_terminal(old_terminal)
        raise
    restore_terminal(old_terminal)

    finish_ns = time.monotonic_ns()
    metadata = {
        "format": "piper_official_delta_time_csv",
        "clock": "time.monotonic_ns",
        "requested_sample_dt_s": args.sample_dt,
        "rows": rows,
        "duration_s": round((finish_ns - start_ns) / 1_000_000_000, 6),
        "missed_deadlines": int(missed),
        "max_lateness_s": round(max_late_ns / 1_000_000_000, 6),
        "final_position": list(position),
        "timestamp_samples": str(timestamps_output),
        "automatic_return_requested": return_requested,
        "automatic_return_completed": return_completed,
        "return_point": args.return_point,
        "error": recording_error,
    }
    metadata_path = output.with_suffix(output.suffix + ".meta.json")
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"INFO: Recording complete: rows={rows}, file={output}")
    print(f"INFO: Timestamp samples: {timestamps_output}")
    print(f"INFO: Timing report: {metadata_path}")
    print(json.dumps(metadata, indent=2, ensure_ascii=False))
    print("step 3: If the teach light is on, short-press it to exit.")
    disconnect = getattr(interface, "DisconnectPort", None)
    if callable(disconnect):
        disconnect()
    return 1 if recording_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
