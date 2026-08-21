# Deployment / 部署

For a complete first-time installation, network handoff, Windows setup, field
workflow, and acceptance checklist, read
[`GETTING_STARTED.zh-CN.md`](GETTING_STARTED.zh-CN.md).

## Hardware

- AgileX Piper arm
- candleLight-compatible USB-CAN adapter (`gs_usb`)
- Raspberry Pi 4 with Ubuntu 22.04 ARM64
- STM32 USB CDC gripper controller (`0483:5740`)
- Windows 10/11 operator PC on the same network

## Raspberry Pi setup

```bash
cd ~/piper_robot_project
bash scripts/setup_raspberry_pi.sh
sudo systemctl start can0.service
sudo usermod -aG dialout "$USER"
```

Log out and back in after changing group membership. Verify:

```bash
systemctl status can0.service --no-pager
ip -details -statistics link show can0
ls -l /dev/piper_gripper
```

The CAN service configures `can0` at `1000000` bit/s. The udev rule maps the
STM32 virtual COM port to `/dev/piper_gripper` and grants access to `dialout`.

## SDK compatibility

The recording/replay path uses the high-level `Piper` class from the
API-enabled official SDK branch used by AgileX's `recordAndPlayTraj` example.
The setup script checks out and installs the `1_0_0_beta` branch from the
official SDK repository. This branch name reflects the API used by the original
teach/replay integration; validate it against your robot firmware before motion.

Always inspect the installed package before first motion:

```bash
source ~/.venvs/piper_robot_project_api/bin/activate
python -c "import piper_sdk; print(piper_sdk.__file__); print('Piper' in dir(piper_sdk))"
```

Expected: the final value is `True`.

## SSH setup

Create an entry in `%USERPROFILE%\.ssh\config`:

```sshconfig
Host piper-pi
    HostName 192.168.1.50
    User piper
    IdentityFile ~/.ssh/id_ed25519
```

Use an SSH key. Do not store passwords in this repository.

## First hardware test

1. Clear the workspace and keep the physical E-stop within reach.
2. Check `can0` and the gripper symlink.
3. Read status without motion.
4. Test gripper open/close with no payload.
5. Enable the arm, then test zero Home at low speed.
6. Record and validate one task before creating the full 27-slot layer.

```bash
python scripts/read_status.py --can-port can0
python gripper/control_gripper.py --port /dev/piper_gripper --action open --no-feedback
python -m teach.piper_power_control --can-port can0 --action enable --reset-first
python -m teach.go_zero_home --can-port can0 --speed 5
```

## Windows workbench

Run `start_piper_windows_workbench.bat`. The first connection check must report:

- Raspberry Pi online
- `can0` active and `ERROR-ACTIVE`
- `/dev/piper_gripper` present
- non-zero Piper feedback after the first SDK frame

Do not start recording or production when feedback remains all zero.
