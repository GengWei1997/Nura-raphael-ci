# nura-webui

小米 Redmi K20 Pro（`xiaomi-raphael`）的 Web 管理面板，跑在 Nura 上。
只用 Python 标准库，不需要额外的运行时。

浏览器打开 `http://<设备地址>:16888/`。左侧是分类，右侧只显示当前那一页。

| 分类 | 内容 |
|---|---|
| 概览 | 电池、供电详情、内存、存储、运行时间、CPU 三簇与运行模式、GPU |
| 温度 | 28 个热区，各自的降频阈值 |
| 网络 | Wi-Fi、USB 网络、全部接口地址 |
| 服务 | systemd 单元，失败的高亮 |
| 容器 | Docker 容器，可启动、暂停、停止 |
| 防火墙 | nftables 入站规则，可添加、编辑、删除 |
| 进程 | 按 CPU 或内存排序，可结束自己的进程 |
| 设置 | 设备功能开关，目前是自动熄屏 |

界面默认简体中文，右上角可切英文。语言、刷新间隔和当前分类存在浏览器里，
也认 URL 里的 `#containers` 这类锚点。

## 安装

```sh
scp nura-webui-*.apk user@<设备地址>:/tmp/
ssh user@<设备地址> 'sudo apk add --allow-untrusted /tmp/nura-webui-*.apk'
```

装完服务就起来了，`nftables` 的放行规则也一起生效。

## 网络页的速率

Wi-Fi 卡片显示名称、信号、频段、信道、地址和 MAC，不显示速率。

原因是这台机器上拿不到真实的链路速率：

- `nmcli` 的 `RATE` 是接口的理论最大值，不是协商值。扫描时**未连接**的 2.4G 网络也会报 `1170 Mbit/s`，而实际连着的 5G 只有 `270 Mbit/s`，两个数字放在一起没有可比性。
- `iwconfig` 报 `no wireless extensions`（高通驱动走 cfg80211，不实现 wireless-extensions）
- `iw` 没装；`/sys/class/net/wlan0/speed` 返回 `Invalid argument`
- `/sys/kernel/debug/ieee80211/phy0/` 是空的，驱动不填 station 信息
- `wpa_cli` 没有控制 socket，NetworkManager 用 D-Bus 直连

待机时实际吞吐只有 `0.01 Mbit/s` 左右，标一个「270 Mbit/s」会误导。频段和信道是当前关联的确定事实，所以用它们替代。

```sh
scp nura-webui-*.apk user@<设备地址>:/tmp/
ssh user@<设备地址> 'sudo apk add --allow-untrusted /tmp/nura-webui-*.apk'
```

装完服务就起来了，`nftables` 的放行规则也一起生效。

## 运行模式

概览的处理器卡片里有三个模式，对应一个 `/usr/libexec/nura-cpufreq mode <模式>` 调用：

| 模式 | governor | scaling_max_freq |
|---|---|---|
| 性能 | `performance` | 硬件上限 |
| 均衡（默认） | `schedutil` | 硬件上限 |
| 省电 | `schedutil` | 硬件上限的 60%，向下取最近的档位 |

这个内核没有 `powersave` governor（只有 `ondemand`、`userspace`、`performance`、
`schedutil`），所以省电是靠 `schedutil` 加 `scaling_max_freq` 封顶实现的，
三簇分别限到 1036 / 1401 / 1612 MHz。

性能和均衡只差在空载：性能把三簇钉死在 1785 / 2419 / 2841 MHz，待机也不降；
均衡空载时降到 1209 / 1612 / 1804 MHz。满载时两者完全一样，
所以性能模式的代价是持续发热和耗电。

GPU 不做调节：`target_freq` 是 `-rw-r--r--`，root 也写不进去，`userspace` 目录
也不存在，面板只显示当前频率和上限。

sysfs 都是 `root:root 0644`，面板以 uid 10000 运行写不了，所以走
`/usr/libexec/nura-cpufreq` 这个固定脚本。sudoers 里对它的授权是
`/usr/libexec/nura-cpufreq mode *`，看着宽，但脚本只接受三个固定模式名，
其它参数一律拒绝，不会把用户的输入拼进 sysfs 路径。

## 端口与防火墙

面板监听 `0.0.0.0:16888`。Nura 的 nftables 默认策略是 `input drop`，所以包里有
`/etc/nftables.d/60_nura-webui.nft` 负责每次开机放行；安装时如果系统已经在运行，
`post-install` 会直接把规则插进当前 ruleset，不用重启。

规则按注释去重，重复安装不会堆叠。

防火墙页可以直接改 `inet filter input` 链：添加、编辑、删除都走 `nft`。
编辑用 `replace rule ... handle N`，handle 不变，规则位置也不变，
所以不会把新规则挪到链尾。

服务带 `AmbientCapabilities=CAP_NET_ADMIN`，靠能力位操作 nft，不以 root 运行。

面板添加的规则只活在当前 ruleset 里，重启 nftables 会丢；只有
`/etc/nftables.d/` 下那条 16888 是持久的。

## 服务

`nura-webui.service` 以 uid 10000 运行，不属于 root：

- 进程列表里只有自己的进程能结束，面板自身和 PID 1 受保护
- 加入 `docker` 组，以便读 `docker.sock`

Nura 用的是 `99-default.preset`（`disable *`）加上按文件名顺序、先命中先生效的
预设，所以 `90-nura-webui.preset` 必须排在它前面，服务才会自动启用。

## 设置

「设置」页是设备功能开关。开关动的是真实的 systemd 单元，所以关掉之后
重启也不会自己回来。

### 自动熄屏

控制 `blank_screen.service`：开机 15 秒后执行 `setterm --blank force` 关掉屏幕，
用来减少 OLED 烧屏和待机耗电。

这个单元由用户在设备上自己创建，不是面板的一部分：

```sh
sudo tee /etc/systemd/system/blank_screen.service > /dev/null <<'EOF'
[Unit]
Description=Auto-blank screen 15s after boot
After=multi-user.target

[Service]
Type=simple
ExecStart=/bin/sh -c '/bin/sleep 15; exec env TERM=linux setterm --blank force </dev/tty1'

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable blank_screen.service
```

面板上的「开」只是 `systemctl enable`，下次开机才熄屏——如果在这里直接
`--now`，一点开关屏幕就会在 15 秒后黑掉，看面板的人会莫名其妙。「关」用的
是 `disable --now`，会顺手停掉正在倒计时的那个 sleep。

### 权限

面板以 uid 10000 运行，改 systemd 单元需要 root。这里不用 sudoers 放行整个
`systemctl`，而是只授权两条完整命令：

```
#10000 ALL=(root) NOPASSWD: \
    /usr/bin/systemctl enable blank_screen.service, \
    /usr/bin/systemctl disable --now blank_screen.service
```

sudo 是按完整命令行匹配的，所以 `systemctl restart sshd`、`enable evil.service`、
多加一个参数，全都会被拒绝并要求密码。按 uid 而不是用户名授权，是因为
pmbootstrap 装出来的账户名不固定，uid 10000 是固定的。

服务单元里为此做了两处调整，都在 `nura-webui.service` 里写了注释：

- `NoNewPrivileges=no`：开着这个标志 sudo 直接报
  `the "no new privileges" flag is set`，根本跑不起来
- `CapabilityBoundingSet` 补上 `CAP_SETUID CAP_SETGID CAP_SETPCAP CAP_AUDIT_WRITE`，
  这是 sudo 切换成 root 需要的；`CAP_SYS_ADMIN`、`CAP_DAC_OVERRIDE` 之类仍然排除在外

`ProtectSystem=strict` 保持不动：`systemctl enable` 是让 PID 1 去建符号链接，
面板进程自己并不写 `/etc`，实测不受只读影响。

## 构建

放进 pmaports 后用 pmbootstrap 构建：

```sh
cp -a nura-webui ~/.local/var/pmbootstrap/cache_git/pmaports/main/
pmbootstrap build nura-webui --arch aarch64
```

产物在 `~/.local/var/pmbootstrap/packages/edge/aarch64/nura-webui-<版本>.apk`。

改了源码记得更新 `APKBUILD` 里的 `sha512sums` 和 `pkgrel`。
