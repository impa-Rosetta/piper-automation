#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ArmRobotUA 风格的 Piper 离散示教点采集工具。"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from typing import Any


OFFICIAL_JOINT_LIMITS_DEG = (
    (-150.0, 150.0),
    (0.0, 180.0),
    (-170.0, 0.0),
    (-100.0, 100.0),
    (-70.0, 70.0),
    (-170.0, 170.0),
)


@dataclass(frozen=True)
class FeedbackSample:
    """一次 Piper 反馈；角度均为度，TCP 位置单位为毫米。"""

    joints_deg: tuple[float, float, float, float, float, float]
    tcp: tuple[float, float, float, float, float, float]
    ctrl_mode: int | None
    arm_status: int | None


def _scaled(value: int | float) -> float:
    return float(value) / 1000.0


def read_feedback(interface: Any) -> FeedbackSample:
    """使用官方 V2 反馈接口读取关节角和末端位姿。"""
    joint = interface.GetArmJointMsgs().joint_state
    pose = interface.GetArmEndPoseMsgs().end_pose
    try:
        status = interface.GetArmStatus().arm_status
        ctrl_mode = int(status.ctrl_mode)
        arm_status = int(status.arm_status)
    except Exception:  # noqa: BLE001
        ctrl_mode = None
        arm_status = None
    return FeedbackSample(
        joints_deg=tuple(
            _scaled(value)
            for value in (
                joint.joint_1,
                joint.joint_2,
                joint.joint_3,
                joint.joint_4,
                joint.joint_5,
                joint.joint_6,
            )
        ),
        tcp=tuple(
            _scaled(value)
            for value in (
                pose.X_axis,
                pose.Y_axis,
                pose.Z_axis,
                pose.RX_axis,
                pose.RY_axis,
                pose.RZ_axis,
            )
        ),
        ctrl_mode=ctrl_mode,
        arm_status=arm_status,
    )


def capture_stable_feedback(
    interface: Any,
    sample_count: int = 9,
    sample_dt: float = 0.02,
) -> tuple[FeedbackSample, dict[str, float]]:
    """采集多帧并取中位数，降低单帧反馈抖动。"""
    if sample_count < 3:
        raise ValueError("sample_count must be at least 3")
    samples: list[FeedbackSample] = []
    for index in range(sample_count):
        samples.append(read_feedback(interface))
        if index + 1 < sample_count:
            time.sleep(sample_dt)

    joint_columns = list(zip(*(sample.joints_deg for sample in samples)))
    tcp_columns = list(zip(*(sample.tcp for sample in samples)))
    joint_median = tuple(statistics.median(column) for column in joint_columns)
    tcp_median = tuple(statistics.median(column) for column in tcp_columns)
    joint_span = max(max(column) - min(column) for column in joint_columns)
    xyz_span = max(max(column) - min(column) for column in tcp_columns[:3])
    rpy_span = max(max(column) - min(column) for column in tcp_columns[3:])
    middle = samples[len(samples) // 2]
    return (
        FeedbackSample(
            joints_deg=joint_median,
            tcp=tcp_median,
            ctrl_mode=middle.ctrl_mode,
            arm_status=middle.arm_status,
        ),
        {
            "joint_span_deg": joint_span,
            "xyz_span_mm": xyz_span,
            "rpy_span_deg": rpy_span,
        },
    )


def point_to_dict(
    sample: FeedbackSample,
    stability: dict[str, float],
) -> dict[str, Any]:
    return {
        "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "joints_deg": [round(value, 6) for value in sample.joints_deg],
        "tcp": [round(value, 6) for value in sample.tcp],
        "ctrl_mode": sample.ctrl_mode,
        "arm_status": sample.arm_status,
        "stability": {
            key: round(value, 6) for key, value in stability.items()
        },
    }


def make_trace_params(
    record: dict[str, Any],
    mode: str,
    gripper: float = 0.0,
    locked_rpy: tuple[float, float, float] | None = None,
) -> tuple[float, float, float, float, float, float, float]:
    """将示教记录转换为 ArmRobotUA 的 J/P/L 参数。"""
    mode = mode.upper()
    if mode == "J":
        values = list(record["joints_deg"])
    elif mode in {"P", "L"}:
        values = list(record["tcp"])
        if locked_rpy is not None:
            values[3:6] = locked_rpy
    else:
        raise ValueError(f"unsupported mode: {mode}")
    if len(values) != 6:
        raise ValueError(f"{mode} point must contain six values")
    return tuple(float(value) for value in values) + (float(gripper),)


def joint_limit_violations(
    joints_deg: list[float] | tuple[float, ...],
) -> list[str]:
    """按官方 piper_set_motor_angle_limit.py 检查六轴命令范围。"""
    if len(joints_deg) != 6:
        raise ValueError("six joint values are required")
    result: list[str] = []
    for index, (value, limits) in enumerate(
        zip(joints_deg, OFFICIAL_JOINT_LIMITS_DEG),
        start=1,
    ):
        lower, upper = limits
        if not lower <= float(value) <= upper:
            result.append(
                f"J{index}={float(value):.3f} deg outside "
                f"[{lower:.1f}, {upper:.1f}] deg"
            )
    return result


def tray_neighbor_errors(
    slots: dict[str, dict[str, Any]],
    expected_pitch_mm: float = 70.0,
) -> list[dict[str, float | str]]:
    """检查三角点阵相邻孔位间距；只报告，不修改示教点。"""
    from teach.common_slots import ROW_COUNTS

    canonical: dict[str, tuple[float, float]] = {}
    row_pitch = expected_pitch_mm * math.sqrt(3.0) / 2.0
    for row, count in enumerate(ROW_COUNTS, start=1):
        offset = expected_pitch_mm / 2.0 if count == 4 else 0.0
        for col in range(1, count + 1):
            canonical[f"r{row}_c{col}"] = (
                offset + (col - 1) * expected_pitch_mm,
                (row - 1) * row_pitch,
            )

    keys = sorted(set(slots) & set(canonical))
    errors: list[dict[str, float | str]] = []
    for index, first in enumerate(keys):
        for second in keys[index + 1 :]:
            cx1, cy1 = canonical[first]
            cx2, cy2 = canonical[second]
            canonical_distance = math.hypot(cx2 - cx1, cy2 - cy1)
            if abs(canonical_distance - expected_pitch_mm) > 1e-6:
                continue
            xyz1 = slots[first]["tcp"][:3]
            xyz2 = slots[second]["tcp"][:3]
            measured = math.sqrt(
                sum((float(b) - float(a)) ** 2 for a, b in zip(xyz1, xyz2))
            )
            errors.append(
                {
                    "from": first,
                    "to": second,
                    "distance_mm": round(measured, 3),
                    "error_mm": round(measured - expected_pitch_mm, 3),
                }
            )
    return errors
