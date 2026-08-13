#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DIY STM32 gripper serial helper used by Piper replay tools."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ACTION_TO_PROTOCOL = {
    "close": "on",
    "grip": "on",
    "open": "off",
    "release": "off",
}

DEFAULT_STATE_FILE = Path("records/diy_gripper_state.json")


def _state_file() -> Path:
    configured = os.environ.get("PIPER_DIY_GRIPPER_STATE_FILE")
    return Path(configured) if configured else DEFAULT_STATE_FILE


def _read_state_sequence(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("sequence", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def publish_commanded_state(action: str) -> dict[str, object]:
    """Publish the latest commanded state for concurrent telemetry logging."""
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    state = "open" if action in {"open", "release"} else "closed"
    payload: dict[str, object] = {
        "format": "piper_diy_gripper_command_state_v1",
        "state": state,
        "action": action,
        "sequence": _read_state_sequence(path) + 1,
        "timestamp": time.time(),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    print(
        f"Published DIY gripper commanded state: {state}, "
        f"sequence={payload['sequence']}, file={path}"
    )
    return payload


def available_ports() -> list[Any]:
    from serial.tools import list_ports

    return list(list_ports.comports())


def choose_port(requested: str | None) -> str:
    """Pick a likely Linux USB serial device when --gripper-port is omitted."""
    if requested:
        return requested
    candidates = [
        port.device
        for port in available_ports()
        if port.device.startswith(("/dev/ttyUSB", "/dev/ttyACM"))
    ]
    if len(candidates) == 1:
        print(f"Auto-selected gripper serial port: {candidates[0]}")
        return candidates[0]
    if not candidates:
        raise RuntimeError(
            "No /dev/ttyUSB* or /dev/ttyACM* gripper serial device found."
        )
    raise RuntimeError(
        "Multiple USB serial ports found; specify --gripper-port: "
        + ", ".join(candidates)
    )


@dataclass
class GripperEvent:
    time_s: float
    action: str
    protocol: str
    replay_elapsed_s: float
    epoch_s: float


class DiyGripper:
    """Persistent serial connection for the simple open/close STM32 gripper."""

    def __init__(
        self,
        *,
        port: str | None,
        baudrate: int = 9600,
        timeout: float = 0.3,
        startup_delay: float = 2.0,
        wait_feedback: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.requested_port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.startup_delay = startup_delay
        self.wait_feedback = wait_feedback
        self.dry_run = dry_run
        self.port: str | None = None
        self.connection: Any | None = None

    def __enter__(self) -> "DiyGripper":
        if self.dry_run:
            print("DIY gripper dry-run: no serial command will be sent.")
            return self
        try:
            import serial
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "pyserial is not installed. Run: python -m pip install -r "
                "gripper/requirements.txt"
            ) from exc

        self.port = choose_port(self.requested_port)
        self.connection = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
        )
        print(f"Opened DIY gripper serial port: {self.port} @ {self.baudrate}")
        time.sleep(self.startup_delay)
        return self

    def __exit__(self, *_: object) -> None:
        if self.connection is not None:
            self.connection.close()
            print("Closed DIY gripper serial port.")

    def send(self, action: str) -> dict[str, Any] | None:
        if action not in ACTION_TO_PROTOCOL:
            raise ValueError(f"unsupported gripper action: {action}")
        protocol = ACTION_TO_PROTOCOL[action]
        payload = {
            "frame_type": "control",
            "version": "1.0",
            "execute": protocol,
        }
        message = json.dumps(payload, separators=(",", ":")) + "\r\n"
        if self.dry_run:
            print(f"[dry-run] gripper {action}: {message.strip()}")
            return None
        if self.connection is None:
            raise RuntimeError("gripper serial port is not open")

        self.connection.reset_input_buffer()
        self.connection.write(message.encode("utf-8"))
        self.connection.flush()
        print(f"Sent gripper {action}: {message.strip()}")
        if not self.wait_feedback:
            publish_commanded_state(action)
            return None

        line = self.connection.readline().decode("utf-8", errors="replace").strip()
        if not line:
            raise TimeoutError("No STM32 feedback before timeout.")
        print(f"Gripper feedback: {line}")
        feedback = json.loads(line)
        actual = feedback.get("switch_state")
        if feedback.get("frame_type") != "feedback" or actual != protocol:
            raise RuntimeError(
                f"Unexpected gripper feedback: expected {protocol!r}, got {feedback!r}"
            )
        publish_commanded_state(action)
        return feedback
