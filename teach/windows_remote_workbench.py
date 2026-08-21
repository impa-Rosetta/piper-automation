#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows operator workstation for a Piper controller running on Raspberry Pi.

The Windows process never opens CAN or the STM32 serial port. All hardware
commands are executed on the Raspberry Pi through OpenSSH, while task files can
be synchronized in both directions.
"""

from __future__ import annotations

import csv
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk
from typing import Callable, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_ROOT = PROJECT_ROOT / "teach" / "production_tasks"
CONFIG_PATH = PROJECT_ROOT / "config" / "windows_remote_workbench.json"
FEEDER_ABOVE = "teach/feeder_above.json"
FULL_LOG_DIR = "records/full_status_logs"
DIY_GRIPPER_STATE_FILE = "records/diy_gripper_state.json"
TASK_FORMAT = "piper_field_task_v1"
DEFAULT_PI_USER = "piper"
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

SYNC_ROOTS = (
    "teach",
    "scripts",
    "gripper",
    "config",
)
SYNC_FILES = (
    "requirements.txt",
    "README.md",
    "README.zh-CN.md",
)
SYNC_EXCLUDED_PARTS = {
    "__pycache__",
    ".git",
    ".venv",
    ".venv-wsl",
}
SYNC_EXCLUDED_SUFFIXES = {
    ".avi",
    ".bmp",
    ".jpeg",
    ".jpg",
    ".mkv",
    ".mp4",
    ".png",
}
PULL_PATHS = (
    "teach/production_tasks",
    "teach/trajectories",
    "teach/gripper_timelines",
    "teach/feeder_above.json",
    "teach/zero_home.json",
    "records/full_status_logs",
    "records/diy_gripper_state.json",
)


def task_id(layer: int, slot: int) -> str:
    return f"layer_{layer:02d}_slot_{slot:02d}"


def task_paths(task_name: str) -> tuple[str, str]:
    root = f"teach/production_tasks/{task_name}"
    return f"{root}/trajectory.csv", f"{root}/gripper_timeline.json"


def inspect_trajectory(path: Path) -> tuple[int, float]:
    if not path.exists():
        return 0, 0.0
    rows = 0
    duration = 0.0
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            for row in csv.reader(stream):
                if not row:
                    continue
                duration += float(row[0])
                rows += 1
    except (OSError, ValueError):
        return -1, 0.0
    return rows, duration


def inspect_timeline(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return len(data.get("events", []))
    except (OSError, json.JSONDecodeError):
        return -1


def safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if destination != target and destination not in target.parents:
                raise ValueError(f"Unsafe archive member: {member.name}")
        bundle.extractall(destination)


class RemoteWorkbench(tk.Tk):
    BG = "#eef2f5"
    PANEL = "#ffffff"
    INK = "#17212b"
    MUTED = "#667581"
    BLUE = "#1769aa"
    GREEN = "#16724b"
    AMBER = "#9b5d00"
    RED = "#b42318"

    def __init__(self) -> None:
        super().__init__()
        self.title("Piper Windows 远程工作台")
        self.geometry("1280x850")
        self.minsize(1120, 730)
        self.configure(bg=self.BG)

        saved = self.load_config()
        self.host = tk.StringVar(value=saved.get("host", "piper-pi"))
        self.remote_root = tk.StringVar(
            value=saved.get("remote_root", "/home/piper/piper-automation")
        )
        self.can_port = tk.StringVar(value=saved.get("can_port", "can0"))
        self.gripper_port = tk.StringVar(
            value=saved.get("gripper_port", "/dev/piper_gripper")
        )
        self.layer = tk.IntVar(value=int(saved.get("layer", 1)))
        self.slot = tk.IntVar(value=int(saved.get("slot", 1)))
        self.sequence_from = tk.IntVar(value=int(saved.get("sequence_from", 1)))
        self.sequence_to = tk.IntVar(value=int(saved.get("sequence_to", 4)))
        self.speed = tk.IntVar(value=int(saved.get("speed", 10)))
        self.play_speed = tk.DoubleVar(value=float(saved.get("play_speed", 1.0)))
        self.anchor_speed = tk.IntVar(value=int(saved.get("anchor_speed", 30)))
        self.selected_task: str | None = None
        self.busy_count = 0
        session_log_dir = PROJECT_ROOT / "records" / "workbench_logs"
        session_log_dir.mkdir(parents=True, exist_ok=True)
        self.session_log_path = session_log_dir / (
            "windows_workbench_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".log"
        )

        self.configure_style()
        self.build_ui()
        self.refresh_tasks()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log(
            "Windows 只负责操作与文件管理；CAN 和夹爪串口始终由树莓派控制。"
        )
        self.log(f"本次工作台日志：{self.session_log_path}")

    def load_config(self) -> dict[str, object]:
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_config(self) -> None:
        data = {
            "host": self.host.get().strip(),
            "remote_root": self.remote_root.get().strip(),
            "can_port": self.can_port.get().strip(),
            "gripper_port": self.gripper_port.get().strip(),
            "layer": self.layer.get(),
            "slot": self.slot.get(),
            "sequence_from": self.sequence_from.get(),
            "sequence_to": self.sequence_to.get(),
            "speed": self.speed.get(),
            "play_speed": self.play_speed.get(),
            "anchor_speed": self.anchor_speed.get(),
        }
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def configure_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("TLabel", background=self.BG, foreground=self.INK)
        style.configure("Panel.TLabel", background=self.PANEL, foreground=self.INK)
        style.configure(
            "Title.TLabel",
            background=self.BG,
            foreground=self.INK,
            font=("Microsoft YaHei UI", 20, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=self.BG,
            foreground=self.MUTED,
            font=("Microsoft YaHei UI", 10),
        )
        style.configure(
            "Primary.TButton",
            background=self.BLUE,
            foreground="white",
            padding=(11, 7),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map("Primary.TButton", background=[("active", "#10578d")])
        style.configure("Action.TButton", padding=(9, 6))
        style.configure("Danger.TButton", foreground=self.RED, padding=(9, 6))
        style.configure("Treeview", rowheight=28, background="white")
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    def build_ui(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill="x", padx=18, pady=(15, 9))
        ttk.Label(header, text="Piper Windows 远程工作台", style="Title.TLabel").pack(
            side="left"
        )
        ttk.Label(
            header,
            text="Windows 操作台  |  树莓派实时控制  |  双向任务同步",
            style="Subtitle.TLabel",
        ).pack(side="left", padx=16, pady=(8, 0))
        self.connection_state = ttk.Label(
            header, text="尚未检查连接", foreground=self.AMBER
        )
        self.connection_state.pack(side="right", pady=(8, 0))

        connection = ttk.Frame(self, style="Panel.TFrame", padding=12)
        connection.pack(fill="x", padx=18, pady=(0, 10))
        self.field(connection, "SSH 主机", self.host, 0, 0, 18)
        self.field(connection, "远程项目", self.remote_root, 0, 2, 34)
        self.field(connection, "CAN", self.can_port, 0, 4, 9)
        self.field(connection, "夹爪端口", self.gripper_port, 0, 6, 18)
        ttk.Button(
            connection,
            text="连接与设备检查",
            style="Primary.TButton",
            command=self.check_remote,
        ).grid(row=0, column=8, padx=(12, 5), sticky="ew")
        ttk.Button(
            connection,
            text="首次配置免密 SSH",
            style="Action.TButton",
            command=self.configure_passwordless_ssh,
        ).grid(row=1, column=8, padx=(12, 5), pady=(6, 0), sticky="ew")
        ttk.Button(
            connection,
            text="打开 SSH 终端",
            style="Action.TButton",
            command=self.open_shell,
        ).grid(row=0, column=9, padx=5, sticky="ew")
        ttk.Button(
            connection,
            text="软件停止",
            style="Danger.TButton",
            command=self.software_stop,
        ).grid(row=0, column=10, padx=(5, 0), sticky="ew")

        content = ttk.Frame(self)
        content.pack(fill="both", expand=True, padx=18)
        content.columnconfigure(0, weight=5)
        content.columnconfigure(1, weight=4)
        content.rowconfigure(0, weight=1)

        left = ttk.Frame(content, style="Panel.TFrame", padding=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        right = ttk.Frame(content, style="Panel.TFrame", padding=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        self.build_task_panel(left)
        self.build_action_panel_v2(right)

        log_panel = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        log_panel.pack(fill="both", padx=18, pady=10)
        top = ttk.Frame(log_panel, style="Panel.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="运行日志", style="Panel.TLabel").pack(side="left")
        ttk.Button(
            top,
            text="打开日志文件夹",
            style="Action.TButton",
            command=self.open_log_folder,
        ).pack(side="right", padx=(6, 0))
        ttk.Button(
            top, text="清空", command=lambda: self.log_box.delete("1.0", "end")
        ).pack(side="right")
        self.log_box = tk.Text(
            log_panel,
            height=9,
            bg="#101820",
            fg="#dce8ee",
            insertbackground="white",
            relief="flat",
            font=("Consolas", 9),
            wrap="word",
        )
        self.log_box.pack(fill="both", expand=True, pady=(6, 0))

    def field(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.Variable,
        row: int,
        column: int,
        width: int,
    ) -> None:
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(
            row=row, column=column, padx=(0, 5), sticky="w"
        )
        ttk.Entry(parent, textvariable=variable, width=width).grid(
            row=row, column=column + 1, padx=(0, 10), sticky="ew"
        )

    def build_task_panel(self, parent: ttk.Frame) -> None:
        title = ttk.Frame(parent, style="Panel.TFrame")
        title.pack(fill="x")
        ttk.Label(
            title,
            text="本机生产任务",
            style="Panel.TLabel",
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side="left")
        ttk.Button(title, text="刷新", command=self.refresh_tasks).pack(side="right")

        create = ttk.Frame(parent, style="Panel.TFrame")
        create.pack(fill="x", pady=(10, 8))
        ttk.Label(create, text="层", style="Panel.TLabel").pack(side="left")
        ttk.Spinbox(create, from_=1, to=99, textvariable=self.layer, width=5).pack(
            side="left", padx=(4, 12)
        )
        ttk.Label(create, text="孔位", style="Panel.TLabel").pack(side="left")
        ttk.Spinbox(create, from_=1, to=27, textvariable=self.slot, width=5).pack(
            side="left", padx=(4, 12)
        )
        ttk.Button(
            create,
            text="创建/选择任务",
            style="Action.TButton",
            command=self.create_task,
        ).pack(side="left")

        columns = ("layer", "slot", "trajectory", "events", "status")
        self.task_tree = ttk.Treeview(
            parent, columns=columns, show="headings", selectmode="browse"
        )
        headings = {
            "layer": "层",
            "slot": "孔位",
            "trajectory": "轨迹",
            "events": "夹爪事件",
            "status": "状态",
        }
        widths = {
            "layer": 48,
            "slot": 55,
            "trajectory": 145,
            "events": 80,
            "status": 95,
        }
        for name in columns:
            self.task_tree.heading(name, text=headings[name])
            self.task_tree.column(name, width=widths[name], anchor="center")
        self.task_tree.pack(fill="both", expand=True)
        self.task_tree.bind("<<TreeviewSelect>>", self.on_task_selected)
        self.selected_label = ttk.Label(
            parent, text="未选择任务", style="Panel.TLabel", foreground=self.MUTED
        )
        self.selected_label.pack(fill="x", pady=(7, 0))

    def build_action_panel(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        notebook.pack(fill="both", expand=True)
        teach_tab = ttk.Frame(notebook, style="Panel.TFrame", padding=10)
        production_tab = ttk.Frame(notebook, style="Panel.TFrame", padding=10)
        sync_tab = ttk.Frame(notebook, style="Panel.TFrame", padding=10)
        notebook.add(teach_tab, text="示教与调试")
        notebook.add(production_tab, text="连续生产")
        notebook.add(sync_tab, text="同步部署")

        self.section(teach_tab, "基础动作")
        row = ttk.Frame(teach_tab, style="Panel.TFrame")
        row.pack(fill="x", pady=(4, 10))
        self.button(row, "夹爪张开", lambda: self.gripper_action("open"))
        self.button(row, "夹爪闭合", lambda: self.gripper_action("close"))
        self.button(row, "回给料上方", self.go_feeder)
        self.button(row, "回全零 Home", self.go_zero_home)

        self.section(teach_tab, "选中任务工作流")
        ttk.Button(
            teach_tab,
            text="1. 录制/覆盖给料上方点",
            style="Action.TButton",
            command=self.record_feeder,
        ).pack(fill="x", pady=3)
        ttk.Button(
            teach_tab,
            text="2. 录制机械臂完整轨迹（H 返回给料上方）",
            style="Action.TButton",
            command=self.record_trajectory,
        ).pack(fill="x", pady=3)
        ttk.Button(
            teach_tab,
            text="3. 回放并标注夹爪动作（P/G/O/C）",
            style="Action.TButton",
            command=self.record_timeline,
        ).pack(fill="x", pady=3)
        ttk.Button(
            teach_tab,
            text="4. 联合回放选中任务",
            style="Primary.TButton",
            command=self.replay_selected,
        ).pack(fill="x", pady=(3, 10))
        ttk.Label(
            teach_tab,
            text=(
                "交互命令会在独立 Windows 控制台中运行。\n"
                "远程录制完成后，点击“从树莓派拉回现场数据”。"
            ),
            style="Panel.TLabel",
            foreground=self.MUTED,
            justify="left",
        ).pack(fill="x")

        self.section(production_tab, "运行范围")
        sequence = ttk.Frame(production_tab, style="Panel.TFrame")
        sequence.pack(fill="x", pady=(5, 8))
        for label, variable in (
            ("层", self.layer),
            ("起始孔", self.sequence_from),
            ("结束孔", self.sequence_to),
        ):
            ttk.Label(sequence, text=label, style="Panel.TLabel").pack(side="left")
            upper = 99 if label == "层" else 27
            ttk.Spinbox(
                sequence, from_=1, to=upper, textvariable=variable, width=5
            ).pack(side="left", padx=(4, 10))

        settings = ttk.Frame(production_tab, style="Panel.TFrame")
        settings.pack(fill="x", pady=(0, 10))
        ttk.Label(settings, text="调试速度%", style="Panel.TLabel").pack(side="left")
        ttk.Spinbox(
            settings, from_=1, to=100, textvariable=self.speed, width=6
        ).pack(side="left", padx=(4, 12))
        ttk.Label(settings, text="回放倍率", style="Panel.TLabel").pack(side="left")
        ttk.Spinbox(
            settings,
            from_=0.1,
            to=5.0,
            increment=0.1,
            textvariable=self.play_speed,
            width=6,
        ).pack(side="left", padx=(4, 12))
        ttk.Label(settings, text="锚点速度%", style="Panel.TLabel").pack(side="left")
        ttk.Spinbox(
            settings, from_=1, to=30, textvariable=self.anchor_speed, width=6
        ).pack(side="left", padx=(4, 0))

        ttk.Button(
            production_tab,
            text="只检查连续任务衔接（不控制硬件）",
            style="Action.TButton",
            command=lambda: self.run_sequence(dry_run=True),
        ).pack(fill="x", pady=4)
        ttk.Button(
            production_tab,
            text="连续运行本层任务",
            style="Primary.TButton",
            command=lambda: self.run_sequence(dry_run=False),
        ).pack(fill="x", pady=4)
        ttk.Label(
            production_tab,
            text=(
                "默认读取每个 task.json 中已经验证过的独立速度参数。\n"
                "调试速度仅用于单任务联合回放，不覆盖连续生产配置。"
            ),
            style="Panel.TLabel",
            foreground=self.MUTED,
            justify="left",
        ).pack(fill="x", pady=(10, 0))

        self.section(sync_tab, "Windows ↔ Raspberry Pi")
        ttk.Button(
            sync_tab,
            text="一键同步本机程序与任务到树莓派",
            style="Primary.TButton",
            command=self.push_project,
        ).pack(fill="x", pady=4)
        ttk.Button(
            sync_tab,
            text="从树莓派拉回现场示教数据",
            style="Action.TButton",
            command=self.pull_data,
        ).pack(fill="x", pady=4)
        ttk.Label(
            sync_tab,
            text=(
                "下发：以 Windows 当前文件覆盖树莓派同名文件。\n"
                "拉回：以树莓派现场录制数据覆盖 Windows 同名文件。\n\n"
                "建议流程：下发程序 → 远程录制 → 拉回数据 → 再备份。"
            ),
            style="Panel.TLabel",
            foreground=self.MUTED,
            justify="left",
        ).pack(fill="x", pady=(10, 0))

    def build_action_panel_v2(self, parent: ttk.Frame) -> None:
        """Build the streamlined field workflow used by the Windows operator."""
        notebook = ttk.Notebook(parent)
        notebook.pack(fill="both", expand=True)
        teach_tab = ttk.Frame(notebook, style="Panel.TFrame", padding=10)
        production_tab = ttk.Frame(notebook, style="Panel.TFrame", padding=10)
        sync_tab = ttk.Frame(notebook, style="Panel.TFrame", padding=10)
        notebook.add(teach_tab, text="示教与验证")
        notebook.add(production_tab, text="连续生产")
        notebook.add(sync_tab, text="同步部署")

        self.section(teach_tab, "设备与公共点位")
        power = ttk.Frame(teach_tab, style="Panel.TFrame")
        power.pack(fill="x", pady=(4, 6))
        ttk.Button(
            power,
            text="机械臂使能/恢复",
            style="Primary.TButton",
            command=self.arm_enable,
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(
            power,
            text="机械臂失能",
            style="Danger.TButton",
            command=self.arm_disable,
        ).pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.arm_power_state = ttk.Label(
            teach_tab,
            text="机械臂使能状态待检查（失能前请扶稳机械臂）",
            style="Panel.TLabel",
            foreground=self.MUTED,
        )
        self.arm_power_state.pack(fill="x", pady=(0, 6))

        quick = ttk.Frame(teach_tab, style="Panel.TFrame")
        quick.pack(fill="x", pady=(4, 6))
        self.button(quick, "夹爪张开", lambda: self.gripper_action_quick("open"))
        self.button(quick, "夹爪闭合", lambda: self.gripper_action_quick("close"))
        self.button(quick, "回给料上方", self.go_feeder)
        self.button(quick, "回全零 Home", self.go_zero_home)
        self.gripper_state = ttk.Label(
            teach_tab,
            text="夹爪待命",
            style="Panel.TLabel",
            foreground=self.MUTED,
        )
        self.gripper_state.pack(fill="x", pady=(0, 6))
        ttk.Button(
            teach_tab,
            text="录制/覆盖公共给料上方点",
            style="Action.TButton",
            command=self.record_feeder,
        ).pack(fill="x", pady=(0, 12))

        full_log = ttk.LabelFrame(
            teach_tab,
            text="完整状态数据记录（机械臂 + DIY 夹爪命令状态）",
            padding=8,
        )
        full_log.pack(fill="x", pady=(0, 12))
        full_log.columnconfigure(0, weight=1)
        full_log.columnconfigure(1, weight=1)
        ttk.Button(
            full_log,
            text="开始 200 Hz 完整记录",
            style="Primary.TButton",
            command=self.start_full_log,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(
            full_log,
            text="拉回记录并打开文件夹",
            style="Action.TButton",
            command=self.pull_full_logs_and_open,
        ).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Label(
            full_log,
            text="独立终端中按 Ctrl+C 停止；每次生成新的时间戳 CSV，不覆盖旧文件。",
            style="Panel.TLabel",
            foreground=self.MUTED,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 0))

        self.section(teach_tab, "选中任务 A / B / C / D")
        steps = [
            (
                "A",
                "录制机械臂完整轨迹",
                "200 Hz；按 H 自动返回给料上方并保存",
                self.record_trajectory,
            ),
            (
                "B",
                "回放并标注夹爪动作",
                "P 暂停，G 闭合，O 打开，C 继续",
                self.record_timeline,
            ),
            (
                "C",
                "机械臂 + 夹爪联合试跑",
                "按原始 CSV 与夹爪时序进行实机验证",
                self.replay_selected,
            ),
            (
                "D",
                "拉回、检查并保存任务",
                "保存新轨迹到 Windows，并检查采样与夹爪事件",
                self.validate_selected,
            ),
        ]
        workflow = ttk.Frame(teach_tab, style="Panel.TFrame")
        workflow.pack(fill="x")
        for index, (badge, title, subtitle, command) in enumerate(steps):
            card = ttk.Frame(workflow, style="Panel.TFrame", padding=(7, 8))
            card.grid(row=index, column=0, sticky="ew", pady=2)
            ttk.Label(
                card,
                text=badge,
                background=self.BLUE,
                foreground="white",
                font=("Microsoft YaHei UI", 11, "bold"),
                width=3,
                anchor="center",
            ).grid(row=0, column=0, rowspan=2, padx=(0, 9), sticky="ns")
            ttk.Label(
                card,
                text=title,
                style="Panel.TLabel",
                font=("Microsoft YaHei UI", 10, "bold"),
            ).grid(row=0, column=1, sticky="w")
            ttk.Label(
                card,
                text=subtitle,
                style="Panel.TLabel",
                foreground=self.MUTED,
            ).grid(row=1, column=1, sticky="w", pady=(2, 0))
            ttk.Button(card, text="开始", command=command).grid(
                row=0, column=2, rowspan=2, padx=(10, 0), sticky="e"
            )
            card.columnconfigure(1, weight=1)
        workflow.columnconfigure(0, weight=1)
        ttk.Label(
            teach_tab,
            text=(
                "A/B/C 在树莓派独立终端运行；D 自动拉回当前任务，"
                "无需再到同步页手动保存。"
            ),
            style="Panel.TLabel",
            foreground=self.MUTED,
            justify="left",
        ).pack(fill="x", pady=(8, 0))

        self.section(production_tab, "运行范围")
        sequence = ttk.Frame(production_tab, style="Panel.TFrame")
        sequence.pack(fill="x", pady=(5, 8))
        for label, variable, upper in (
            ("层", self.layer, 99),
            ("起始孔", self.sequence_from, 27),
            ("结束孔", self.sequence_to, 27),
        ):
            ttk.Label(sequence, text=label, style="Panel.TLabel").pack(side="left")
            ttk.Spinbox(
                sequence, from_=1, to=upper, textvariable=variable, width=5
            ).pack(side="left", padx=(4, 10))

        settings = ttk.Frame(production_tab, style="Panel.TFrame")
        settings.pack(fill="x", pady=(0, 10))
        ttk.Label(settings, text="调试速度%", style="Panel.TLabel").pack(side="left")
        ttk.Spinbox(
            settings, from_=1, to=100, textvariable=self.speed, width=6
        ).pack(side="left", padx=(4, 12))
        ttk.Label(settings, text="回放倍率", style="Panel.TLabel").pack(side="left")
        ttk.Spinbox(
            settings,
            from_=0.1,
            to=5.0,
            increment=0.1,
            textvariable=self.play_speed,
            width=6,
        ).pack(side="left", padx=(4, 12))
        ttk.Label(settings, text="锚点速度%", style="Panel.TLabel").pack(side="left")
        ttk.Spinbox(
            settings, from_=1, to=30, textvariable=self.anchor_speed, width=6
        ).pack(side="left", padx=(4, 0))

        ttk.Button(
            production_tab,
            text="只检查连续任务衔接（不控制硬件）",
            style="Action.TButton",
            command=lambda: self.run_sequence(dry_run=True),
        ).pack(fill="x", pady=4)
        ttk.Button(
            production_tab,
            text="连续运行本层任务",
            style="Primary.TButton",
            command=lambda: self.run_sequence(dry_run=False),
        ).pack(fill="x", pady=4)
        ttk.Label(
            production_tab,
            text=(
                "默认读取每个 task.json 中已经验证过的独立速度参数。\n"
                "调试速度只用于单任务联合回放，不覆盖连续生产配置。"
            ),
            style="Panel.TLabel",
            foreground=self.MUTED,
            justify="left",
        ).pack(fill="x", pady=(10, 0))

        self.section(sync_tab, "Windows → Raspberry Pi")
        ttk.Button(
            sync_tab,
            text="一键同步本机程序与任务到树莓派",
            style="Primary.TButton",
            command=self.push_project,
        ).pack(fill="x", pady=4)
        ttk.Button(
            sync_tab,
            text="从树莓派拉回全部现场示教数据",
            style="Action.TButton",
            command=self.pull_data,
        ).pack(fill="x", pady=4)
        ttk.Label(
            sync_tab,
            text=(
                "下发：以 Windows 当前文件覆盖树莓派同名文件。\n"
                "拉回：以树莓派现场数据覆盖 Windows 同名文件。\n\n"
                "单个任务完成 A/B/C 后，直接使用步骤 D 保存；这里用于批量同步。"
            ),
            style="Panel.TLabel",
            foreground=self.MUTED,
            justify="left",
        ).pack(fill="x", pady=(10, 0))

    def section(self, parent: ttk.Frame, text: str) -> None:
        ttk.Label(
            parent,
            text=text,
            style="Panel.TLabel",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(fill="x", pady=(2, 5))

    def button(
        self, parent: ttk.Frame, text: str, command: Callable[[], None]
    ) -> None:
        ttk.Button(
            parent, text=text, style="Action.TButton", command=command
        ).pack(side="left", padx=(0, 5))

    def log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {text}\n"
        self.log_box.insert("end", line)
        self.log_box.see("end")
        try:
            with self.session_log_path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        except OSError:
            pass

    def open_log_folder(self) -> None:
        try:
            os.startfile(self.session_log_path.parent)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开日志目录", str(exc))

    def set_busy(self, active: bool) -> None:
        self.busy_count += 1 if active else -1
        self.busy_count = max(0, self.busy_count)
        if self.busy_count:
            self.connection_state.configure(text="正在执行...", foreground=self.AMBER)

    def host_name(self) -> str:
        value = self.host.get().strip()
        if not value:
            raise ValueError("SSH 主机不能为空")
        # OpenSSH otherwise falls back to the current Windows account name
        # (for example, administrator), which is rarely the Raspberry Pi user.
        return value if "@" in value else f"{DEFAULT_PI_USER}@{value}"

    def remote_project(self) -> str:
        value = self.remote_root.get().strip()
        if not value.startswith("/"):
            raise ValueError("远程项目必须是绝对 Linux 路径")
        return value.rstrip("/")

    def remote_prefix(self) -> str:
        root = shlex.quote(self.remote_project())
        return (
            f"cd {root} && "
            "source ~/.venvs/piper_robot_project_api/bin/activate && "
        )

    def remote_python(self, module: str, *arguments: object) -> str:
        command = ["python", "-m", module, *(str(item) for item in arguments)]
        return self.remote_prefix() + shlex.join(command)

    def run_capture(
        self,
        remote_command: str,
        title: str,
        on_complete: Callable[[int], None] | None = None,
    ) -> None:
        try:
            host = self.host_name()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.save_config()
        self.log(f"{title}: ssh {host} ...")
        self.set_busy(True)

        def worker() -> None:
            try:
                process = subprocess.Popen(
                    ["ssh", "-o", "BatchMode=yes", host, remote_command],
                    cwd=PROJECT_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    self.after(0, self.log, line.rstrip())
                code = process.wait()
                if on_complete is not None:
                    self.after(0, on_complete, code)
                self.after(0, self.log, f"{title} 结束，returncode={code}")
                if title == "连接与设备检查":
                    self.after(
                        0,
                        self.connection_state.configure,
                        {
                            "text": "树莓派在线" if code == 0 else "检查失败",
                            "foreground": self.GREEN if code == 0 else self.RED,
                        },
                    )
            except OSError as exc:
                if on_complete is not None:
                    self.after(0, on_complete, -1)
                self.after(0, self.log, f"{title} ERROR: {exc}")
            finally:
                self.after(0, self.set_busy, False)

        threading.Thread(target=worker, daemon=True).start()

    def run_terminal(self, remote_command: str, title: str) -> None:
        try:
            host = self.host_name()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.save_config()
        shell = (
            f"{remote_command}; result=$?; echo; "
            f"echo '{title} finished, returncode='$result; "
            "read -r -p 'Press Enter to close this terminal...'; exit $result"
        )
        try:
            subprocess.Popen(
                ["ssh", "-tt", host, shell],
                cwd=PROJECT_ROOT,
                creationflags=CREATE_NEW_CONSOLE,
            )
            self.log(f"已打开独立终端：{title}")
        except OSError as exc:
            messagebox.showerror("无法启动 SSH", str(exc))

    def open_shell(self) -> None:
        try:
            subprocess.Popen(
                ["ssh", "-tt", self.host_name()],
                cwd=PROJECT_ROOT,
                creationflags=CREATE_NEW_CONSOLE,
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法启动 SSH", str(exc))

    def configure_passwordless_ssh(self) -> None:
        raw_host = self.host.get().strip()
        if not raw_host:
            messagebox.showerror("配置错误", "请先填写树莓派 IP 或主机名。")
            return
        if "@" in raw_host:
            pi_user, pi_address = raw_host.split("@", 1)
        else:
            pi_user, pi_address = DEFAULT_PI_USER, raw_host
        if not pi_user or not pi_address:
            messagebox.showerror("配置错误", "SSH 主机格式应为 IP、主机名或 用户@主机。")
            return
        if not messagebox.askyesno(
            "配置免密 SSH",
            "将为当前 Windows 账户创建 SSH 密钥，并安装到树莓派。\n"
            "新终端中只需输入一次树莓派 Linux 登录密码。\n\n"
            f"目标：{pi_user}@{pi_address}\n确认继续吗？",
        ):
            return
        script = PROJECT_ROOT / "scripts" / "configure_windows_workstation.ps1"
        command = [
            "powershell.exe",
            "-NoExit",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-PiAddress",
            pi_address,
            "-PiUser",
            pi_user,
            "-Alias",
            "piper-pi",
            "-RemoteRoot",
            self.remote_project(),
        ]
        try:
            subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                creationflags=CREATE_NEW_CONSOLE,
            )
            self.log("已打开免密 SSH 配置终端；按提示输入一次树莓派密码。")
        except OSError as exc:
            messagebox.showerror("无法启动配置程序", str(exc))

    def check_remote(self) -> None:
        command = (
            "check_rc=0; "
            "echo '=== SSH target ==='; "
            "printf 'user='; whoami; printf 'host='; hostname; "
            "printf 'ip='; hostname -I; "
            "echo '=== Project ==='; "
            + self.remote_prefix()
            + "git rev-parse --short HEAD 2>/dev/null || check_rc=1; "
            "echo '=== USB-CAN ==='; "
            "lsusb | grep -E '1d50:606f|CAN|OpenMoko' || check_rc=1; "
            "echo '=== CAN service/interface ==='; "
            "systemctl is-active can0.service || check_rc=1; "
            f"ip -details -statistics link show {shlex.quote(self.can_port.get())} "
            "|| check_rc=1; "
            "echo '=== Gripper ==='; id; "
            f"ls -l {shlex.quote(self.gripper_port.get())} || check_rc=1; "
            f"test -r {shlex.quote(self.gripper_port.get())} "
            f"-a -w {shlex.quote(self.gripper_port.get())} "
            "&& echo 'gripper_access=read-write' "
            "|| { echo 'gripper_access=FAILED'; check_rc=1; }; "
            "echo '=== Piper feedback (3 s) ==='; "
            + self.remote_prefix()
            + "timeout 3 python -u scripts/read_status.py "
            f"--can-port {shlex.quote(self.can_port.get())}; "
            "feedback_rc=$?; "
            "if [ $feedback_rc -ne 0 ] && [ $feedback_rc -ne 124 ]; "
            "then check_rc=1; fi; exit $check_rc"
        )
        self.run_capture(command, "连接与设备检查")

    def software_stop(self) -> None:
        if not messagebox.askyesno(
            "软件停止",
            "将向 Piper 发送官方停止命令。\n"
            "软件停止不能替代物理急停，确认发送吗？",
        ):
            return
        command = self.remote_python(
            "teach.piper_power_control",
            "--can-port",
            self.can_port.get(),
            "--action",
            "stop",
        )
        self.run_capture(command, "软件停止")

    def arm_enable(self) -> None:
        self.arm_power_state.configure(
            text="正在发送官方恢复与使能命令...",
            foreground=self.AMBER,
        )
        command = self.remote_python(
            "teach.piper_power_control",
            "--can-port",
            self.can_port.get(),
            "--action",
            "enable",
            "--reset-first",
            "--timeout",
            8,
        )
        self.run_capture(
            command,
            "机械臂使能/恢复",
            lambda code: self.arm_power_done("enable", code),
        )

    def arm_disable(self) -> None:
        self.arm_power_state.configure(
            text="正在发送官方失能命令，请扶稳机械臂...",
            foreground=self.AMBER,
        )
        command = self.remote_python(
            "teach.piper_power_control",
            "--can-port",
            self.can_port.get(),
            "--action",
            "disable",
            "--timeout",
            8,
        )
        self.run_capture(
            command,
            "机械臂失能",
            lambda code: self.arm_power_done("disable", code),
        )

    def arm_power_done(self, action: str, return_code: int) -> None:
        if return_code == 0:
            enabled = action == "enable"
            self.arm_power_state.configure(
                text="机械臂已使能" if enabled else "机械臂已失能，请扶稳机械臂",
                foreground=self.GREEN if enabled else self.RED,
            )
            return
        self.arm_power_state.configure(
            text="使能/失能失败，请检查 CAN 与运行日志",
            foreground=self.RED,
        )

    def create_task(self) -> None:
        layer = int(self.layer.get())
        slot = int(self.slot.get())
        if layer < 1 or not 1 <= slot <= 27:
            messagebox.showerror("参数错误", "层号必须大于0，孔位必须在1～27。")
            return
        name = task_id(layer, slot)
        directory = TASK_ROOT / name
        directory.mkdir(parents=True, exist_ok=True)
        trajectory, timeline = task_paths(name)
        manifest_path = directory / "task.json"
        if not manifest_path.exists():
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            manifest = {
                "format": TASK_FORMAT,
                "task_id": name,
                "created_at": now,
                "updated_at": now,
                "status": "草稿",
                "task_type": "转子放置",
                "layer": layer,
                "slot": slot,
                "trajectory_file": trajectory,
                "gripper_timeline_file": timeline,
                "recording": {"sample_dt_s": 0.005, "nominal_hz": 200},
                "replay": {
                    "speed_percent": int(self.speed.get()),
                    "play_speed": float(self.play_speed.get()),
                    "stream_dt_s": 0.005,
                    "clock": "recorded",
                    "event_sync": "actual",
                    "gripper_action_hold_s": 0.3,
                    "gripper_event_offset_s": 0.0,
                },
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        self.refresh_tasks(select=name)
        self.log(f"任务已创建/选择：{name}")

    def refresh_tasks(self, select: str | None = None) -> None:
        TASK_ROOT.mkdir(parents=True, exist_ok=True)
        previous = select or self.selected_task
        for item in self.task_tree.get_children():
            self.task_tree.delete(item)
        for directory in sorted(TASK_ROOT.glob("layer_*_slot_*")):
            try:
                manifest = json.loads(
                    (directory / "task.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                manifest = {}
            layer = int(manifest.get("layer", 0))
            slot = int(manifest.get("slot", 0))
            rows, duration = inspect_trajectory(directory / "trajectory.csv")
            events = inspect_timeline(directory / "gripper_timeline.json")
            trajectory_text = "缺失" if rows == 0 else f"{rows}帧/{duration:.1f}s"
            if rows < 0:
                trajectory_text = "格式错误"
            event_text = "缺失" if events == 0 else str(events)
            if events < 0:
                event_text = "格式错误"
            self.task_tree.insert(
                "",
                "end",
                iid=directory.name,
                values=(
                    layer,
                    slot,
                    trajectory_text,
                    event_text,
                    str(manifest.get("status", "草稿")),
                ),
            )
        if previous and self.task_tree.exists(previous):
            self.task_tree.selection_set(previous)
            self.task_tree.focus(previous)
            self.task_tree.see(previous)
            self.selected_task = previous
            self.selected_label.configure(text=f"当前任务：{previous}")

    def on_task_selected(self, _event: object | None = None) -> None:
        selection = self.task_tree.selection()
        if not selection:
            return
        self.selected_task = selection[0]
        values = self.task_tree.item(selection[0], "values")
        if len(values) >= 2:
            self.layer.set(int(values[0]))
            self.slot.set(int(values[1]))
        self.selected_label.configure(text=f"当前任务：{self.selected_task}")

    def require_task(self) -> str | None:
        if not self.selected_task:
            messagebox.showerror("未选择任务", "请先创建或选择一个层/孔位任务。")
            return None
        return self.selected_task

    def gripper_action(self, action: str) -> None:
        verb = "张开" if action == "open" else "闭合"
        if not messagebox.askyesno(
            f"夹爪{verb}",
            f"夹爪将立即{verb}。\n请确保手指和物体不会被夹伤，确认继续吗？",
        ):
            return
        command = (
            self.remote_prefix()
            + "python gripper/control_gripper.py "
            f"--port {shlex.quote(self.gripper_port.get())} "
            f"--action {action} --no-feedback"
        )
        self.run_capture(command, f"夹爪{verb}")

    def go_zero_home(self) -> None:
        if not self.motion_confirm("机械臂将回到六关节全零 Home。"):
            return
        command = self.remote_python(
            "teach.go_zero_home",
            "--can-port",
            self.can_port.get(),
            "--speed",
            min(30, max(1, self.speed.get())),
            "--timeout",
            30,
            "--tolerance-deg",
            1.0,
            "--yes",
        )
        self.run_terminal(command, "回全零 Home")

    def go_feeder(self) -> None:
        if not self.motion_confirm("机械臂将移动到已保存的给料上方点。"):
            return
        command = self.remote_python(
            "teach.go_home",
            "--can-port",
            self.can_port.get(),
            "--home",
            FEEDER_ABOVE,
            "--speed",
            min(30, max(1, self.anchor_speed.get())),
            "--timeout",
            30,
            "--tolerance",
            0.01,
            "--no-gripper",
            "--yes",
        )
        self.run_terminal(command, "回给料上方")

    def record_feeder(self) -> None:
        if not messagebox.askyesno(
            "录制给料上方点",
            "将使用树莓派当前关节反馈覆盖公共给料上方点。\n"
            "请先进入示教状态并把机械臂拖到安全位置，确认继续吗？",
        ):
            return
        command = self.remote_python(
            "teach.set_home",
            "--can-port",
            self.can_port.get(),
            "--output",
            FEEDER_ABOVE,
            "--no-gripper",
            "--overwrite",
        )
        self.run_terminal(command, "录制给料上方点")

    def record_trajectory(self) -> None:
        name = self.require_task()
        if not name:
            return
        trajectory, _timeline = task_paths(name)
        if not messagebox.askyesno(
            "录制完整轨迹",
            f"将覆盖 {name} 的机械臂轨迹。\n"
            "开始后拖动机械臂完成动作，按 H 自动返回给料上方并保存。\n"
            "确认继续吗？",
        ):
            return
        root = shlex.quote(str(Path(trajectory).parent).replace("\\", "/"))
        record = self.remote_python(
            "teach.record_trajectory_precise",
            "--can-port",
            self.can_port.get(),
            "--output",
            trajectory,
            "--sample-dt",
            "0.005",
            "--overwrite",
            "--no-auto-home",
            "--no-gripper",
            "--return-point",
            FEEDER_ABOVE,
            "--return-key",
            "h",
            "--return-speed",
            min(30, max(1, self.anchor_speed.get())),
            "--return-timeout",
            30,
            "--return-tolerance",
            0.01,
        )
        command = self.remote_prefix() + f"mkdir -p {root} && " + record.removeprefix(
            self.remote_prefix()
        )
        self.run_terminal(command, f"录制轨迹 {name}")

    def record_timeline(self) -> None:
        name = self.require_task()
        if not name:
            return
        trajectory, timeline = task_paths(name)
        if not self.motion_confirm(
            f"将回放 {name} 并记录夹爪动作。\n"
            "使用 P 暂停、G 闭合、O 张开、C 继续。"
        ):
            return
        command = self.remote_python(
            "teach.record_gripper_timeline",
            "--can-port",
            self.can_port.get(),
            "--row",
            1,
            "--col",
            1,
            "--trajectory-file",
            trajectory,
            "--output",
            timeline,
            "--gripper-port",
            self.gripper_port.get(),
            "--speed",
            self.speed.get(),
            "--play-speed",
            self.play_speed.get(),
            "--stream-dt",
            0.005,
            "--clock",
            "recorded",
            "--start-point",
            FEEDER_ABOVE,
            "--start-point-tolerance",
            0.01,
            "--home-speed",
            min(30, max(1, self.anchor_speed.get())),
            "--overwrite",
        )
        self.run_terminal(command, f"夹爪标注 {name}")

    def replay_selected(self) -> None:
        name = self.require_task()
        if not name:
            return
        trajectory, timeline = task_paths(name)
        if not self.motion_confirm(f"将联合回放机械臂和夹爪任务 {name}。"):
            return
        command = self.remote_python(
            "teach.play_slot_with_gripper",
            "--can-port",
            self.can_port.get(),
            "--row",
            1,
            "--col",
            1,
            "--trajectory-file",
            trajectory,
            "--timeline",
            timeline,
            "--gripper-port",
            self.gripper_port.get(),
            "--speed",
            self.speed.get(),
            "--play-speed",
            self.play_speed.get(),
            "--stream-dt",
            0.005,
            "--clock",
            "recorded",
            "--event-sync",
            "actual",
            "--gripper-action-hold",
            0.3,
            "--tracking-error-limit",
            0.5,
            "--start-point",
            FEEDER_ABOVE,
            "--start-point-tolerance",
            0.01,
            "--home-speed",
            min(30, max(1, self.anchor_speed.get())),
            "--yes",
        )
        self.run_terminal(command, f"联合回放 {name}")

    def run_sequence(self, dry_run: bool) -> None:
        layer = int(self.layer.get())
        start = int(self.sequence_from.get())
        end = int(self.sequence_to.get())
        if layer < 1 or not (1 <= start <= end <= 27):
            messagebox.showerror("参数错误", "请检查层号及起止孔位。")
            return
        if not dry_run and not self.motion_confirm(
            f"将连续执行第 {layer} 层孔位 {start}～{end}。\n"
            "程序只在开始时前往一次给料上方，随后保持 CAN 和夹爪串口连接。"
        ):
            return
        arguments: list[object] = [
            "--layer",
            layer,
            "--from-slot",
            start,
            "--to-slot",
            end,
            "--can-port",
            self.can_port.get(),
            "--gripper-port",
            self.gripper_port.get(),
            "--anchor",
            FEEDER_ABOVE,
            "--anchor-speed",
            min(30, max(1, self.anchor_speed.get())),
            "--anchor-limit",
            0.035,
        ]
        if dry_run:
            arguments.append("--dry-run")
        command = self.remote_python("teach.run_task_sequence", *arguments)
        self.run_terminal(
            command,
            "连续任务检查" if dry_run else f"连续生产 L{layer:02d}",
        )

    def motion_confirm(self, description: str) -> bool:
        return messagebox.askyesno(
            "机械臂运动确认",
            description
            + "\n\n请清空工作空间、准备好物理急停，并确保没有其他 CAN 控制程序。",
        )

    def iter_sync_files(self) -> Iterable[Path]:
        for name in SYNC_ROOTS:
            root = PROJECT_ROOT / name
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(PROJECT_ROOT)
                if any(part in SYNC_EXCLUDED_PARTS for part in relative.parts):
                    continue
                if path.suffix.lower() in {
                    ".pyc",
                    ".pyo",
                    *SYNC_EXCLUDED_SUFFIXES,
                }:
                    continue
                yield path
        for name in SYNC_FILES:
            path = PROJECT_ROOT / name
            if path.is_file():
                yield path
        sdk_zip = PROJECT_ROOT / "third_party" / "piper_sdk_1_0_0_beta.zip"
        if sdk_zip.is_file():
            yield sdk_zip

    def build_push_archive(self, output: Path) -> int:
        count = 0
        with tarfile.open(output, "w:gz") as bundle:
            for path in self.iter_sync_files():
                bundle.add(path, arcname=path.relative_to(PROJECT_ROOT).as_posix())
                count += 1
        return count

    def push_project(self) -> None:
        if not messagebox.askyesno(
            "同步到树莓派",
            "将以 Windows 当前程序和任务覆盖树莓派同名文件。\n"
            "如果树莓派刚完成新录制但尚未拉回，请先取消并执行“拉回现场数据”。\n\n"
            "确认下发吗？",
        ):
            return
        self.save_config()
        self.set_busy(True)
        self.log("正在生成同步包...")

        def worker() -> None:
            archive = Path(tempfile.gettempdir()) / "piper_windows_sync.tar.gz"
            try:
                count = self.build_push_archive(archive)
                host = self.host_name()
                remote_archive = "/tmp/piper_windows_sync.tar.gz"
                self.after(
                    0,
                    self.log,
                    f"同步包包含 {count} 个文件，大小 {archive.stat().st_size / 1048576:.1f} MB",
                )
                subprocess.run(
                    ["scp", str(archive), f"{host}:{remote_archive}"],
                    check=True,
                    cwd=PROJECT_ROOT,
                )
                command = (
                    f"mkdir -p {shlex.quote(self.remote_project())} && "
                    f"tar -xzf {remote_archive} -C {shlex.quote(self.remote_project())} "
                    f"&& rm -f {remote_archive} && echo SYNC_TO_PI_OK"
                )
                result = subprocess.run(
                    ["ssh", host, command],
                    check=True,
                    text=True,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self.after(0, self.log, result.stdout.strip())
                self.after(0, self.log, "本机程序与任务已同步到树莓派。")
            except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                self.after(0, self.log, f"同步失败：{exc}")
                self.after(0, messagebox.showerror, "同步失败", str(exc))
            finally:
                archive.unlink(missing_ok=True)
                self.after(0, self.set_busy, False)

        threading.Thread(target=worker, daemon=True).start()

    def pull_data(self) -> None:
        if not messagebox.askyesno(
            "拉回现场数据",
            "将以树莓派上的现场轨迹、夹爪时间线和任务配置覆盖 Windows 同名文件。\n"
            "确认拉回吗？",
        ):
            return
        self.save_config()
        self.set_busy(True)
        self.log("正在从树莓派打包现场数据...")

        def worker() -> None:
            local_archive = Path(tempfile.gettempdir()) / "piper_pi_data.tar.gz"
            remote_archive = "/tmp/piper_pi_data.tar.gz"
            try:
                host = self.host_name()
                roots = " ".join(shlex.quote(item) for item in PULL_PATHS)
                command = (
                    f"cd {shlex.quote(self.remote_project())} && "
                    f"existing=''; for p in {roots}; do "
                    "if [ -e \"$p\" ]; then existing=\"$existing $p\"; fi; done; "
                    "test -n \"$existing\" && "
                    f"tar -czf {remote_archive} $existing && echo PI_DATA_PACKED"
                )
                subprocess.run(["ssh", host, command], check=True)
                subprocess.run(
                    ["scp", f"{host}:{remote_archive}", str(local_archive)],
                    check=True,
                    cwd=PROJECT_ROOT,
                )
                subprocess.run(
                    ["ssh", host, f"rm -f {remote_archive}"],
                    check=False,
                )
                safe_extract(local_archive, PROJECT_ROOT)
                self.after(0, self.log, "树莓派现场数据已拉回 Windows。")
                self.after(0, self.refresh_tasks)
            except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                self.after(0, self.log, f"拉回失败：{exc}")
                self.after(0, messagebox.showerror, "拉回失败", str(exc))
            finally:
                local_archive.unlink(missing_ok=True)
                self.after(0, self.set_busy, False)

        threading.Thread(target=worker, daemon=True).start()

    def gripper_action_quick(self, action: str) -> None:
        """Send an immediate gripper command without a confirmation dialog."""
        verb = "张开" if action == "open" else "闭合"
        self.gripper_state.configure(
            text=f"夹爪{verb}命令已发送",
            foreground=self.GREEN if action == "open" else self.BLUE,
        )
        command = self.remote_python(
            "teach.control_gripper_logged",
            "--port",
            self.gripper_port.get(),
            "--action",
            action,
            "--startup-delay",
            0,
            "--state-file",
            DIY_GRIPPER_STATE_FILE,
        )
        self.run_capture(command, f"夹爪{verb}")

    def start_full_log(self) -> None:
        command = (
            self.remote_prefix()
            + f"mkdir -p {shlex.quote(FULL_LOG_DIR)} && "
            + shlex.join(
                [
                    "python",
                    "-u",
                    "scripts/record_piper_full_log.py",
                    "--can-port",
                    self.can_port.get(),
                    "--interval",
                    "0.005",
                    "--output-dir",
                    FULL_LOG_DIR,
                    "--diy-gripper-state-file",
                    DIY_GRIPPER_STATE_FILE,
                    "--diy-gripper-initial-state",
                    "unknown",
                ]
            )
        )
        self.run_terminal(command, "200 Hz 完整状态记录")

    def open_full_log_folder(self) -> None:
        folder = PROJECT_ROOT / FULL_LOG_DIR
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开文件夹", str(exc))

    def pull_full_logs_and_open(self) -> None:
        self.save_config()
        self.set_busy(True)
        self.log("正在从树莓派拉回完整状态记录 ...")

        def worker() -> None:
            local_archive = Path(tempfile.gettempdir()) / "piper_full_logs.tar.gz"
            remote_archive = "/tmp/piper_full_logs.tar.gz"
            try:
                host = self.host_name()
                command = (
                    f"cd {shlex.quote(self.remote_project())} && "
                    f"test -d {shlex.quote(FULL_LOG_DIR)} && "
                    f"tar -czf {remote_archive} {shlex.quote(FULL_LOG_DIR)}"
                )
                subprocess.run(["ssh", host, command], check=True)
                subprocess.run(
                    ["scp", f"{host}:{remote_archive}", str(local_archive)],
                    check=True,
                    cwd=PROJECT_ROOT,
                )
                subprocess.run(
                    ["ssh", host, f"rm -f {remote_archive}"],
                    check=False,
                )
                safe_extract(local_archive, PROJECT_ROOT)
                self.after(0, self.log, "完整状态记录已拉回 Windows。")
                self.after(0, self.open_full_log_folder)
            except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                self.after(0, self.log, f"完整记录拉回失败：{exc}")
                self.after(
                    0,
                    messagebox.showerror,
                    "完整记录拉回失败",
                    str(exc),
                )
            finally:
                local_archive.unlink(missing_ok=True)
                self.after(0, self.set_busy, False)

        threading.Thread(target=worker, daemon=True).start()

    def validate_selected(self) -> None:
        """Pull the selected remote task, validate it, and save it on Windows."""
        name = self.require_task()
        if not name:
            return
        self.save_config()
        self.set_busy(True)
        self.log(f"正在拉回并检查任务 {name} ...")

        def worker() -> None:
            local_archive = (
                Path(tempfile.gettempdir()) / f"piper_validate_{name}.tar.gz"
            )
            remote_archive = f"/tmp/piper_validate_{name}.tar.gz"
            relative = f"teach/production_tasks/{name}"
            try:
                host = self.host_name()
                command = (
                    f"cd {shlex.quote(self.remote_project())} && "
                    f"test -d {shlex.quote(relative)} && "
                    f"tar -czf {shlex.quote(remote_archive)} "
                    f"{shlex.quote(relative)}"
                )
                subprocess.run(["ssh", host, command], check=True)
                subprocess.run(
                    ["scp", f"{host}:{remote_archive}", str(local_archive)],
                    check=True,
                    cwd=PROJECT_ROOT,
                )
                subprocess.run(
                    ["ssh", host, f"rm -f {shlex.quote(remote_archive)}"],
                    check=False,
                )
                safe_extract(local_archive, PROJECT_ROOT)
                self.after(0, self.finish_task_validation, name)
            except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                self.after(0, self.log, f"任务拉回失败：{exc}")
                self.after(
                    0,
                    messagebox.showerror,
                    "任务拉回失败",
                    f"无法从树莓派保存任务 {name}：\n{exc}",
                )
            finally:
                local_archive.unlink(missing_ok=True)
                self.after(0, self.set_busy, False)

        threading.Thread(target=worker, daemon=True).start()

    def finish_task_validation(self, name: str) -> None:
        directory = TASK_ROOT / name
        rows, duration = inspect_trajectory(directory / "trajectory.csv")
        events = inspect_timeline(directory / "gripper_timeline.json")
        problems: list[str] = []
        if rows < 0:
            problems.append("trajectory.csv 格式错误")
        elif rows == 0:
            problems.append("缺少 trajectory.csv")
        elif rows < 20:
            problems.append(f"轨迹采样点过少：{rows}")
        if events < 0:
            problems.append("gripper_timeline.json 格式错误")
        elif events == 0:
            problems.append("缺少夹爪事件或事件数量为 0")
        if problems:
            self.log(f"{name} 检查未通过：{'；'.join(problems)}")
            messagebox.showwarning("任务尚不完整", "\n".join(problems))
            self.refresh_tasks(select=name)
            return

        manifest_path = directory / "task.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {"format": TASK_FORMAT, "task_id": name}
        checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
        manifest["status"] = "已验证"
        manifest["updated_at"] = checked_at
        manifest["validation"] = {
            "checked_at": checked_at,
            "trajectory_rows": rows,
            "trajectory_duration_s": round(duration, 6),
            "gripper_events": events,
            "result": "files_valid",
            "note": "Physical replay completed separately in workflow step C.",
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.selected_task = name
        self.refresh_tasks(select=name)
        self.log(
            f"{name} 已保存到 Windows：{rows} 点，"
            f"{duration:.3f}s，{events} 个夹爪事件。"
        )
        messagebox.showinfo(
            "任务已保存",
            f"任务：{name}\n"
            f"轨迹采样点：{rows}\n"
            f"轨迹时长：{duration:.3f} s\n"
            f"夹爪事件：{events}\n\n"
            "树莓派现场文件已拉回 Windows，并标记为“已验证”。",
        )

    def on_close(self) -> None:
        self.save_config()
        self.destroy()


def main() -> int:
    if os.name != "nt":
        print("This workstation is intended to run on Windows.", file=sys.stderr)
        return 2
    if not (Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "OpenSSH").exists():
        print("Windows OpenSSH is not installed.", file=sys.stderr)
        return 2
    app = RemoteWorkbench()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
