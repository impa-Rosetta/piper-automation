#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按程序定义的 home/start 姿态移动 Piper。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from teach.play_trajectory import enable_official, import_official_piper


def load_home(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"home file not found: {path}. Run teach/set_home.py first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if "joint" not in data or len(data["joint"]) != 6:
        raise SystemExit(f"invalid home file: {path}")
    return data



def current_joint(piper: Any) -> list[float]:
    """读取当前六轴关节角，单位 rad。"""
    return [float(v) for v in piper.get_joint_states()[0]]


def joint_error(home_joint: list[float], now_joint: list[float]) -> list[float]:
    """计算当前关节与 home 的误差，单位 rad。"""
    return [float(a) - float(b) for a, b in zip(now_joint, home_joint)]


def joint_error_norm(error: list[float]) -> float:
    """最大绝对关节误差，单位 rad。"""
    return max(abs(v) for v in error)


def send_joint_target(
    piper: Any,
    interface: Any,
    target: list[float],
    *,
    speed: int,
    timeout: float,
    tolerance: float,
    settle: float,
    gripper: float | None,
    no_gripper: bool,
    label: str,
) -> None:
    """持续发送一个关节目标，直到到位或超时。"""
    print(f"stage: {label}")
    start = time.time()
    reached_at = None
    last_error = None
    while True:
        interface.ModeCtrl(0x01, 0x01, speed, 0x00)
        piper.move_j(target, speed)
        if not no_gripper and gripper is not None:
            piper.move_gripper(gripper, 1)

        now_joint = current_joint(piper)
        err = joint_error(target, now_joint)
        last_error = err
        err_norm = joint_error_norm(err)
        if err_norm <= tolerance and reached_at is None:
            reached_at = time.time()
            print(f"  reached tolerance: max_error={err_norm:.6f} rad")
        if reached_at is not None and time.time() >= reached_at + settle:
            break
        if time.time() - start > timeout:
            print(f"  warning: {label} timeout before tolerance reached")
            print("  current_joint(rad)=", now_joint)
            print("  error(rad)=", last_error)
            break
        time.sleep(0.005)


def staged_home_targets(current: list[float], home: list[float], safe_j5: float) -> list[tuple[str, list[float]]]:
    """生成安全回 home 阶段。

    阶段 1：只调整 J5，让夹爪和后臂尽量成一直线。
    阶段 2：保持 safe_j5，让 J1/J2/J3/J4/J6 回 home。
    阶段 3：最后让 J5 回 home。
    """
    stage1 = list(current)
    stage1[4] = safe_j5

    stage2 = list(home)
    stage2[4] = safe_j5

    stage3 = list(home)
    return [
        ("1/3 align J5 to safe angle", stage1),
        ("2/3 move other joints home while holding safe J5", stage2),
        ("3/3 restore final home including J5", stage3),
    ]

def main() -> int:
    parser = argparse.ArgumentParser(description="Move Piper to saved teach home/start pose.")
    parser.add_argument("--can-port", default="can0")
    parser.add_argument("--home", default="teach/home.json")
    parser.add_argument("--speed", type=int, default=20)
    parser.add_argument("--hold", type=float, default=2.0, help="Minimum command hold time in seconds.")
    parser.add_argument("--timeout", type=float, default=8.0, help="Maximum time allowed for reaching home.")
    parser.add_argument("--tolerance", type=float, default=0.01, help="Joint max error tolerance in rad. 0.01 rad ~= 0.57 deg.")
    parser.add_argument("--settle", type=float, default=0.5, help="Extra hold time after reaching tolerance.")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--no-gripper", action="store_true")
    parser.add_argument("--staged", action="store_true", help="Use staged safe home: J5 safe angle first, other joints, then final J5.")
    parser.add_argument("--safe-j5", type=float, default=None, help="Safe J5 angle in rad. If omitted, uses home J5.")
    args = parser.parse_args()

    if not (1 <= args.speed <= 30):
        raise SystemExit("--speed must be 1..30 for home motion")

    home = load_home(Path(args.home))
    print("home joint(rad)=", home["joint"])
    print("home gripper=", home.get("gripper"))
    if not args.yes:
        input("Press Enter to enable Piper and move to saved home/start pose, or Ctrl+C to abort.")

    _piper_sdk, Piper = import_official_piper()
    piper = Piper(args.can_port)
    interface = piper.init()
    piper.connect()
    time.sleep(0.1)

    enable_official(interface, piper, not args.no_gripper, args.speed)
    print(f"moving to home with speed={args.speed}, tolerance={args.tolerance} rad ...")
    gripper = None if args.no_gripper else home.get("gripper")
    if args.staged:
        safe_j5 = home["joint"][4] if args.safe_j5 is None else args.safe_j5
        print(f"using staged home, safe_j5={safe_j5:.6f} rad")
        start_current = current_joint(piper)
        for label, target in staged_home_targets(start_current, home["joint"], safe_j5):
            send_joint_target(
                piper,
                interface,
                target,
                speed=args.speed,
                timeout=args.timeout,
                tolerance=args.tolerance,
                settle=args.settle,
                gripper=gripper,
                no_gripper=args.no_gripper,
                label=label,
            )
    else:
        send_joint_target(
            piper,
            interface,
            home["joint"],
            speed=args.speed,
            timeout=args.timeout,
            tolerance=args.tolerance,
            settle=args.settle,
            gripper=gripper,
            no_gripper=args.no_gripper,
            label="direct home",
        )
    print("home command complete")
    disconnect = getattr(interface, "DisconnectPort", None)
    if callable(disconnect):
        disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


