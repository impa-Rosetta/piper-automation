# 从零部署与交接教程

本文面向第一次接手本项目的工程师，目标是从一台新的 Windows 电脑和一块已安装
Ubuntu 22.04 ARM64 的树莓派开始，完成代码获取、树莓派配网、SSH、USB-CAN、夹爪、
轨迹录制、联合回放和连续生产。

若新同事继续使用原来已经调通的同一块树莓派，优先阅读
[AI 辅助同机树莓派交接手册](AI_ASSISTED_HANDOVER.zh-CN.md)，并先运行只读诊断脚本。

> 先读安全说明：本项目会控制真实机械臂。第一次执行任何运动必须空载、低速、清空
> 工作区，并保证物理急停在手边。软件停止不能代替急停和安全围栏。

## 1. 系统组成

| 设备 | 职责 | 不负责的事情 |
| --- | --- | --- |
| Windows 10/11 电脑 | 图形工作台、任务管理、SSH/SCP、数据备份 | 不直接打开 CAN 或夹爪串口 |
| 树莓派 4 | Piper SDK、SocketCAN、夹爪串口、实时录制与回放 | 不保存任何热点密码到 GitHub |
| candleLight USB-CAN | 将 Piper CAN 总线接入树莓派 | 波特率固定为 1 Mbps |
| STM32 夹爪控制器 | 接收 `open` / `close` 串口命令 | 当前协议不保证提供电流反馈 |

建议树莓派主机名使用 `piper-pi`，项目目录使用
`/home/<用户名>/piper_robot_project`。GitHub 仓库名仍为 `piper-automation`，二者不要
混淆。文档中的 `<PI_USER>`、`<PI_IP>` 和
`<REMOTE_ROOT>` 都必须替换为现场实际值。

## 2. 交接前必须保存的内容

GitHub 只保存通用程序，不保存工厂轨迹、真实坐标、日志和网络凭据。原操作员离开前
必须单独导出：

- `teach/production_tasks/`：每层每孔的轨迹和夹爪时间线；
- `teach/feeder_above.json`：公共给料上方锚点；
- `teach/home.json`、`teach/zero_home.json`：已保存的回位数据（如果存在）；
- `config/windows_remote_workbench.json`：本机连接配置（仅内部保存）；
- 需要留档的 `records/`。

在树莓派项目目录执行：

```bash
bash scripts/export_site_data.sh
```

脚本会生成带时间戳的压缩包。把它复制到内部文件服务器或交接 U 盘，不要提交到公开
GitHub。恢复方法见第 10 节。

## 3. 树莓派首次联网

优先使用网线连接树莓派和路由器。这样即使 Wi-Fi 配置错误，也能从有线网络继续 SSH。
树莓派和 Windows 必须位于同一局域网，且网络不能开启客户端隔离。

### 3.1 找到树莓派 IP

可采用任一方法：

1. 在路由器或手机热点的“已连接设备”中查找 `piper-pi`；
2. 在树莓派本地终端执行 `hostname -I`；
3. Windows PowerShell 尝试 `ping piper-pi.local`；
4. 已知网段时查看路由器 DHCP 租约，不建议盲目扫描公共网络。

测试：

```powershell
ping <PI_IP>
ssh <PI_USER>@<PI_IP>
```

首次出现主机指纹时，核对设备后输入 `yes`。

### 3.2 把树莓派改连同事的热点或工厂 Wi-Fi

完整换网说明见 [NETWORK_HANDOVER.zh-CN.md](NETWORK_HANDOVER.zh-CN.md)。最稳妥的做法是：

1. 保持网线或旧热点连接；
2. 先添加新 Wi-Fi；
3. 确认新网络能分配 IP；
4. 再关闭旧热点；
5. 在新网络重新建立 SSH。

项目提供交互式脚本，密码不会出现在命令历史中：

```bash
cd ~/piper_robot_project
bash scripts/configure_pi_wifi.sh --ssid "新的热点名称"
```

网络切换时当前 SSH 断开属于正常现象。到新热点设备列表中查找新的 IP。

## 4. Windows 准备

安装：

- Git for Windows；
- Python 3.10 或更高版本（安装时勾选 Add Python to PATH）；
- Windows OpenSSH Client。

检查：

```powershell
git --version
python --version
ssh -V
scp
```

若没有 OpenSSH Client，以管理员身份打开 PowerShell：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

### 4.1 从 GitHub 获取 Windows 工作台

```powershell
cd D:\robot
git clone https://github.com/impa-Rosetta/piper-automation.git
cd piper-automation
```

路径可以自行更换，但尽量不要放在 OneDrive 同步目录。

### 4.2 配置 SSH 密钥（每台 Windows 电脑各做一次）

运行项目辅助脚本。它会创建当前 Windows 账户自己的 SSH 密钥、写入 SSH
别名，并把公钥安装到树莓派。执行过程中只需输入一次树莓派 Linux 登录密码；
此后工作台的 Home、给料上方、机械臂和夹爪按钮均不再询问密码：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure_windows_workstation.ps1 `
  -PiAddress <PI_IP> `
  -PiUser <PI_USER> `
  -RemoteRoot /home/<PI_USER>/piper_robot_project
```

验证：

```powershell
ssh piper-pi "hostname; uname -m"
```

也可以在 Windows 工作台顶部填写树莓派 IP 后，点击“首次配置免密 SSH”。
注意：换一台 Windows 电脑时必须重新配置一次，因为 SSH 私钥属于具体的 Windows
账户，不应从上一位操作员电脑直接复制。

## 5. 树莓派获取和安装项目

登录树莓派：

```powershell
ssh piper-pi
```

如果使用本项目已经交付的同一块树莓派，项目已位于
`/home/piper/piper_robot_project`，不要重复克隆，也不要在该目录执行 `git pull`。这块
树莓派采用工作台归档同步方式部署，Git 信息不存在属于正常现象。代码更新流程是：

1. 在 Windows 的 `piper-automation` 仓库执行 `git pull`；
2. 打开工作台并先执行“从树莓派拉回现场数据”；
3. 再执行“同步程序到树莓派”。

只有全新树莓派才执行下面的首次安装命令：

在树莓派执行：

```bash
cd ~
git clone https://github.com/impa-Rosetta/piper-automation.git piper_robot_project
cd piper_robot_project
bash scripts/setup_raspberry_pi.sh
sudo usermod -aG dialout "$USER"
sudo reboot
```

安装脚本会：

- 创建 `~/.venvs/piper_robot_project_api`；
- 安装 Python 依赖；
- 从 AgileX 官方仓库安装 API-enabled `piper_sdk`；
- 安装 `can0.service`；
- 安装 STM32 夹爪 udev 规则，生成 `/dev/piper_gripper`；
- 设置 CAN 服务开机启用。

重启后重新 SSH，并激活环境：

```bash
cd ~/piper_robot_project
source ~/.venvs/piper_robot_project_api/bin/activate
```

## 6. 连接硬件

断开机械臂电源后检查接线：

1. candleLight 连接树莓派 USB 与 Piper CAN；
2. CAN-H 对 CAN-H、CAN-L 对 CAN-L，并确认终端电阻和机械臂电源；
3. STM32 夹爪控制器连接树莓派 USB；
4. 同一设备不要同时交给另一台电脑或虚拟机；
5. 再上电并释放物理急停。

检查 USB：

```bash
lsusb
ls -l /dev/piper_gripper
```

candleLight 常见 USB ID 为 `1d50:606f`，STM32 Virtual COM Port 常见 ID 为
`0483:5740`。现场硬件批次不同则以实际 ID 为准，并相应修改 udev 规则。

## 7. 启动 CAN 并只读检查

```bash
sudo systemctl restart can0.service
systemctl status can0.service --no-pager
ip -details -statistics link show can0
```

正确状态通常包含：

- 接口存在且为 `UP,LOWER_UP`；
- `bitrate 1000000`；
- `can state ERROR-ACTIVE`；
- 机械臂上电后 RX 包计数持续增长。

只读检查，不发送运动：

```bash
source ~/.venvs/piper_robot_project_api/bin/activate
python scripts/read_status.py --can-port can0
```

第一次可能短暂显示零反馈；若持续全零，禁止运动，按故障排查文档处理。

测试夹爪时确保手指远离夹持区域：

```bash
python gripper/control_gripper.py --port /dev/piper_gripper --action open --no-feedback
python gripper/control_gripper.py --port /dev/piper_gripper --action close --no-feedback
```

## 8. 第一次低速运动验证

清空工作区、空载、手握急停，然后：

```bash
python -m teach.piper_power_control --can-port can0 --action recover
python -m teach.piper_power_control --can-port can0 --action enable --reset-first
python -m teach.go_zero_home --can-port can0 --speed 5
```

全零 Home 是程序化起点，不保证对所有现场夹具都天然安全。设备位置或末端结构变化后，
必须重新做碰撞评估。

## 9. 启动 Windows 工作台

在 Windows 项目目录双击：

```text
start_piper_windows_workbench.bat
```

工作台连接参数建议：

- 主机：`piper-pi`（工作台会明确使用默认 Linux 用户 `piper`，等价于
  `piper@piper-pi`）；如果树莓派使用其他账号，填写 `用户名@主机名或IP`；
- 远程项目：`/home/<PI_USER>/piper_robot_project`；
- CAN：`can0`；
- 夹爪：`/dev/piper_gripper`。

若终端出现 `administrator@piper-pi's password`，说明使用的是旧版工作台或旧配置：
先更新项目，再重新打开工作台。正确提示应为 `piper@piper-pi's password`；这里要求
输入的是树莓派 Linux 账号密码，不是热点密码。

先点击“连接与设备检查”。使用同一块已录制现场数据的树莓派时，再点击
“从树莓派拉回现场数据”，把未公开提交的工厂轨迹同步到这台 Windows 电脑。确认任务
列表出现后再进行：

1. **同步程序到树莓派**；
2. **录制公共给料上方点**；
3. 选择层号和孔位；
4. **A 录制机械臂完整轨迹**；
5. **B 回放并标注夹爪动作**；
6. **C 低速联合试跑**；
7. **D 拉回 Windows 并验证保存**；
8. 完成一层后，以低速执行连续生产测试。

详细按键、安全条件和生产流程见 [FIELD_RUNBOOK.zh-CN.md](FIELD_RUNBOOK.zh-CN.md)。

## 10. 恢复既有现场轨迹

把内部交接的现场数据压缩包复制到树莓派项目目录之外，例如 `~/backup/`，然后：

```bash
cd ~/piper_robot_project
bash scripts/restore_site_data.sh ~/backup/piper_site_data_YYYYMMDD_HHMMSS.tar.gz
```

脚本会先显示将恢复的文件，并要求输入 `RESTORE`。恢复后不要直接生产，必须按以下顺序：

1. 查看任务数量和文件时间；
2. 无负载低速验证一个已知孔位；
3. 验证夹爪事件；
4. 再验证两个连续孔位；
5. 最后才允许整层连续运行。

## 11. 日常启动顺序

1. 固定机械臂、托盘、给料位置和夹具；
2. 接通树莓派、CAN、夹爪和机械臂；
3. Windows 与树莓派连接同一网络；
4. 工作台执行设备检查；
5. 释放急停并执行 `recover/enable`；
6. 低速回给料上方或安全起点；
7. 先运行单任务，再运行连续任务；
8. 结束后停止生产、张开夹爪、机械臂回安全位置并失能；
9. 拉回新轨迹和日志，进行内部备份。

## 12. 交接验收标准

- 新同事能在不知道原热点密码的情况下让树莓派接入新网络；
- Windows 可以通过 `ssh piper-pi` 免密登录；
- `can0` 开机自动出现且为 1 Mbps；
- `/dev/piper_gripper` 重插后仍存在；
- Piper 状态反馈非零且无通信错误；
- 夹爪能独立开合；
- 全零 Home 和给料上方点经过现场安全确认；
- 至少一个孔位完成 A/B/C/D 全流程；
- 两个相邻任务可连续执行且夹爪时序正确；
- 现场轨迹已有至少两份、位于不同存储介质的备份。

## 13. 权威参考

- [GitHub：从远程仓库获取代码](https://docs.github.com/en/get-started/using-git/getting-changes-from-a-remote-repository)
- [Microsoft：Windows OpenSSH 安装与使用](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse)
- [Ubuntu Server：网络配置](https://documentation.ubuntu.com/server/how-to/networking/)
- [Netplan Wi-Fi 示例](https://netplan.readthedocs.io/en/0.107/examples/)
- [AgileX Piper SDK](https://github.com/agilexrobotics/piper_sdk)
