#!/usr/bin/env python3
"""Position-only trajectory parsing and interpolation helpers."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class TrajectorySample:
    time_from_start: float
    joints: tuple[float, float, float, float, float, float]
    gripper: float | None = None


def rows_to_samples(
    rows: Iterable[Sequence[float]],
    *,
    have_gripper: bool,
) -> list[TrajectorySample]:
    """Convert official delta-time CSV rows to cumulative-time samples."""
    samples: list[TrajectorySample] = []
    elapsed = 0.0
    for index, row in enumerate(rows):
        minimum = 8 if have_gripper else 7
        if len(row) < minimum:
            raise ValueError(
                f"row {index + 1} has {len(row)} values; expected at least {minimum}"
            )
        delta = float(row[0])
        if delta < 0:
            raise ValueError(f"row {index + 1} has a negative wait time")
        if index:
            elapsed += delta
        joints = tuple(float(value) for value in row[1:7])
        gripper = float(row[7]) if have_gripper else None
        samples.append(TrajectorySample(elapsed, joints, gripper))

    if not samples:
        raise ValueError("trajectory is empty")
    return samples


def sample_linear(
    samples: Sequence[TrajectorySample],
    time_from_start: float,
) -> TrajectorySample:
    """Sample a position-only trajectory using bounded linear interpolation."""
    if time_from_start <= samples[0].time_from_start:
        return samples[0]
    if time_from_start >= samples[-1].time_from_start:
        return samples[-1]

    times = [sample.time_from_start for sample in samples]
    right = bisect_right(times, time_from_start)
    before = samples[right - 1]
    after = samples[right]
    duration = after.time_from_start - before.time_from_start
    if duration <= 0:
        return after
    ratio = (time_from_start - before.time_from_start) / duration
    joints = tuple(
        start + ratio * (end - start)
        for start, end in zip(before.joints, after.joints)
    )
    if before.gripper is None or after.gripper is None:
        gripper = after.gripper
    else:
        gripper = before.gripper + ratio * (after.gripper - before.gripper)
    return TrajectorySample(time_from_start, joints, gripper)


def joint_error(
    target: Sequence[float],
    actual: Sequence[float],
) -> tuple[float, float, float, float, float, float]:
    if len(target) != 6 or len(actual) != 6:
        raise ValueError("joint vectors must contain six values")
    return tuple(float(t) - float(a) for t, a in zip(target, actual))


def max_abs_joint_error(target: Sequence[float], actual: Sequence[float]) -> float:
    return max(abs(value) for value in joint_error(target, actual))
