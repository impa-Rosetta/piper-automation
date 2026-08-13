# Data Format / 数据格式

## Trajectory

The main trajectory file has no header to preserve compatibility with the
AgileX teach/replay example:

```text
delta_time_s,j1_rad,j2_rad,j3_rad,j4_rad,j5_rad,j6_rad[,gripper]
```

- `delta_time_s` is the elapsed time since the previously written sample.
- Joint angles are radians returned by the official high-level SDK.
- The first row has a zero delta and explicitly records the start state.
- The production recorder defaults to `0.005 s` (200 Hz).
- Static frames are preserved so dwell periods remain reproducible.

主轨迹文件为了兼容官方示教示例而不包含表头。第一列是相邻帧时间差，六个关节角
单位为弧度。首行显式保存起点；静止帧也会保留，从而还原停顿和夹爪动作窗口。

## Timestamp sidecar

The precise recorder creates `trajectory.csv.timestamps.csv` with scheduling
and capture timing diagnostics. It is useful for measuring jitter and matching
third-person video or external telemetry.

精准录制器会额外生成时间戳 sidecar，用于分析采样抖动，以及和第三视角视频或其他
传感器日志进行时间对齐。

## Gripper timeline

The STM32 gripper has two actions: `close` and `open`. Events are stored against
trajectory time, not frame index:

```json
{
  "format": "piper_diy_gripper_timeline_v1",
  "events": [
    {"time_s": 4.215, "action": "close"},
    {"time_s": 12.680, "action": "open"}
  ]
}
```

Production uses `event_sync=actual`. If the arm is temporarily behind schedule,
the event waits for actual trajectory progress instead of firing early.

生产模式默认使用 `event_sync=actual`。如果机械臂短暂落后于计划时间，夹爪事件会
等待实际轨迹进度，不会因为墙上时钟已经到点而提前触发。

## Task manifest

`task.json` binds one tray address to its motion and gripper files, plus the
validated replay settings. See `examples/production_tasks/` for a safe example
without physical coordinates.

`task.json` 将层号、孔位、轨迹、夹爪时间线和已验证的回放参数绑定为一个生产任务。
安全示例位于 `examples/production_tasks/`，不包含任何真实机械臂坐标。
