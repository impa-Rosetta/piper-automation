# 现场故障排查

遵循“先只读、后夹爪、再低速运动”的顺序。不要用反复重启或反复使能掩盖硬件问题。

## SSH 连接拒绝

如果密码提示显示 `administrator@piper-pi`，Windows 当前账户被错误当成了树莓派账户。
新版工作台在“SSH 主机”只填写 `piper-pi` 或 IP 时会自动使用 `piper`；其他 Linux
账号请填写 `用户名@主机名或IP`。可先在 PowerShell 验证：

```powershell
ssh piper@piper-pi
```

Windows：

```powershell
ping <PI_IP>
Test-NetConnection <PI_IP> -Port 22
```

树莓派本地或经网线登录后：

```bash
sudo systemctl enable --now ssh
systemctl status ssh --no-pager
hostname -I
```

`Connection refused` 表示 IP 可达但 SSH 服务未监听；超时通常是 IP、网络隔离或防火墙问题。

## `can0` 不存在

```bash
lsusb | grep -E '1d50:606f|CAN|OpenMoko'
sudo modprobe gs_usb
ip -br link
```

USB 看不到：检查供电、线缆和 USB 接口。USB 能看到但 `can0` 不存在：检查 `gs_usb`
驱动和 `dmesg`。

## `can0` 存在但 DOWN

```bash
sudo systemctl restart can0.service
systemctl status can0.service --no-pager
ip -details link show can0
```

不要在接口已经 UP 时重复使用带冲突参数的 `ip link set ... up type can ...`。

## CAN UP 但反馈持续全零

检查 Piper 是否上电、急停是否释放、CAN-H/CAN-L、终端电阻、1 Mbps 波特率、RX
包计数以及是否有另一个控制进程。持续全零时禁止录点或保存轨迹。

## `SEND_MESSAGE_FAILED (100017)`

通常表示 SocketCAN 发送链路不可用，而不是轨迹算法问题：

```bash
ip -details -statistics link show can0
dmesg | tail -n 80
```

## `/dev/piper_gripper` 不存在或无权限

```bash
lsusb | grep -i stm
ls -l /dev/ttyACM*
sudo udevadm control --reload-rules
sudo udevadm trigger
groups
sudo usermod -aG dialout "$USER"
```

修改用户组后注销再登录。临时 `chmod` 只适合诊断，重插后会失效。

## 工作台提示模块不存在

```bash
cd ~/piper-automation
source ~/.venvs/piper_robot_project_api/bin/activate
python -c "import piper_sdk; print(piper_sdk.__file__); print('Piper' in dir(piper_sdk))"
```

最后应输出 `True`。

## 回放起点误差过大

不要简单提高误差阈值。检查当前任务是否从公共给料上方录制、锚点是否被移动、机械结构
是否变化、任务文件是否属于当前工位，以及是否在改变速度后跳过了联合验证。

## 夹爪事件提前或被吞

- 使用实际轨迹进度同步；
- 为夹爪保留足够动作保持时间；
- 不要未经验证直接提高回放倍率；
- 确保时间线与当前 `trajectory.csv` 配套；
- 修改轨迹后重新标注夹爪时间线。

## 换热点后找不到树莓派

参考 [NETWORK_HANDOVER.zh-CN.md](NETWORK_HANDOVER.zh-CN.md)。先检查热点是否允许设备互访，
再在热点客户端列表找新 IP。用户名和密码不会因为换网络改变。
