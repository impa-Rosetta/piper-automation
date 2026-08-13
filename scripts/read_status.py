from __future__ import annotations

import argparse
import time

from piper_sdk import C_PiperInterface_V2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read Piper arm status over CAN.")
    parser.add_argument("--can-port", default="can0", help="CAN interface name, for example can0.")
    parser.add_argument("--hz", type=float, default=10.0, help="Print rate in Hz.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    interval = 1.0 / max(0.5, min(args.hz, 200.0))

    piper = C_PiperInterface_V2(args.can_port)
    piper.ConnectPort()

    print(f"Connected to Piper on {args.can_port}. Press Ctrl+C to stop.")
    while True:
        arm_status = piper.GetArmStatus()
        joint_state = piper.GetArmJointMsgs().joint_state
        end_pose = piper.GetArmEndPoseMsgs().end_pose

        joints_deg = [
            joint_state.joint_1 * 1e-3,
            joint_state.joint_2 * 1e-3,
            joint_state.joint_3 * 1e-3,
            joint_state.joint_4 * 1e-3,
            joint_state.joint_5 * 1e-3,
            joint_state.joint_6 * 1e-3,
        ]
        tcp_mm_deg = [
            end_pose.X_axis * 1e-3,
            end_pose.Y_axis * 1e-3,
            end_pose.Z_axis * 1e-3,
            end_pose.RX_axis * 1e-3,
            end_pose.RY_axis * 1e-3,
            end_pose.RZ_axis * 1e-3,
        ]

        print(
            "status="
            f"{arm_status.arm_status.arm_status} "
            "mode="
            f"{arm_status.arm_status.ctrl_mode} "
            "joints_deg="
            f"{[round(value, 3) for value in joints_deg]} "
            "tcp="
            f"{[round(value, 3) for value in tcp_mm_deg]}"
        )
        time.sleep(interval)


if __name__ == "__main__":
    main()
