#!/usr/bin/env python3
"""Send a DIY gripper command and publish its commanded state."""

from __future__ import annotations

import argparse
import os

from teach.gripper_serial import DiyGripper


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Control the DIY gripper and record its commanded state."
    )
    parser.add_argument("--port", default="/dev/piper_gripper")
    parser.add_argument(
        "--action",
        required=True,
        choices=("open", "close", "grip", "release"),
    )
    parser.add_argument("--baudrate", type=int, default=9600)
    parser.add_argument("--timeout", type=float, default=0.3)
    parser.add_argument("--startup-delay", type=float, default=0.0)
    parser.add_argument(
        "--state-file",
        default="records/diy_gripper_state.json",
    )
    args = parser.parse_args()
    os.environ["PIPER_DIY_GRIPPER_STATE_FILE"] = args.state_file

    with DiyGripper(
        port=args.port,
        baudrate=args.baudrate,
        timeout=args.timeout,
        startup_delay=max(0.0, args.startup_delay),
        wait_feedback=False,
    ) as gripper:
        gripper.send(args.action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
