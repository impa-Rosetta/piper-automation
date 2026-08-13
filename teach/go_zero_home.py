#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Move Piper to the program-defined zero Home: all six joints at 0 degree."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from teach.armrobot_waypoint_recorder import read_feedback
from teach.can_utils import require_can_interface


ZERO_JOINTS_DEG = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def enable_arm(interface: Any, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if interface.EnablePiper():
            print("Piper enable OK.")
            return
        time.sleep(0.02)
    raise TimeoutError("EnablePiper timeout")


def emergency_stop(interface: Any) -> None:
    try:
        interface.MotionCtrl_1(0x01, 0x00, 0x00)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: emergency stop command failed: {exc}")


def joint_error_deg(actual: tuple[float, ...]) -> list[float]:
    return [target - now for target, now in zip(ZERO_JOINTS_DEG, actual)]


def max_abs(values: list[float]) -> float:
    return max(abs(value) for value in values)


def send_zero_joint_command(interface: Any, speed: int) -> None:
    interface.MotionCtrl_2(0x01, 0x01, speed, 0x00)
    interface.JointCtrl(0, 0, 0, 0, 0, 0)


def feedback_hz(interface: Any) -> tuple[float, float]:
    try:
        joint_hz = float(interface.GetArmJointMsgs().Hz)
    except Exception:  # noqa: BLE001
        joint_hz = 0.0
    try:
        status_hz = float(interface.GetArmStatus().Hz)
    except Exception:  # noqa: BLE001
        status_hz = 0.0
    return joint_hz, status_hz


def save_snapshot(path: Path, interface: Any, reached: bool) -> None:
    feedback = read_feedback(interface)
    data = {
        "format": "piper_zero_home_v1",
        "saved_at": datetime.now().astimezone().isoformat(),
        "target_joints_deg": list(ZERO_JOINTS_DEG),
        "reached": reached,
        "joints_deg": [round(value, 6) for value in feedback.joints_deg],
        "tcp": [round(value, 6) for value in feedback.tcp],
        "ctrl_mode": feedback.ctrl_mode,
        "arm_status": feedback.arm_status,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Saved zero Home snapshot: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Move Piper to Home defined as all six joints at 0 degree using "
            "official MotionCtrl_2 + JointCtrl."
        )
    )
    parser.add_argument("--can-port", default="can0")
    parser.add_argument("--speed", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--command-dt", type=float, default=0.02)
    parser.add_argument(
        "--tolerance-deg",
        type=float,
        default=1.0,
        help="Reached when every joint is within this many degrees.",
    )
    parser.add_argument(
        "--settled-frames",
        type=int,
        default=10,
        help="How many consecutive in-tolerance samples are required.",
    )
    parser.add_argument(
        "--snapshot",
        default="teach/zero_home.json",
        help="Save final feedback here.",
    )
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.speed <= 30:
        raise SystemExit("--speed must be 1..30 for first zero-home tests")
    if args.timeout <= 0 or args.command_dt <= 0:
        raise SystemExit("--timeout and --command-dt must be positive")
    if args.tolerance_deg <= 0:
        raise SystemExit("--tolerance-deg must be positive")
    if args.settled_frames < 1:
        raise SystemExit("--settled-frames must be >= 1")

    print("=" * 72)
    print("Piper zero Home: J1=J2=J3=J4=J5=J6=0 deg")
    print("=" * 72)
    print("This command moves the arm. Clear the workspace and keep E-stop ready.")
    print(f"speed={args.speed}%, tolerance={args.tolerance_deg} deg")
    if not args.yes:
        if input("Type RUN to enable and move to zero Home: ").strip() != "RUN":
            print("Cancelled; no motion command was sent.")
            return 0

    require_can_interface(args.can_port)
    from piper_sdk import C_PiperInterface_V2

    interface = C_PiperInterface_V2(args.can_port)
    interface.ConnectPort()
    time.sleep(0.2)
    reached = False
    settled = 0
    last_print = 0.0
    deadline = time.monotonic() + args.timeout

    exit_code = 0
    try:
        enable_arm(interface)
        while time.monotonic() < deadline:
            send_zero_joint_command(interface, args.speed)
            joint_hz, status_hz = feedback_hz(interface)
            if joint_hz <= 0 or status_hz <= 0:
                raise RuntimeError(
                    "Piper feedback is not alive "
                    f"(joint_hz={joint_hz:.1f}, status_hz={status_hz:.1f}). "
                    "Check can0, power, and USB-CAN connection."
                )
            feedback = read_feedback(interface)
            if feedback.arm_status not in {None, 0}:
                raise RuntimeError(f"arm_status={feedback.arm_status}")
            error = joint_error_deg(feedback.joints_deg)
            error_norm = max_abs(error)
            settled = settled + 1 if error_norm <= args.tolerance_deg else 0
            now = time.monotonic()
            if now - last_print >= 0.5:
                print(
                    "joints_deg="
                    + ", ".join(f"{value:+.2f}" for value in feedback.joints_deg)
                    + f" | max_error={error_norm:.3f} deg"
                )
                last_print = now
            if settled >= args.settled_frames:
                reached = True
                break
            time.sleep(args.command_dt)
    except KeyboardInterrupt:
        print("\nCtrl+C: sending official emergency stop.")
        emergency_stop(interface)
        exit_code = 130
    except (RuntimeError, TimeoutError) as exc:
        print(f"\nERROR: {exc}")
        print("Sending official emergency stop.")
        emergency_stop(interface)
        save_snapshot(Path(args.snapshot), interface, reached=False)
        exit_code = 1

    if exit_code:
        disconnect = getattr(interface, "DisconnectPort", None)
        if callable(disconnect):
            disconnect()
        return exit_code

    if reached:
        print("Zero Home reached.")
    else:
        print("WARNING: timeout before reaching tolerance.")
    save_snapshot(Path(args.snapshot), interface, reached=reached)
    disconnect = getattr(interface, "DisconnectPort", None)
    if callable(disconnect):
        disconnect()
    return 0 if reached else 2


if __name__ == "__main__":
    raise SystemExit(main())
