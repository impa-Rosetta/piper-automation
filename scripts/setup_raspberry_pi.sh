#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${PIPER_VENV:-$HOME/.venvs/piper_robot_project_api}"

sudo apt-get update
sudo apt-get install -y \
  can-utils \
  git \
  openssh-server \
  python3-venv \
  python3-tk \
  usbutils \
  wpasupplicant

python3 -m venv "$VENV"
source "$VENV/bin/activate"
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r "$PROJECT_ROOT/requirements.txt"

SDK_REPO="${PIPER_SDK_REPO:-https://github.com/agilexrobotics/piper_sdk.git}"
SDK_BRANCH="${PIPER_SDK_BRANCH:-1_0_0_beta}"
SDK_SRC="${PIPER_SDK_SRC:-$HOME/src/piper_sdk_1_0_0_beta}"
if [[ -d "$SDK_SRC/.git" ]]; then
  git -C "$SDK_SRC" fetch origin "$SDK_BRANCH"
  git -C "$SDK_SRC" checkout "$SDK_BRANCH"
  git -C "$SDK_SRC" pull --ff-only origin "$SDK_BRANCH"
else
  rm -rf "$SDK_SRC"
  git clone --branch "$SDK_BRANCH" --single-branch "$SDK_REPO" "$SDK_SRC"
fi
python -m pip install "$SDK_SRC"

python - <<'PY'
import piper_sdk

print("piper_sdk:", piper_sdk.__file__)
if "Piper" not in dir(piper_sdk):
    raise SystemExit("Installed piper_sdk has no high-level Piper class")
PY

sudo install -m 0755 "$PROJECT_ROOT/scripts/start_can0.sh" /usr/local/sbin/piper-can0-up
sudo install -m 0644 "$PROJECT_ROOT/systemd/can0.service" /etc/systemd/system/can0.service
sudo install -m 0644 "$PROJECT_ROOT/udev/99-piper-gripper.rules" /etc/udev/rules.d/99-piper-gripper.rules
sudo systemctl daemon-reload
sudo systemctl enable can0.service
sudo systemctl enable --now ssh
sudo usermod -aG dialout "$USER"
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "Setup complete. Reconnect the CAN adapter and STM32 gripper, then run:"
echo "  sudo systemctl start can0.service"
echo "  ip -details link show can0"
echo "  ls -l /dev/piper_gripper"
echo "Log out and back in once so the dialout group membership takes effect."
