# 树莓派换热点与网络交接

树莓派的用户名、密码与网络无关；更换热点只会改变网络连接和通常由 DHCP 分配的 IP。
项目不把 Wi-Fi 名称和密码写进 GitHub。

## 推荐交接策略

工厂部署不要长期依赖个人手机热点。优先级建议：

1. 工厂路由器有线连接 + DHCP 地址保留；
2. 工厂专用 Wi-Fi + DHCP 地址保留；
3. 专用随身路由器；
4. 手机热点只作为调试和应急手段。

建议保留主机名 `piper-pi`，但 Windows 工作台最终仍以 SSH 配置中的地址为准。

## 场景 A：当前还能 SSH

先连接树莓派：

```powershell
ssh <PI_USER>@<OLD_IP>
```

查看网络管理方式：

```bash
ip -br link
ip route
command -v nmcli || true
sudo netplan get
```

项目脚本会自动优先使用 NetworkManager；没有 `nmcli` 时使用 Netplan：

```bash
cd ~/piper-automation
bash scripts/configure_pi_wifi.sh --ssid "NEW_SSID"
```

脚本交互读取密码，不会把密码写入 shell 历史。切换网络后 SSH 可能立即断开。

在新的路由器/热点设备列表中找到 `piper-pi` 的新 IP，然后在 Windows 修改：

```text
%USERPROFILE%\.ssh\config
```

对应内容：

```sshconfig
Host piper-pi
    HostName <NEW_IP>
    User <PI_USER>
    IdentityFile ~/.ssh/id_ed25519
```

测试：

```powershell
ssh piper-pi "hostname; hostname -I"
```

## 场景 B：旧热点失效，但可以接网线

1. 树莓派断电；
2. 树莓派网口接入与 Windows 同一台路由器；
3. 上电等待约 1 到 2 分钟；
4. 在路由器 DHCP 客户端列表查找 `piper-pi`；
5. 用新 IP 登录，然后按场景 A 添加 Wi-Fi。

不要因为 Wi-Fi 失效就立即重装系统。项目、SDK 和现场轨迹通常仍完整保存在 TF 卡中。

## 场景 C：没有网络，但有显示器和键盘

登录树莓派本地终端，执行：

```bash
hostname
ip -br link
cd ~/piper-automation
bash scripts/configure_pi_wifi.sh --ssid "NEW_SSID"
hostname -I
```

然后从 Windows SSH 到显示的新 IP。

## 场景 D：完全失联，只能处理 TF 卡

Windows 默认不能直接可靠编辑 Ubuntu 的 ext4 根分区。推荐按以下顺序：

1. 优先借用网线、micro-HDMI 显示器和 USB 键盘；
2. 或把 TF 卡挂载到另一台 Linux 电脑，备份 `/home/<PI_USER>/piper-automation`；
3. 最后才考虑重新烧录；
4. 重装前必须确认已备份 `teach/production_tasks` 和锚点文件。

重新烧录后需要重新执行完整安装教程，原轨迹也必须从内部备份恢复。

## 手动 Netplan 配置参考

仅在确认系统使用 Netplan、并且有网线或本地控制台兜底时操作：

```bash
ip -br link
sudo nano /etc/netplan/99-piper-wifi.yaml
```

示例：

```yaml
network:
  version: 2
  renderer: networkd
  wifis:
    wlan0:
      dhcp4: true
      optional: true
      access-points:
        "NEW_SSID":
          password: "NEW_PASSWORD"
```

设置权限并应用：

```bash
sudo chmod 600 /etc/netplan/99-piper-wifi.yaml
sudo netplan generate
sudo netplan apply
```

Wi-Fi 密码会以系统配置形式保存在树莓派本地，因此 TF 卡也应视为敏感资产。

## IP 稳定性

最推荐在路由器按树莓派 Wi-Fi 或网卡 MAC 地址设置“DHCP 地址保留”，而不是在树莓派
写死静态 IP。这样换网时不会把旧网段地址带到新网络。

查看 MAC：

```bash
ip link show wlan0
ip link show eth0
```

## 原操作员离开前的账号交接

1. 新同事生成自己的 SSH 公钥并加入 `~/.ssh/authorized_keys`；
2. 新同事验证免密登录；
3. 更换树莓派登录密码；
4. 删除原操作员的 SSH 公钥；
5. 删除个人热点配置；
6. 将路由器、树莓派、GitHub 和现场数据备份的责任人写入内部资产表。

删除旧 Wi-Fi 前先确认新 Wi-Fi 可用。NetworkManager 环境可查看并删除：

```bash
nmcli connection show
sudo nmcli connection delete "OLD_CONNECTION_NAME"
```
