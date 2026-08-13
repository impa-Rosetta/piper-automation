#!/usr/bin/env python3
"""Control the DIY STM32 gripper over a USB serial port."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

ACTION_TO_PROTOCOL = {
    "close": "on",
    "grip": "on",
    "open": "off",
    "release": "off",
}


def available_ports() -> list[Any]:
    from serial.tools import list_ports

    return list(list_ports.comports())


def print_ports() -> None:
    ports = available_ports()
    if not ports:
        print("No serial ports found.")
        return
    print("Available serial ports:")
    for port in ports:
        print(
            f"  {port.device}: {port.description} "
            f"[VID:PID={port.vid!s}:{port.pid!s}]"
        )


def choose_port(requested: str | None) -> str:
    if requested:
        return requested
    candidates = [
        port.device
        for port in available_ports()
        if port.device.startswith(("/dev/ttyUSB", "/dev/ttyACM"))
    ]
    if len(candidates) == 1:
        print(f"Auto-selected serial port: {candidates[0]}")
        return candidates[0]
    if not candidates:
        raise RuntimeError(
            "No /dev/ttyUSB* or /dev/ttyACM* device found. "
            "Connect the STM32 USB device to the Linux controller."
        )
    raise RuntimeError(
        "Multiple USB serial ports found; select one with --port: "
        + ", ".join(candidates)
    )


def command_payload(action: str) -> dict[str, str]:
    try:
        execute = ACTION_TO_PROTOCOL[action]
    except KeyError as exc:
        raise ValueError(f"unsupported action: {action}") from exc
    return {
        "frame_type": "control",
        "version": "1.0",
        "execute": execute,
    }


def send_action(
    connection: Any,
    action: str,
    wait_feedback: bool,
) -> dict[str, Any] | None:
    payload = command_payload(action)
    message = json.dumps(payload, separators=(",", ":")) + "\r\n"
    connection.reset_input_buffer()
    connection.write(message.encode("utf-8"))
    connection.flush()
    print(f"Sent: {message.strip()}")
    if not wait_feedback:
        return None

    line = connection.readline().decode("utf-8", errors="replace").strip()
    if not line:
        raise TimeoutError("No STM32 feedback before timeout.")
    print(f"Received: {line}")
    try:
        feedback = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError("STM32 feedback is not valid JSON.") from exc
    expected = payload["execute"]
    actual = feedback.get("switch_state")
    if feedback.get("frame_type") != "feedback" or actual != expected:
        raise RuntimeError(
            f"Unexpected feedback: expected switch_state={expected!r}, "
            f"got {feedback!r}"
        )
    return feedback


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Control the DIY STM32 gripper through USB serial."
    )
    parser.add_argument("--port", help="For example /dev/ttyUSB0.")
    parser.add_argument("--baudrate", type=int, default=9600)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--startup-delay", type=float, default=2.0)
    parser.add_argument(
        "--action",
        choices=tuple(ACTION_TO_PROTOCOL),
        help="open/release sends off; close/grip sends on.",
    )
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--no-feedback", action="store_true")
    args = parser.parse_args()

    try:
        import serial
    except ModuleNotFoundError:
        print(
            "ERROR: pyserial is not installed. Run: "
            "python -m pip install -r gripper/requirements.txt",
            file=sys.stderr,
        )
        return 3

    if args.list_ports:
        print_ports()
        return 0
    if not args.action:
        parser.error("--action is required unless --list-ports is used")

    try:
        port = choose_port(args.port)
        with serial.Serial(
            port=port,
            baudrate=args.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=args.timeout,
        ) as connection:
            print(f"Opened {port} at {args.baudrate} baud.")
            time.sleep(args.startup_delay)
            send_action(
                connection,
                args.action,
                wait_feedback=not args.no_feedback,
            )
    except PermissionError:
        print(
            f"Permission denied for {args.port or 'USB serial port'}. "
            "Add the user to group dialout and log in again.",
            file=sys.stderr,
        )
        return 2
    except serial.SerialException as exc:
        if getattr(exc, "errno", None) == 13 or "Permission denied" in str(exc):
            print(
                "ERROR: Serial port permission denied. Run: "
                "sudo usermod -aG dialout $USER, then log out and log in. "
                "For a temporary test: sudo chmod a+rw "
                f"{args.port or '<serial-port>'}",
                file=sys.stderr,
            )
            return 2
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Gripper action complete: {args.action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
