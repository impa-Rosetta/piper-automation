# AI 辅助同机树莓派交接手册

本文适用于以下明确场景：新同事更换了一台 Windows 电脑，但继续使用原项目已经调通的
同一块树莓派、同一台 Piper、同一个 USB-CAN 和同一个 STM32 夹爪控制器。

目标不是让 AI 自主操作机械臂，而是让 AI 根据标准化诊断日志帮助完成电脑配置、连接
排查和软件使用，使新电脑达到原操作电脑相同的工作状态。

## 1. 已经保存在树莓派上的内容

同一块树莓派已经具备：

- Ubuntu 22.04 ARM64；
- Linux 用户 `piper`，主机名 `piper-pi`；
- 项目目录 `/home/piper/piper_robot_project`；
- Python 环境 `~/.venvs/piper_robot_project_api`；
- `piper_sdk 1.0.0`；
- `can0.service` 和 1 Mbps SocketCAN 配置；
- `/dev/piper_gripper` 夹爪稳定设备名；
- 已录制的工厂任务、轨迹、夹爪时间线和公共锚点。

这些内容不会因为换 Windows 电脑而消失。树莓派的 IP 可能随热点或路由器变化，Linux
用户名和密码不会因为换网络改变。

## 2. 安全边界

AI 可以帮助：

- 解释终端日志；
- 检查项目路径、SSH、依赖、CAN 和串口权限；
- 修改 Windows 工作台配置；
- 给出人工确认后的低速测试步骤。

AI 不应在无人观察时：

- 使能或移动机械臂；
- 打开或闭合带负载夹爪；
- 绕过急停、碰撞、通信或反馈异常；
- 删除、覆盖现场轨迹；
- 把密码、热点密钥、私钥或现场坐标提交到公开 GitHub。

第一次真实运动必须由现场人员清空工作区、手握急停，并从单任务低速验证开始。

## 3. 新 Windows 电脑首次配置

安装 Git、Python 3.10 以上版本和 Windows OpenSSH Client，然后在 PowerShell 执行：

```powershell
git --version
python --version
ssh -V

cd D:\robot
git clone https://github.com/impa-Rosetta/piper-automation.git
cd piper-automation
```

从路由器或热点设备列表找到树莓派当前 IP。假设为 `<PI_IP>`：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure_windows_workstation.ps1 `
  -PiAddress <PI_IP> `
  -PiUser piper `
  -RemoteRoot /home/piper/piper_robot_project
```

此处只输入一次树莓派 Linux 登录密码。脚本会为当前 Windows 账户创建独立 SSH 密钥，
以后工作台不应反复要求密码。

验证：

```powershell
ssh piper-pi "whoami; hostname; hostname -I"
```

应看到用户 `piper`、主机 `piper-pi`。若提示 `administrator@piper-pi`，说明 SSH 用户
配置错误，不要继续运行机械臂命令。

## 4. 一键生成 AI 诊断日志

在 Windows 仓库目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\collect_handover_diagnostics.ps1
```

脚本只读取状态，不会使能机械臂、发送关节命令或控制夹爪。日志保存到：

```text
records\handover_diagnostics\handover_diagnostics_YYYYMMDD_HHMMSS.log
```

通过标准至少包括：

- `project=present`；
- `piper_sdk` 版本可见；
- USB 列表同时存在 `1d50:606f` 和 `0483:5740`；
- `can0.service` 为 `active`；
- `can0` 为 `UP`、`ERROR-ACTIVE`、`1000000` bit/s；
- `gripper_access=read-write`；
- `task_count` 与树莓派现场已有任务一致；
- Piper 反馈不是持续全零，状态正常；
- 最后一行 `check_rc=0`。

将日志发给 AI 时先检查内容，不要附加任何密码、私钥或 Wi-Fi 密钥。

## 5. 启动工作台并恢复相同现场状态

双击：

```text
start_piper_windows_workbench.bat
```

确认顶部参数：

```text
SSH 主机:    piper-pi
远程项目:    /home/piper/piper_robot_project
CAN:         can0
夹爪端口:    /dev/piper_gripper
```

严格按以下顺序操作：

1. 点击“连接与设备检查”；
2. 点击“从树莓派拉回现场数据”；
3. 确认任务列表、层号和孔位与树莓派一致；
4. 确认公共给料上方点、零位 Home 和任务文件已经拉回；
5. 此后才能点击“同步程序到树莓派”；
6. 先做一个已验证任务的低速 C 联合试跑；
7. 再测试两个连续任务；
8. 最后才允许整层连续生产。

公开 GitHub 不包含真实轨迹和现场坐标。因此，“从 GitHub 克隆完成”不等于“现场任务
已经恢复”；同事必须从同一块树莓派拉回私有任务数据。

## 6. 当前树莓派的更新方式

当前交付树莓派采用工作台归档同步，项目目录可能没有 `.git`。出现：

```text
deployment=archive/no-git (supported)
```

属于正常状态。不要在树莓派执行 `git pull`，也不要为了获得 `.git` 删除现有项目目录。
正确更新方法是：

```powershell
cd D:\robot\piper-automation
git pull
```

然后打开 Windows 工作台：先拉回现场数据，再同步程序到树莓派。

## 7. 可直接复制给 AI 的上下文

新同事可以把下面内容与诊断日志一起发给 AI：

```text
我正在接手 AgileX Piper 自动化项目，并继续使用原来已经调通的同一块树莓派。

架构：
- Windows 只运行可视化工作台，通过 SSH/SCP 管理文件；不直接控制 CAN 或夹爪串口。
- 树莓派主机名 piper-pi，Linux 用户 piper。
- 树莓派项目目录 /home/piper/piper_robot_project。
- Python 环境 ~/.venvs/piper_robot_project_api。
- Piper 使用 SocketCAN can0，波特率 1000000。
- DIY 夹爪端口 /dev/piper_gripper。
- 真实轨迹只保存在树莓派和内部备份，不在公开 GitHub。
- 树莓派是 archive/no-git 部署，不能在其项目目录执行 git pull。

请先根据我附上的 handover diagnostics 日志定位问题。优先做只读检查，不要让我删除
轨迹、重装树莓派、提高安全阈值或直接发送运动命令。只有 CAN、夹爪权限和非零机械臂
反馈都正常后，再给出需要人工确认的低速测试步骤。
```

## 8. 常见问题与 AI 应对顺序

### SSH 每次要求密码

重新运行 `configure_windows_workstation.ps1`。不要把上一位同事的私钥复制到新电脑。

### `can0` 不存在

先看诊断日志的 USB 部分。USB 没有 `1d50:606f` 时检查接线和供电；USB 存在时执行：

```powershell
ssh piper-pi "sudo systemctl restart can0.service; ip -br link show can0"
```

### 夹爪不存在或无权限

确认 USB 中存在 `0483:5740`，并检查：

```powershell
ssh piper-pi "ls -l /dev/piper_gripper; id"
```

不要长期依赖 `chmod 777`；应使用项目 udev 规则和 `dialout` 用户组。

### 工作台没有任务

先确认远程路径是 `/home/piper/piper_robot_project`，然后执行“从树莓派拉回现场数据”。
不要先点“同步程序到树莓派”。

### 机械臂反馈持续全零

停止后续运动操作，检查机械臂电源、急停、CAN 接线、终端电阻、波特率及是否存在第二个
控制进程。不能用反复使能掩盖全零反馈。

## 9. 达到原操作环境的验收清单

- [ ] 新电脑可执行 `ssh piper-pi` 且无需密码；
- [ ] AI 诊断脚本返回 `check_rc=0`；
- [ ] 工作台远程路径为 `/home/piper/piper_robot_project`；
- [ ] 工作台任务数量与树莓派一致；
- [ ] Home、给料上方和任务数据已拉回；
- [ ] 单任务低速联合试跑通过；
- [ ] 两个连续任务低速试跑通过；
- [ ] 工作台日志和现场数据已完成一次备份；
- [ ] 操作员知道物理急停、软件停止和故障恢复流程。

完成以上项目后，新电脑才可以视为达到原操作电脑相同的可用状态。
