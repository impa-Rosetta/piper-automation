#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""迁移自 AgileX 官方 recordAndPlayTraj/playTrajectory_en.py。

功能：读取官方示例 CSV 轨迹并通过 piper_sdk 高层 API 回放。

重要：
- 使用 Piper.move_j() / Piper.move_gripper()，不自己发 CAN。
- 使用 interface.ModeCtrl() / EmergencyStop() 复用官方 CAN 模式切换与安全流程。
- 支持键盘输入暂停、继续、停止：p=pause, c=continue, s=stop, q=quit。
"""

from __future__ import annotations

import argparse
import csv
import inspect
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


def import_official_piper() -> tuple[Any, Any]:
    try:
        import piper_sdk  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"import piper_sdk failed: {exc!r}") from exc

    Piper = getattr(piper_sdk, "Piper", None)
    if Piper is None:
        methods = ", ".join(name for name in dir(piper_sdk) if "Piper" in name or "piper" in name)
        raise SystemExit(
            "当前 piper_sdk 没有官方 recordAndPlayTraj 需要的 Piper 类。\n"
            f"piper_sdk file: {getattr(piper_sdk, '__file__', None)}\n"
            f"piper_sdk version: {getattr(piper_sdk, '__version__', 'unknown')}\n"
            f"Piper-related names: {methods or '(none)'}\n"
            "请安装 AgileX 官方 API-enabled piper_sdk，例如官方教程中的 1_0_0_beta 分支。"
        )
    return piper_sdk, Piper


def print_sdk_info(piper_sdk: Any, piper: Any, interface: Any) -> None:
    print("SDK inspection")
    print("--------------")
    print(f"piper_sdk.__file__: {getattr(piper_sdk, '__file__', None)}")
    print(f"piper_sdk.__version__: {getattr(piper_sdk, '__version__', 'unknown')}")
    print(f"Piper class: {type(piper)}")
    print(f"Interface class: {type(interface)}")
    for obj_name, obj, names in (
        ("Piper", piper, ("get_joint_states", "get_gripper_states", "enable_arm", "disable_arm", "enable_gripper", "move_j", "move_gripper")),
        ("Interface", interface, ("GetArmStatus", "ModeCtrl", "EmergencyStop")),
    ):
        print(f"{obj_name} methods:")
        for name in names:
            attr = getattr(obj, name, None)
            if callable(attr):
                try:
                    sig = inspect.signature(attr)
                except Exception:  # noqa: BLE001
                    sig = "(...)"
                print(f"  {name}{sig}")
            else:
                print(f"  {name}: missing")
    print()


def load_track(path: Path) -> list[list[float]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            track = [[float(item) for item in row] for row in csv.reader(f) if row]
    except FileNotFoundError as exc:
        raise SystemExit(f"ERROR: Trajectory file not found: {path}") from exc
    if not track:
        raise SystemExit(f"ERROR: Trajectory file is empty: {path}")
    return track


def get_ctrl_mode(interface: Any) -> int | None:
    try:
        return int(interface.GetArmStatus().arm_status.ctrl_mode)
    except Exception:  # noqa: BLE001
        return None


def get_position(piper: Any, have_gripper: bool) -> tuple[float, ...]:
    joint_state = tuple(float(v) for v in piper.get_joint_states()[0])
    if have_gripper:
        return joint_state + (float(piper.get_gripper_states()[0][0]),)
    return joint_state


def official_stop_to_safe(interface: Any, piper: Any, have_gripper: bool) -> None:
    """官方示例的安全停止流程：急停后等待 2/3/5 关节回安全范围，再 disable。"""
    print("INFO: Switching out of teach mode with official safety stop sequence.")
    interface.EmergencyStop(0x01)
    time.sleep(1.0)
    limit_angle = [0.1745, 0.7854, 0.2094]
    pos = get_position(piper, have_gripper)
    print("INFO: If arm is sagging, gently support joints 2/3/5 toward safe range.")
    while not (abs(pos[1]) < limit_angle[0] and abs(pos[2]) < limit_angle[0] and limit_angle[2] < pos[4] < limit_angle[1]):
        time.sleep(0.01)
        pos = get_position(piper, have_gripper)
    piper.disable_arm()
    time.sleep(1.0)


def ensure_can_mode(interface: Any, piper: Any, have_gripper: bool, speed: int, timeout: float) -> None:
    """确保退出示教并进入 CAN 关节控制模式。"""
    print("step 1: Ensure teach light is off before replay.")
    if get_ctrl_mode(interface) != 1:
        official_stop_to_safe(interface, piper, have_gripper)
        deadline = time.time() + timeout
        while get_ctrl_mode(interface) != 1:
            if time.time() > deadline:
                raise SystemExit("ERROR: CAN mode switch failed. Check teach button light and firmware state.")
            interface.ModeCtrl(0x01, 0x01, speed, 0x00)
            time.sleep(0.01)
    print("INFO: CAN control mode detected (ctrl_mode == 1).")


def enable_official(interface: Any, piper: Any, have_gripper: bool, speed: int) -> None:
    while not piper.enable_arm():
        time.sleep(0.01)
    if have_gripper:
        interface.ModeCtrl(0x01, 0x01, speed, 0x00)
        piper.enable_gripper()
        time.sleep(0.01)
    print("INFO: Enable successful")


def read_key() -> str | None:
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if not ready:
        return None
    return sys.stdin.read(1).lower()


def wait_with_controls(duration: float, paused: dict[str, bool]) -> bool:
    """等待期间支持 p/c/s/q 控制。返回 False 表示停止。"""
    end = time.time() + max(duration, 0.0)
    while time.time() < end or paused["paused"]:
        key = read_key()
        if key == "p":
            paused["paused"] = True
            print("\nPAUSED. Press c to continue, s/q to stop.")
        elif key == "c":
            paused["paused"] = False
            print("\nCONTINUE")
            end = time.time() + max(end - time.time(), 0.0)
        elif key in ("s", "q", "\x03"):
            print("\nSTOP requested")
            return False
        if not paused["paused"]:
            time.sleep(0.005)
        else:
            time.sleep(0.05)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Official-style Piper teach trajectory player.")
    parser.add_argument("--can-port", default="can0")
    parser.add_argument("--input", default="teach/trajectory.csv")
    parser.add_argument("--speed", type=int, default=20, help="机械臂运动速度百分比；首次建议 10~20。")
    parser.add_argument("--play-speed", type=float, default=1.0, help="时间倍率：2=两倍速，0.5=半速。")
    parser.add_argument("--times", type=int, default=1, help="播放次数，0 表示无限循环。")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--no-gripper", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--stream-dt", type=float, default=0.005, help="回放时重复发送当前目标的周期，默认 0.005s。")
    parser.add_argument("--print-sdk", action="store_true")
    args = parser.parse_args()

    if not (1 <= args.speed <= 100):
        raise SystemExit("--speed must be 1..100")
    if args.speed > 20 and not args.yes:
        raise SystemExit("首次回放建议 --speed 10~20。若确认要高速，请加 --yes。")
    if args.play_speed <= 0:
        raise SystemExit("--play-speed must be positive")

    track = load_track(Path(args.input))
    have_gripper = not args.no_gripper
    piper_sdk, Piper = import_official_piper()
    piper = Piper(args.can_port)
    interface = piper.init()
    piper.connect()
    time.sleep(0.1)

    print(f"Connected Piper on {args.can_port}")
    print(f"piper_sdk.__file__: {getattr(piper_sdk, '__file__', None)}")
    print(f"loaded rows={len(track)} from {args.input}")
    if args.print_sdk:
        print_sdk_info(piper_sdk, piper, interface)

    ensure_can_mode(interface, piper, have_gripper, args.speed, args.timeout)
    enable_official(interface, piper, have_gripper, args.speed)

    if not args.yes:
        input("step 2: Press Enter to start trajectory replay. During replay: p=pause, c=continue, s/q=stop.")

    fd = sys.stdin.fileno()
    old = None
    if sys.stdin.isatty() and termios is not None and tty is not None:
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    paused = {"paused": False}
    try:
        count = 0
        while args.times == 0 or count < args.times:
            for n, pos in enumerate(track):
                joints = pos[1:-1] if have_gripper and len(pos) >= 8 else pos[1:7]
                if len(joints) != 6:
                    raise SystemExit(f"ERROR: row {n} does not contain 6 joints: {pos}")
                wait = pos[0] / args.play_speed
                print(f"INFO: replay #{count + 1}, row={n + 1}/{len(track)}, wait={wait:0.4f}s")
                send_until = time.time() + (args.interval if n == len(track) - 1 else max(wait, args.stream_dt))
                while time.time() < send_until or paused["paused"]:
                    key = read_key()
                    if key == "p":
                        paused["paused"] = True
                        print("\nPAUSED. Press c to continue, s/q to stop.")
                    elif key == "c":
                        paused["paused"] = False
                        print("\nCONTINUE")
                        send_until = time.time() + args.stream_dt
                    elif key in ("s", "q", "\x03"):
                        print("\nSTOP requested")
                        return 0
                    if not paused["paused"]:
                        interface.ModeCtrl(0x01, 0x01, args.speed, 0x00)
                        piper.move_j(joints, args.speed)
                        if have_gripper and len(pos) >= 8:
                            piper.move_gripper(pos[-1], 1)
                    time.sleep(args.stream_dt)
            count += 1
    except KeyboardInterrupt:
        print("\nInterrupted by Ctrl+C")
        return 130
    finally:
        if old is not None and termios is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print("INFO: Replay complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

