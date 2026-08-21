#!/usr/bin/env bash
set -euo pipefail

CAN_PORT="${PIPER_CAN_PORT:-can0}"
CAN_BITRATE="${PIPER_CAN_BITRATE:-1000000}"
WAIT_SECONDS="${PIPER_CAN_WAIT_SECONDS:-30}"

modprobe gs_usb

for ((second = 0; second < WAIT_SECONDS; second++)); do
  if ip link show "$CAN_PORT" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! ip link show "$CAN_PORT" >/dev/null 2>&1; then
  echo "CAN interface $CAN_PORT did not appear within ${WAIT_SECONDS}s" >&2
  exit 1
fi

ip link set "$CAN_PORT" down 2>/dev/null || true
ip link set "$CAN_PORT" type can bitrate "$CAN_BITRATE"
ip link set "$CAN_PORT" up
ip -details link show "$CAN_PORT"
