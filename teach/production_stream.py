#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent Piper + DIY gripper playback for production task sequences."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from teach.gripper_serial import DiyGripper
from teach.play_slot_with_gripper import (
    enter_raw_terminal,
    load_piper_only_samples,
    read_key,
    recorded_period_ns,
    restore_terminal,
)
from teach.play_trajectory import enable_official, ensure_can_mode, import_official_piper
from teach.play_trajectory_precise import (
    converge,
    current_joints,
    send_target,
    wait_next,
)
from teach.trajectory_math import (
    TrajectorySample,
    max_abs_joint_error,
    sample_linear,
)


@dataclass(frozen=True)
class ReplaySettings:
    speed: int
    play_speed: float
    stream_dt: float
    clock: str
    event_sync: str
    gripper_action_hold: float
    gripper_event_offset: float


@dataclass(frozen=True)
class PreparedTask:
    task_id: str
    trajectory: Path
    timeline: Path
    samples: tuple[TrajectorySample, ...]
    events: tuple[dict[str, Any], ...]
    settings: ReplaySettings
    original_duration_s: float
    trim_start_s: float
    trim_end_s: float
    annotation_speed: int | None
    annotation_play_speed: float | None

    @property
    def duration_s(self) -> float:
        return self.samples[-1].time_from_start

    @property
    def saved_s(self) -> float:
        return max(0.0, self.original_duration_s - self.duration_s)


def _max_error(left: Sequence[float], right: Sequence[float]) -> float:
    return max(abs(float(a) - float(b)) for a, b in zip(left, right))


def _first_time_at_or_after(
    samples: Sequence[TrajectorySample], requested_time: float
) -> int:
    for index, sample in enumerate(samples):
        if sample.time_from_start >= requested_time:
            return index
    return len(samples) - 1


def _trim_window(
    samples: Sequence[TrajectorySample],
    *,
    events: Sequence[dict[str, Any]],
    threshold: float,
    leading_keep: float,
    trailing_keep: float,
    action_hold: float,
) -> tuple[int, int]:
    """Find a conservative moving window while retaining endpoint settle time."""
    if len(samples) < 3 or threshold <= 0:
        return 0, len(samples) - 1

    first_joints = samples[0].joints
    last_joints = samples[-1].joints
    first_moving = next(
        (
            index
            for index, sample in enumerate(samples)
            if _max_error(sample.joints, first_joints) > threshold
        ),
        None,
    )
    last_moving = next(
        (
            index
            for index in range(len(samples) - 1, -1, -1)
            if _max_error(samples[index].joints, last_joints) > threshold
        ),
        None,
    )
    if first_moving is None or last_moving is None or first_moving > last_moving:
        return 0, len(samples) - 1

    start_time = max(0.0, samples[first_moving].time_from_start - leading_keep)
    event_end = max(
        (float(event["time_s"]) + action_hold for event in events),
        default=0.0,
    )
    end_time = max(
        samples[last_moving].time_from_start + trailing_keep,
        event_end,
    )
    start_index = _first_time_at_or_after(samples, start_time)
    end_index = _first_time_at_or_after(samples, end_time)
    end_index = max(start_index + 1, min(end_index, len(samples) - 1))
    return start_index, end_index


def prepare_task(
    *,
    task_id: str,
    trajectory: Path,
    timeline: Path,
    settings: ReplaySettings,
    auto_trim: bool,
    idle_threshold: float,
    leading_keep: float,
    trailing_keep: float,
) -> PreparedTask:
    import json

    original = load_piper_only_samples(trajectory)
    original_duration = original[-1].time_from_start
    timeline_data = json.loads(timeline.read_text(encoding="utf-8"))
    metadata = timeline_data.get("metadata", {})
    annotation_speed = (
        int(metadata["speed"]) if metadata.get("speed") is not None else None
    )
    annotation_play_speed = (
        float(metadata["play_speed"])
        if metadata.get("play_speed") is not None
        else None
    )
    raw_events = [dict(event) for event in timeline_data.get("events", [])]
    for event in raw_events:
        event["time_s"] = max(
            0.0,
            float(event["time_s"]) + settings.gripper_event_offset,
        )

    if auto_trim:
        start_index, end_index = _trim_window(
            original,
            events=raw_events,
            threshold=idle_threshold,
            leading_keep=leading_keep,
            trailing_keep=trailing_keep,
            action_hold=settings.gripper_action_hold,
        )
    else:
        start_index, end_index = 0, len(original) - 1

    origin = original[start_index].time_from_start
    absolute_end = original[end_index].time_from_start
    samples = tuple(
        TrajectorySample(
            time_from_start=sample.time_from_start - origin,
            joints=sample.joints,
            gripper=sample.gripper,
        )
        for sample in original[start_index : end_index + 1]
    )
    events: list[dict[str, Any]] = []
    for event in raw_events:
        shifted = dict(event)
        shifted["time_s"] = float(event["time_s"]) - origin
        if shifted["time_s"] < -1e-6:
            raise ValueError(
                f"{task_id}: gripper event {event['action']} would be removed "
                "by idle trimming"
            )
        if shifted["time_s"] > samples[-1].time_from_start + 1e-6:
            raise ValueError(
                f"{task_id}: gripper event {event['action']} lies after the "
                "trimmed trajectory"
            )
        shifted["time_s"] = max(0.0, shifted["time_s"])
        events.append(shifted)

    return PreparedTask(
        task_id=task_id,
        trajectory=trajectory,
        timeline=timeline,
        samples=samples,
        events=tuple(events),
        settings=settings,
        original_duration_s=original_duration,
        trim_start_s=origin,
        trim_end_s=max(0.0, original_duration - absolute_end),
        annotation_speed=annotation_speed,
        annotation_play_speed=annotation_play_speed,
    )


class ProductionStream:
    """Keep Piper and gripper connections alive across all recorded tasks."""

    def __init__(
        self,
        *,
        can_port: str,
        gripper_port: str | None,
        gripper_baudrate: int,
        gripper_timeout: float,
        gripper_startup_delay: float,
        gripper_feedback: bool,
        dry_run_gripper: bool,
        tracking_error_limit: float,
        tracking_timeout: float,
        boundary_limit: float,
        final_tolerance: float,
        final_timeout: float,
        final_settle: float,
        between_task_delay: float,
        event_position_tolerance: float,
        event_wait_timeout: float,
    ) -> None:
        self.can_port = can_port
        self.gripper = DiyGripper(
            port=gripper_port,
            baudrate=gripper_baudrate,
            timeout=gripper_timeout,
            startup_delay=gripper_startup_delay,
            wait_feedback=gripper_feedback,
            dry_run=dry_run_gripper,
        )
        self.tracking_error_limit = tracking_error_limit
        self.tracking_timeout = tracking_timeout
        self.boundary_limit = boundary_limit
        self.final_tolerance = final_tolerance
        self.final_timeout = final_timeout
        self.final_settle = final_settle
        self.between_task_delay = between_task_delay
        self.event_position_tolerance = event_position_tolerance
        self.event_wait_timeout = event_wait_timeout
        self.interface: Any | None = None
        self.piper: Any | None = None
        self.old_terminal: Any | None = None

    def __enter__(self) -> "ProductionStream":
        _, Piper = import_official_piper()
        self.piper = Piper(self.can_port)
        self.interface = self.piper.init()
        self.piper.connect()
        time.sleep(0.1)
        self.gripper.__enter__()
        self.old_terminal = enter_raw_terminal()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is not None:
            self.emergency_stop()
        restore_terminal(self.old_terminal)
        self.gripper.__exit__(exc_type, exc, traceback)
        if self.interface is not None:
            disconnect = getattr(self.interface, "DisconnectPort", None)
            if callable(disconnect):
                disconnect()

    def emergency_stop(self) -> None:
        if self.interface is None:
            return
        try:
            self.interface.EmergencyStop(0x01)
            print("Official emergency stop sent.")
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: failed to send emergency stop: {exc}")

    def initialize_motion(self, speed: int) -> None:
        if self.interface is None or self.piper is None:
            raise RuntimeError("production stream is not connected")
        ensure_can_mode(self.interface, self.piper, False, speed, 5.0)
        enable_official(self.interface, self.piper, False, speed)

    def replay(self, task: PreparedTask, *, first_task: bool) -> bool:
        if self.interface is None or self.piper is None:
            raise RuntimeError("production stream is not connected")
        samples = list(task.samples)
        settings = task.settings
        events = list(task.events)
        start_error = max_abs_joint_error(
            samples[0].joints, current_joints(self.piper)
        )
        print(
            f"[TASK] {task.task_id}: duration={task.duration_s:.3f}s "
            f"trimmed={task.saved_s:.3f}s start_error={start_error:.6f}rad"
        )
        if start_error > self.boundary_limit:
            raise RuntimeError(
                f"{task.task_id}: current posture differs from its trimmed start "
                f"by {start_error:.6f}rad (limit {self.boundary_limit:.6f}rad)"
            )
        if first_task:
            converge(
                self.interface,
                self.piper,
                samples[0],
                label=f"{task.task_id} start",
                speed=settings.speed,
                have_gripper=False,
                tolerance=min(self.boundary_limit, 0.01),
                timeout=3.0,
                settle=0.05,
                stream_dt=settings.stream_dt,
            )

        fixed_period_ns = max(1, round(settings.stream_dt * 1_000_000_000))
        deadline_ns = time.monotonic_ns()
        trajectory_time = 0.0
        target_index = 0
        target = samples[0]
        fired = 0
        hold_until_ns = 0
        event_wait_since_ns: int | None = None
        paused = False
        recovery_since_ns: int | None = None
        next_print_ns = deadline_ns

        while trajectory_time < samples[-1].time_from_start:
            if settings.clock == "recorded":
                period_ns = recorded_period_ns(
                    samples,
                    target_index,
                    play_speed=settings.play_speed,
                    fallback_dt=settings.stream_dt,
                )
            else:
                period_ns = fixed_period_ns
            now_ns, deadline_ns, _ = wait_next(deadline_ns, period_ns)
            key = read_key()
            if key == "p":
                paused = True
                print("\nPAUSED. Press C=continue or S/Q=stop.")
            elif key == "c":
                paused = False
                recovery_since_ns = None
                print("\nCONTINUE")
            elif key in ("s", "q", "\x03"):
                print("\nSTOP requested.")
                return False

            actual = current_joints(self.piper)
            event_clock = trajectory_time
            waiting_for_event_pose = False
            while fired < len(events) and float(events[fired]["time_s"]) <= trajectory_time:
                event = events[fired]
                event_joints = event.get("joint_rad")
                if event_joints is None:
                    event_joints = sample_linear(
                        samples, float(event["time_s"])
                    ).joints
                event_error = max_abs_joint_error(event_joints, actual)
                if (
                    settings.event_sync == "actual"
                    and event_error > self.event_position_tolerance
                ):
                    waiting_for_event_pose = True
                    target = TrajectorySample(
                        time_from_start=trajectory_time,
                        joints=tuple(float(value) for value in event_joints),
                        gripper=None,
                    )
                    if event_wait_since_ns is None:
                        event_wait_since_ns = now_ns
                        print(
                            f"\nEVENT HOLD: {task.task_id} {event['action']} "
                            f"joint_error={event_error:.6f}rad"
                        )
                    elif (
                        now_ns - event_wait_since_ns
                        > round(self.event_wait_timeout * 1_000_000_000)
                    ):
                        raise RuntimeError(
                            f"{task.task_id}: gripper event {event['action']} "
                            f"position did not converge within "
                            f"{self.event_wait_timeout:.2f}s; "
                            f"last error={event_error:.6f}rad"
                        )
                    break
                self.gripper.send(str(event["action"]))
                print(
                    f"\nAuto gripper {event['action']} "
                    f"task={task.task_id} trajectory_time={trajectory_time:.3f}s "
                    f"event_clock={event_clock:.3f}s "
                    f"event_pose_error={event_error:.6f}rad"
                )
                fired += 1
                event_wait_since_ns = None
                if settings.gripper_action_hold > 0:
                    hold_until_ns = max(
                        hold_until_ns,
                        time.monotonic_ns()
                        + round(settings.gripper_action_hold * 1_000_000_000),
                    )

            send_target(
                self.interface,
                self.piper,
                target,
                speed=settings.speed,
                have_gripper=False,
            )
            error = max_abs_joint_error(target.joints, actual)
            blocked = (
                self.tracking_error_limit > 0
                and error > self.tracking_error_limit * 2.5
            )
            if blocked:
                if recovery_since_ns is None:
                    recovery_since_ns = now_ns
                    print(f"\nTRACKING HOLD: error={error:.6f}rad")
                elif (
                    now_ns - recovery_since_ns
                    > round(self.tracking_timeout * 1_000_000_000)
                ):
                    raise RuntimeError(
                        f"{task.task_id}: tracking error did not recover"
                    )
            else:
                recovery_since_ns = None
                if (
                    not paused
                    and not waiting_for_event_pose
                    and now_ns >= hold_until_ns
                ):
                    if settings.clock == "recorded":
                        if target_index + 1 < len(samples):
                            target_index += 1
                            target = samples[target_index]
                            trajectory_time = target.time_from_start
                        else:
                            trajectory_time = samples[-1].time_from_start
                    else:
                        trajectory_time = min(
                            samples[-1].time_from_start,
                            trajectory_time
                            + settings.stream_dt * settings.play_speed,
                        )
                        target = sample_linear(samples, trajectory_time)

            if now_ns >= next_print_ns:
                state = "PAUSED" if paused else "RUN"
                print(
                    f"INFO: {state} {task.task_id} "
                    f"t={trajectory_time:.3f}/{samples[-1].time_from_start:.3f}s "
                    f"events={fired}/{len(events)} error={error:.6f}rad"
                )
                next_print_ns = now_ns + 500_000_000

        if fired != len(events):
            raise RuntimeError(
                f"{task.task_id}: only fired {fired}/{len(events)} gripper events"
            )
        converge(
            self.interface,
            self.piper,
            samples[-1],
            label=f"{task.task_id} boundary",
            speed=settings.speed,
            have_gripper=False,
            tolerance=self.final_tolerance,
            timeout=self.final_timeout,
            settle=self.final_settle,
            stream_dt=settings.stream_dt,
        )
        if self.between_task_delay > 0:
            deadline = time.monotonic() + self.between_task_delay
            while time.monotonic() < deadline:
                send_target(
                    self.interface,
                    self.piper,
                    samples[-1],
                    speed=settings.speed,
                    have_gripper=False,
                )
                time.sleep(settings.stream_dt)
        print(
            f"[DONE] {task.task_id}: gripper_events={fired}/{len(events)}"
        )
        return True
