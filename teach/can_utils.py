#!/usr/bin/env python3
"""SocketCAN preflight checks shared by the controller commands."""

from __future__ import annotations

from pathlib import Path


def require_can_interface(can_port: str) -> None:
    """Fail early with an actionable message when SocketCAN is unavailable."""
    interface_path = Path("/sys/class/net") / can_port
    if not interface_path.exists():
        raise SystemExit(
            f"ERROR: CAN interface {can_port!r} does not exist.\n"
            "Connect the candleLight adapter, load gs_usb, and run:\n"
            f"  sudo ip link set {can_port} up type can bitrate 1000000"
        )
    try:
        state = (interface_path / "operstate").read_text(encoding="utf-8").strip()
    except OSError:
        state = "unknown"
    if state == "down":
        raise SystemExit(
            f"ERROR: CAN interface {can_port!r} is DOWN. Run:\n"
            f"  sudo ip link set {can_port} up type can bitrate 1000000"
        )
