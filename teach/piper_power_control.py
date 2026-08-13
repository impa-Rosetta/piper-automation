#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Enable, recover, or disable Piper through the official piper_sdk API."""

from __future__ import annotations

import argparse
import time
from typing import Any

from teach.can_utils import require_can_interface


def enable_piper(interface: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if interface.EnablePiper():
            return True
        time.sleep(0.02)
    return False


def disable_piper(interface: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # DisablePiper returns whether any joint was enabled before the
        # current official DisableArm(7) command.
        if not interface.DisablePiper():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Control Piper enable state with official piper_sdk APIs."
    )
    parser.add_argument("--can-port", default="can0")
    parser.add_argument(
        "--action",
        choices=("enable", "disable", "stop", "recover"),
        required=True,
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--reset-first",
        action="store_true",
        help="Send official MotionCtrl_1 recovery before enabling.",
    )
    args = parser.parse_args()

    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")

    require_can_interface(args.can_port)
    from piper_sdk import C_PiperInterface_V2

    interface = C_PiperInterface_V2(args.can_port)
    interface.ConnectPort()
    time.sleep(0.2)

    try:
        if args.action == "enable":
            if args.reset_first:
                print("Official recovery: MotionCtrl_1(0x02, 0x00, 0x00)")
                interface.MotionCtrl_1(0x02, 0x00, 0x00)
                time.sleep(0.3)
            print("Enabling Piper with EnablePiper() ...")
            if not enable_piper(interface, args.timeout):
                print("ERROR: EnablePiper timeout")
                return 1
            print("Piper enable confirmed for all joints.")
            return 0

        if args.action == "stop":
            print("Sending official emergency stop: MotionCtrl_1(0x01, 0, 0)")
            interface.MotionCtrl_1(0x01, 0x00, 0x00)
            return 0

        if args.action == "recover":
            print("Sending official recovery: MotionCtrl_1(0x02, 0, 0)")
            interface.MotionCtrl_1(0x02, 0x00, 0x00)
            return 0

        print("Disabling Piper with DisablePiper() ...")
        if not disable_piper(interface, args.timeout):
            print("ERROR: DisablePiper timeout")
            return 1
        print("Piper disable confirmed for all joints.")
        return 0
    finally:
        disconnect = getattr(interface, "DisconnectPort", None)
        if callable(disconnect):
            disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
