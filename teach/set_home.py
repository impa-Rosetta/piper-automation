#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保存 Piper 官方示教轨迹使用的统一起点/home。"""

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

from teach.record_trajectory import import_official_piper


def get_position(piper: Any, have_gripper: bool) -> dict[str, Any]:
    joint = [float(v) for v in piper.get_joint_states()[0]]
    data: dict[str, Any] = {"joint": joint}
    if have_gripper:
        data["gripper"] = float(piper.get_gripper_states()[0][0])
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Save current Piper pose as teach home/start pose.")
    parser.add_argument("--can-port", default="can0")
    parser.add_argument("--output", default="teach/home.json")
    parser.add_argument("--no-gripper", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    out = Path(args.output)
    if out.exists() and not args.overwrite:
        raise SystemExit(f"home file exists: {out}. Add --overwrite to replace it.")

    piper_sdk, Piper = import_official_piper()
    piper = Piper(args.can_port)
    interface = piper.init()
    piper.connect()
    time.sleep(0.1)

    input("Move Piper to the desired program-defined home/start pose, then press Enter to save it.")
    data = {
        "format": "piper_teach_home_v1",
        "can_port": args.can_port,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "piper_sdk_file": getattr(piper_sdk, "__file__", None),
        "piper_sdk_version": getattr(piper_sdk, "__version__", "unknown"),
        "status": str(interface.GetArmStatus()),
        **get_position(piper, not args.no_gripper),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"saved home: {out}")
    print("joint(rad)=", data["joint"])
    print("gripper=", data.get("gripper"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
