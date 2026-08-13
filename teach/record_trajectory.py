#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""迁移自 AgileX 官方 recordAndPlayTraj/recordTrajectory_en.py。

功能：在 Piper 官方示教模式下录制连续轨迹，并保存为官方示例兼容的 CSV。

注意：
- 本脚本使用 piper_sdk 的官方高层 Piper 类。
- 不直接发送 CAN 帧，不修改 SDK 源码。
- 轨迹文件格式保持官方示例形式：
  wait_time,joint1,joint2,joint3,joint4,joint5,joint6,gripper
  其中 joint 单位为 rad，gripper 为 SDK 高层接口返回值。
"""

from __future__ import annotations

import argparse
import csv
import inspect
import os
import sys
import time
from pathlib import Path
from typing import Any


def import_official_piper() -> tuple[Any, Any]:
    """导入官方 piper_sdk 与 Piper 类；缺失时给出明确版本提示。"""
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
            "官方教程说明 recordAndPlayTraj 需要 API-enabled 版本，例如 piper_sdk 的 1_0_0_beta 分支。"
        )
    return piper_sdk, Piper


def print_sdk_info(piper_sdk: Any, piper: Any, interface: Any) -> None:
    """打印运行时 SDK 信息，相当于用户要求的 python -c 检查。"""
    print("SDK inspection")
    print("--------------")
    print(f"piper_sdk.__file__: {getattr(piper_sdk, '__file__', None)}")
    print(f"piper_sdk.__version__: {getattr(piper_sdk, '__version__', 'unknown')}")
    print(f"Piper class: {type(piper)}")
    print(f"Interface class: {type(interface)}")
    print("Piper methods:")
    for name in ("init", "connect", "get_joint_states", "get_gripper_states", "enable_arm", "disable_arm", "move_j", "move_gripper"):
        attr = getattr(piper, name, None)
        if callable(attr):
            try:
                sig = inspect.signature(attr)
            except Exception:  # noqa: BLE001
                sig = "(...)"
            print(f"  {name}{sig}")
        else:
            print(f"  {name}: missing")
    print("Interface methods:")
    for name in ("GetArmStatus", "ModeCtrl", "EmergencyStop"):
        attr = getattr(interface, name, None)
        print(f"  {name}: {'ok' if callable(attr) else 'missing'}")
    print()


def get_ctrl_mode(interface: Any) -> int | None:
    """读取官方状态里的 ctrl_mode。2=示教模式，1=CAN 控制模式。"""
    try:
        return int(interface.GetArmStatus().arm_status.ctrl_mode)
    except Exception:  # noqa: BLE001
        return None


def wait_for_teach_mode(interface: Any, timeout: float) -> None:
    """等待用户短按 teach 按钮进入示教模式。"""
    print("step 1: Short-press teach button to enter teach mode; green light should turn on.")
    deadline = time.time() + timeout
    while get_ctrl_mode(interface) != 2:
        if time.time() > deadline:
            raise SystemExit("ERROR: Teach mode detection timeout. Check teach button, firmware, and SDK version.")
        time.sleep(0.01)
    print("INFO: Teach mode detected (ctrl_mode == 2).")


def get_position(piper: Any, have_gripper: bool) -> tuple[float, ...]:
    """读取当前位置。官方高层接口返回 joint rad；夹爪值沿用 SDK 返回单位。"""
    joint_state = tuple(float(v) for v in piper.get_joint_states()[0])
    if have_gripper:
        gripper = float(piper.get_gripper_states()[0][0])
        return joint_state + (gripper,)
    return joint_state


def main() -> int:
    parser = argparse.ArgumentParser(description="Official-style Piper teach trajectory recorder.")
    parser.add_argument("--can-port", default="can0")
    parser.add_argument("--output", default="teach/trajectory.csv")
    parser.add_argument("--record-time", type=float, default=0.0, help="最大录制时间，0 表示直到退出示教或 Ctrl+C。")
    parser.add_argument("--timeout", type=float, default=30.0, help="等待进入示教模式超时时间。")
    parser.add_argument("--sample-dt", type=float, default=0.005, help="采样周期，默认 0.005s，约 200Hz。")
    parser.add_argument("--no-gripper", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-sdk", action="store_true")
    parser.add_argument("--record-static", action="store_true", default=True, help="固定频率记录每一帧，包括静止点。默认开启。")
    parser.add_argument("--change-only", action="store_true", help="恢复官方原始逻辑：只在位置变化时写入。")
    args = parser.parse_args()

    out = Path(args.output)
    if out.exists() and not args.overwrite:
        raise SystemExit(f"trajectory file exists: {out}. Add --overwrite to replace it.")
    out.parent.mkdir(parents=True, exist_ok=True)

    piper_sdk, Piper = import_official_piper()
    piper = Piper(args.can_port)
    interface = piper.init()
    piper.connect()
    time.sleep(0.1)

    print(f"Connected Piper on {args.can_port}")
    print(f"piper_sdk.__file__: {getattr(piper_sdk, '__file__', None)}")
    if args.print_sdk:
        print_sdk_info(piper_sdk, piper, interface)

    have_gripper = not args.no_gripper
    wait_for_teach_mode(interface, args.timeout)
    input("step 2: Move arm to trajectory start, then press Enter to start recording.")

    last_pos = get_position(piper, have_gripper)
    last_time = time.time()
    end_time = last_time + args.record_time if args.record_time > 0 else None
    rows = 0

    print(f"INFO: Recording to {out}. Short-press teach button again to exit teach mode, or Ctrl+C to stop.")
    try:
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([0.0, *last_pos])
            rows += 1
            print(f"INFO: row={rows}, wait=0.0000s, pos={last_pos}  # explicit start/home frame")
            while end_time is None or time.time() < end_time:
                if get_ctrl_mode(interface) != 2:
                    print("INFO: Teach mode exited; stop recording.")
                    break
                current_pos = get_position(piper, have_gripper)
                should_write = (not args.change_only) or current_pos != last_pos
                if should_write:
                    wait_time = round(time.time() - last_time, 4)
                    writer.writerow([wait_time, *current_pos])
                    rows += 1
                    if rows % 20 == 0 or current_pos != last_pos:
                        print(f"INFO: row={rows}, wait={wait_time:0.4f}s, pos={current_pos}")
                    last_pos = current_pos
                    last_time = time.time()
                time.sleep(args.sample_dt)
    except KeyboardInterrupt:
        print("\nINFO: Recording interrupted by Ctrl+C; file has been saved.")

    print(f"INFO: Recording complete. rows={rows}, file={out}")
    print("step 3: If teach light is still on, short-press teach button to exit teach mode.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


