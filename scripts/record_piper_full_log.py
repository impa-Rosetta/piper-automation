#!/usr/bin/env python3
"""Record a timestamped, non-overwriting Piper full-state CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from piper_sdk import C_PiperInterface_V2


BASE_COLUMNS = [
    "seq",
    "timestamp",
    "elapsed_s",
    "can_fps",
    "is_ok",
    "arm_code",
    "motion_reached",
    "ctrl_mode",
    "move_mode",
    "move_spd_rate",
    "end_x_m",
    "end_y_m",
    "end_z_m",
    "end_rx_deg",
    "end_ry_deg",
    "end_rz_deg",
    "gripper_angle_deg",
    "gripper_effort_nm",
    "gripper_enabled",
]

JOINT_COLUMN_SUFFIXES = [
    "angle_deg",
    "speed_rad_s",
    "current_a",
    "torque_nm",
    "voltage_v",
    "driver_temp_c",
    "motor_temp_c",
    "bus_current_a",
    "enabled",
    "collision",
    "stall",
    "motor_overheat",
    "driver_overcurrent",
    "driver_overheat",
    "voltage_low",
    "driver_error",
    "angle_limit",
    "comm_error",
    "crash_level",
]


def first_attr(obj: Any, names: Iterable[str], default: Any = None) -> Any:
    if obj is None:
        return default
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def nested_attr(obj: Any, container_names: Iterable[str]) -> Any:
    return first_attr(obj, container_names, obj)


def as_float(value: Any, scale: float = 1.0) -> float | str:
    try:
        return float(value) * scale
    except (TypeError, ValueError):
        return ""


def as_int(value: Any) -> int | str:
    try:
        return int(value)
    except (TypeError, ValueError):
        return ""


def as_bool(value: Any) -> bool | str:
    if value is None or value == "":
        return ""
    return bool(value)


def unique_output_path(output_dir: Path, start: datetime) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"piper_full_{start.strftime('%Y%m%d_%H%M%S_%f')}"
    candidate = output_dir / f"{stem}.csv"
    sequence = 1
    while candidate.exists():
        candidate = output_dir / f"{stem}_{sequence:02d}.csv"
        sequence += 1
    return candidate


def header(*, include_diy_gripper: bool = False) -> list[str]:
    columns = list(BASE_COLUMNS)
    for joint_index in range(1, 7):
        columns.extend(
            f"j{joint_index}_{suffix}" for suffix in JOINT_COLUMN_SUFFIXES
        )
    if include_diy_gripper:
        columns.extend(
            [
                "diy_gripper_state",
                "diy_gripper_event",
                "diy_gripper_event_index",
                "diy_gripper_timeline_time_s",
            ]
        )
    return columns


def load_diy_gripper_events(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events", [])
    if not isinstance(events, list):
        raise ValueError(f"gripper timeline has no event list: {path}")
    normalized = []
    for index, event in enumerate(events, start=1):
        action = str(event.get("action", "")).lower()
        if action not in ("open", "close", "grip", "release"):
            raise ValueError(f"invalid gripper action at event {index}: {event}")
        normalized.append(
            {
                "index": index,
                "time_s": float(event["time_s"]),
                "action": action,
                "state": "closed" if action in ("close", "grip") else "open",
            }
        )
    return sorted(normalized, key=lambda item: item["time_s"])


def load_live_diy_gripper_state(
    path: Path,
) -> tuple[str, int, float | str]:
    """Read the latest commanded DIY gripper state from an atomic JSON file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = str(data.get("state", "unknown"))
        if state not in {"open", "closed", "unknown"}:
            state = "unknown"
        sequence = int(data.get("sequence", 0))
        epoch_s: float | str = float(data.get("timestamp", ""))
        return state, sequence, epoch_s
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return "unknown", 0, ""


def diy_gripper_annotation(
    events: list[dict[str, Any]],
    elapsed_s: float,
    *,
    initial_state: str,
    last_event_index: int,
) -> tuple[str, str, int, float | str]:
    state = initial_state
    event_name = ""
    event_index = 0
    event_time: float | str = ""
    for event in events:
        if float(event["time_s"]) <= elapsed_s:
            state = str(event["state"])
            if int(event["index"]) > last_event_index:
                event_name = str(event["action"])
                event_index = int(event["index"])
                event_time = float(event["time_s"])
        else:
            break
    return state, event_name, event_index, event_time


def motor(wrapper: Any, index: int) -> Any:
    return first_attr(
        wrapper,
        (
            f"motor_{index}",
            f"motor{index}",
            f"joint_{index}",
            f"joint{index}",
        ),
    )


def status_flag(status_obj: Any, index: int, kind: str) -> bool | str:
    error_status = first_attr(status_obj, ("err_status", "error_status"))
    if kind == "angle":
        names = (
            f"joint_{index}_angle_limit",
            f"joint_{index}_angle_limit_status",
        )
    else:
        names = (
            f"joint_{index}_communication_status",
            f"joint_{index}_communication_error",
        )
    value = first_attr(error_status, names)
    if value is not None:
        return bool(value)

    error_code = as_int(first_attr(status_obj, ("err_code", "error_code")))
    if error_code == "":
        return ""
    bit = (index - 1) + (8 if kind == "angle" else 0)
    return bool(int(error_code) & (1 << bit))


def crash_levels(interface: Any) -> list[int | str]:
    try:
        feedback = interface.GetCrashProtectionLevelFeedback()
        values = nested_attr(
            feedback,
            (
                "crash_protection_level_feedback",
                "crash_protection_rating",
            ),
        )
        return [
            as_int(
                first_attr(
                    values,
                    (
                        f"joint_{index}_protection_level",
                        f"joint_{index}_crash_protection_level",
                    ),
                )
            )
            for index in range(1, 7)
        ]
    except Exception:  # noqa: BLE001
        return [""] * 6


def request_crash_levels(interface: Any) -> None:
    query = getattr(interface, "ArmParamEnquiryAndConfig", None)
    if not callable(query):
        return
    try:
        query(param_enquiry=0x02)
        time.sleep(0.1)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: collision-level query failed: {exc}")


def read_sample(
    interface: Any,
    levels: list[int | str],
) -> tuple[list[Any], list[float | str]]:
    joint_wrapper = interface.GetArmJointMsgs()
    joint_state = nested_attr(joint_wrapper, ("joint_state",))
    status_wrapper = interface.GetArmStatus()
    status = nested_attr(status_wrapper, ("arm_status",))
    pose_wrapper = interface.GetArmEndPoseMsgs()
    pose = nested_attr(pose_wrapper, ("end_pose",))
    gripper_wrapper = interface.GetArmGripperMsgs()
    gripper = nested_attr(gripper_wrapper, ("gripper_state",))
    high_wrapper = interface.GetArmHighSpdInfoMsgs()
    low_wrapper = interface.GetArmLowSpdInfoMsgs()
    mode_wrapper = interface.GetArmModeCtrl()
    mode = nested_attr(mode_wrapper, ("ctrl_151", "arm_mode_ctrl", "mode_ctrl"))

    try:
        can_fps: float | str = float(interface.GetCanFps())
    except Exception:  # noqa: BLE001
        can_fps = ""

    status_hz = as_float(first_attr(status_wrapper, ("Hz", "hz")))
    joint_hz = as_float(first_attr(joint_wrapper, ("Hz", "hz")))
    is_ok = (
        can_fps != ""
        and float(can_fps) > 0.0
        and status_hz != ""
        and float(status_hz) > 0.0
        and joint_hz != ""
        and float(joint_hz) > 0.0
    )

    arm_code = as_int(first_attr(status, ("arm_status", "status")))
    motion_status = as_int(first_attr(status, ("motion_status",)))
    motion_reached: bool | str = (
        motion_status == 0 if motion_status != "" else ""
    )

    ctrl_mode = as_int(first_attr(mode, ("ctrl_mode",)))
    if ctrl_mode == "":
        ctrl_mode = as_int(first_attr(status, ("ctrl_mode",)))
    move_mode = as_int(first_attr(mode, ("move_mode",)))
    if move_mode == "":
        move_mode = as_int(first_attr(status, ("mode_feed", "move_mode")))
    move_speed = as_int(
        first_attr(mode, ("move_spd_rate_ctrl", "move_spd_rate"))
    )

    gripper_status = first_attr(gripper, ("foc_status", "status_code"))
    gripper_enabled = as_bool(
        first_attr(
            gripper_status,
            ("driver_enable_status", "enabled"),
        )
    )
    if gripper_enabled == "" and isinstance(gripper_status, int):
        gripper_enabled = bool(gripper_status & (1 << 6))

    base_values: list[Any] = [
        can_fps,
        is_ok,
        arm_code,
        motion_reached,
        ctrl_mode,
        move_mode,
        move_speed,
        as_float(first_attr(pose, ("X_axis", "x")), 1e-6),
        as_float(first_attr(pose, ("Y_axis", "y")), 1e-6),
        as_float(first_attr(pose, ("Z_axis", "z")), 1e-6),
        as_float(first_attr(pose, ("RX_axis", "rx")), 1e-3),
        as_float(first_attr(pose, ("RY_axis", "ry")), 1e-3),
        as_float(first_attr(pose, ("RZ_axis", "rz")), 1e-3),
        as_float(first_attr(gripper, ("grippers_angle", "angle")), 1e-3),
        as_float(first_attr(gripper, ("grippers_effort", "effort")), 1e-3),
        gripper_enabled,
    ]

    joint_values: list[Any] = []
    terminal_angles: list[float | str] = []
    for index in range(1, 7):
        angle = as_float(first_attr(joint_state, (f"joint_{index}",)), 1e-3)
        terminal_angles.append(angle)
        high = motor(high_wrapper, index)
        low = motor(low_wrapper, index)
        foc = first_attr(low, ("foc_status", "status_code"))

        def foc_flag(name: str, bit: int) -> bool | str:
            value = first_attr(foc, (name,))
            if value is not None:
                return bool(value)
            if isinstance(foc, int):
                return bool(foc & (1 << bit))
            return ""

        effort_raw = first_attr(high, ("effort", "torque"))
        torque = as_float(effort_raw, 1e-3)
        if torque == "":
            current_raw = first_attr(high, ("current",))
            coefficient = 1.18125 if index <= 3 else 0.95844
            current_a = as_float(current_raw, 1e-3)
            torque = (
                float(current_a) * coefficient if current_a != "" else ""
            )

        joint_values.extend(
            [
                angle,
                as_float(first_attr(high, ("motor_speed", "speed")), 1e-3),
                as_float(first_attr(high, ("current",)), 1e-3),
                torque,
                as_float(first_attr(low, ("vol", "voltage")), 0.1),
                as_int(first_attr(low, ("foc_temp", "driver_temp"))),
                as_int(first_attr(low, ("motor_temp",))),
                as_float(first_attr(low, ("bus_current",)), 1e-3),
                foc_flag("driver_enable_status", 6),
                foc_flag("collision_status", 4),
                foc_flag("stall_status", 7),
                foc_flag("motor_overheating", 1),
                foc_flag("driver_overcurrent", 2),
                foc_flag("driver_overheating", 3),
                foc_flag("voltage_too_low", 0),
                foc_flag("driver_error_status", 5),
                status_flag(status, index, "angle"),
                status_flag(status, index, "communication"),
                levels[index - 1],
            ]
        )

    return base_values + joint_values, terminal_angles


def csv_value(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.6f}"
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record full Piper feedback to a new timestamped CSV. "
            "Press Ctrl+C to stop and save."
        )
    )
    parser.add_argument("--can-port", default="can0")
    parser.add_argument(
        "--interval",
        type=float,
        default=0.1,
        help="Sampling interval in seconds; default 0.1 (10 Hz).",
    )
    parser.add_argument(
        "--output-dir",
        default="records/full_status_logs",
        help="A new timestamped CSV is created here on every run.",
    )
    parser.add_argument(
        "--no-crash-level-query",
        action="store_true",
        help="Do not send the read-only 0x477 collision-level query.",
    )
    parser.add_argument(
        "--diy-gripper-timeline",
        default=None,
        help=(
            "Optional teach/gripper_timelines/*.json. Adds DIY gripper "
            "state/event annotation columns based on this recording elapsed time."
        ),
    )
    parser.add_argument(
        "--diy-gripper-initial-state",
        choices=("open", "closed", "unknown"),
        default="unknown",
        help="Initial DIY gripper state before the first timeline event.",
    )
    parser.add_argument(
        "--diy-gripper-state-file",
        default=None,
        help=(
            "Optional JSON file updated by teach.control_gripper_logged. "
            "Adds commanded DIY gripper open/close state and live events."
        ),
    )
    args = parser.parse_args()
    if not 0.005 <= args.interval <= 10.0:
        parser.error("--interval must be within 0.005..10 seconds")
    diy_events: list[dict[str, Any]] = []
    if args.diy_gripper_timeline:
        diy_events = load_diy_gripper_events(Path(args.diy_gripper_timeline))
    live_gripper_path = (
        Path(args.diy_gripper_state_file)
        if args.diy_gripper_state_file
        else None
    )
    if diy_events and live_gripper_path is not None:
        parser.error(
            "--diy-gripper-timeline and --diy-gripper-state-file are mutually exclusive"
        )

    interface = C_PiperInterface_V2(args.can_port)
    interface.ConnectPort()
    time.sleep(0.5)
    if not args.no_crash_level_query:
        request_crash_levels(interface)
    levels = crash_levels(interface)

    start_datetime = datetime.now().astimezone()
    start_monotonic = time.monotonic()
    output = unique_output_path(Path(args.output_dir), start_datetime)
    rows = 0
    next_deadline = start_monotonic

    print(f"Connected to Piper on {args.can_port}.")
    print(f"Sampling interval: {args.interval:.3f} s")
    columns = header(
        include_diy_gripper=bool(diy_events) or live_gripper_path is not None
    )
    print(f"Columns: {len(columns)}")
    if live_gripper_path is not None:
        print(f"DIY gripper command-state file: {live_gripper_path}")
        print("DIY gripper values are commanded states, not measured feedback.")
    print(f"Columns: {len(columns)}")
    if diy_events:
        print(f"DIY gripper timeline: {args.diy_gripper_timeline}")
        print(f"DIY gripper events: {len(diy_events)}")
    print(f"Recording started: {start_datetime.isoformat(timespec='milliseconds')}")
    print(f"Output file: {output}")
    print("Press Ctrl+C to stop and close the file.")

    try:
        with output.open("x", newline="", encoding="utf-8", buffering=1) as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            last_diy_event_index = 0
            live_diy_state = args.diy_gripper_initial_state
            last_live_sequence = 0
            if live_gripper_path is not None:
                (
                    existing_state,
                    last_live_sequence,
                    _existing_epoch,
                ) = load_live_diy_gripper_state(live_gripper_path)
                if existing_state != "unknown":
                    live_diy_state = existing_state
            while True:
                remaining = next_deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                sample_monotonic = time.monotonic()
                timestamp = time.time()
                elapsed = sample_monotonic - start_monotonic
                values, angles = read_sample(interface, levels)
                extra_values: list[Any] = []
                if diy_events:
                    (
                        diy_state,
                        diy_event,
                        diy_event_index,
                        diy_event_time,
                    ) = diy_gripper_annotation(
                        diy_events,
                        elapsed,
                        initial_state=args.diy_gripper_initial_state,
                        last_event_index=last_diy_event_index,
                    )
                    if diy_event_index:
                        last_diy_event_index = diy_event_index
                    extra_values = [
                        diy_state,
                        diy_event,
                        diy_event_index or "",
                        diy_event_time,
                    ]
                elif live_gripper_path is not None:
                    (
                        current_state,
                        current_sequence,
                        _event_epoch,
                    ) = load_live_diy_gripper_state(live_gripper_path)
                    live_event = ""
                    live_event_index: int | str = ""
                    live_event_time: float | str = ""
                    if current_state != "unknown":
                        live_diy_state = current_state
                    if current_sequence > last_live_sequence:
                        live_event = (
                            "open" if live_diy_state == "open" else "close"
                        )
                        live_event_index = current_sequence
                        live_event_time = elapsed
                        last_live_sequence = current_sequence
                    extra_values = [
                        live_diy_state,
                        live_event,
                        live_event_index,
                        live_event_time,
                    ]

                writer.writerow(
                    [
                        rows + 1,
                        f"{timestamp:.6f}",
                        f"{elapsed:.6f}",
                        *[csv_value(value) for value in values],
                        *[csv_value(value) for value in extra_values],
                    ]
                )
                angle_text = " ".join(
                    f"J{index}={float(value):+8.3f}deg"
                    if value != ""
                    else f"J{index}=N/A"
                    for index, value in enumerate(angles, start=1)
                )
                print(
                    f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
                    f"t={elapsed:8.3f}s {angle_text}"
                )
                rows += 1
                next_deadline += args.interval
                if next_deadline < sample_monotonic:
                    skipped = (
                        int((sample_monotonic - next_deadline) / args.interval)
                        + 1
                    )
                    next_deadline += skipped * args.interval
    except KeyboardInterrupt:
        print("\nRecording stopped by Ctrl+C.")
    finally:
        disconnect = getattr(interface, "DisconnectPort", None)
        if callable(disconnect):
            disconnect()

    print(f"Saved {rows} samples to: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
