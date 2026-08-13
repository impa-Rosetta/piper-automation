#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""托盘槽位与官方示教轨迹文件命名工具。"""

from __future__ import annotations

from pathlib import Path

ROW_COUNTS = [4, 5, 4, 5, 4, 5]


def validate_slot(row: int, col: int) -> None:
    """校验托盘 row/col 是否存在。row/col 从 1 开始。"""
    if row < 1 or row > len(ROW_COUNTS):
        raise ValueError(f"row must be 1..{len(ROW_COUNTS)}, got {row}")
    max_col = ROW_COUNTS[row - 1]
    if col < 1 or col > max_col:
        raise ValueError(f"row {row} has columns 1..{max_col}, got {col}")


def slot_key(row: int, col: int) -> str:
    validate_slot(row, col)
    return f"r{row}_c{col}"


def slot_path(row: int, col: int, root: str | Path = "teach/trajectories") -> Path:
    """返回某个槽位的官方 CSV 轨迹路径。"""
    return Path(root) / f"{slot_key(row, col)}.csv"


def armrobot_slot_path(
    row: int,
    col: int,
    root: str | Path = "teach/armrobot_slots",
) -> Path:
    """返回某个槽位的 ArmRobotUA TraceLib 轨迹路径。"""
    return Path(root) / f"{slot_key(row, col)}.log"


def iter_slots() -> list[tuple[int, int]]:
    """按行列顺序返回全部 27 个槽位。"""
    return [(row, col) for row, count in enumerate(ROW_COUNTS, start=1) for col in range(1, count + 1)]
