# nura-webui

小米 Redmi K20 Pro（`xiaomi-raphael`）的 Web 管理面板，跑在 Nura 上。
只用 Python 标准库，不需要额外的运行时。

浏览器打开 `http://<设备地址>:16888/`。左侧是分类，右侧只显示当前那一页。

| 分类 | 内容 |
|---|---|
| 概览 | 系统信息（发行版、架构、内核）、电池、供电详情、内存、存储、运行时间、CPU 三簇与运行模式、GPU |
| 温度 | 28 个热区，各自的降频阈值 |
| 网络 | Wi-Fi、USB 网络、全部接口地址 |
| 服务 | systemd 单元，失败的高亮 |
| 容器 | Docker 容器，可启动、暂停、停止 |
| 防火墙 | nftables 入站/出站规则，可添加、编辑、删除 |
| 进程 | 按 CPU 或内存排序，可结束自己的进程 |
| 设置 | 设备功能开关（自动熄屏、直供电模式）与面板版本、设备、主机名 |

界面默认简体中文，右上角可切英文。语言、刷新间隔和当前分类存在浏览器里，
也认 URL 里的 `#containers` 这类锚点。

## 概览里的系统信息

概览最上面一条是发行版信息，全部来自 `/etc/os-release` 和 `uname`：
`NAME` 作标题、`VERSION` 作副标题、`ID_LIKE` 作右上角的 family 标记，
右边是架构和内核。改 `/etc/os-release` 里的名字，面板不用动就会跟着变。

左上角的标志取自官网 `https://nura.eco/static/img/2026-09/logo-new.svg`，
只保留三角形部分（去掉 "Nura" 字样），内联进 HTML，`nura-logo.svg` 是
源文件留档、只参与校验和，不装进包里。

面板自己的版本号显示在设置页底部，从 `/lib/apk/db/installed` 里读
`nura-webui` 的 `V:` 字段，所以它永远等于实际装上的那个包，不需要在代码里
再维护一份。开发时直接 scp 上去的文件没有包记录，那里就显示 `—`。

## 安装

```sh
scp nura-webui-*.apk user@<设备地址>:/tmp/
ssh user@<设备地址> 'sudo apk add --allow-untrusted /tmp/nura-webui-*.apk'
```

装完服务就起来了，Docker 也一起装好并设为开机自启，`nftables` 的放行规则
同时生效。已经装过旧版时，apk 会走 `post-upgrade`，同样会拉起 Docker 并重启
面板——两条路径都要维护，缺一个就会在对应场景静默跳过。

详见下面的「Docker 与容器页」。

## 网络页的速率

Wi-Fi 卡片显示名称、信号、频段、信道、地址和 MAC，不显示速率。

原因是这台机器上拿不到真实的链路速率：

- `nmcli` 的 `RATE` 是接口的理论最大值，不是协商值。扫描时**未连接**的 2.4G 网络也会报 `1170 Mbit/s`，而实际连着的 5G 只有 `270 Mbit/s`，两个数字放在一起没有可比性。
- `iwconfig` 报 `no wireless extensions`（高通驱动走 cfg80211，不实现 wireless-extensions）
- `iw` 没装；`/sys/class/net/wlan0/speed` 返回 `Invalid argument`
- `/sys/kernel/debug/ieee80211/phy0/` 是空的，驱动不填 station 信息
- `wpa_cli` 没有控制 socket，NetworkManager 用 D-Bus 直连

待机时实际吞吐只有 `0.01 Mbit/s` 左右，标一个「270 Mbit/s」会误导。频段和信道是当前关联的确定事实，所以用它们替代。

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

防火墙页可以改 `inet filter` 的 `input` 和 `output` 两条链：添加、编辑、删除
都走 `nft`。入站规则匹配来源（`ip saddr`），出站规则匹配目的（`ip daddr`），
同一个“地址”输入框按所选的链自动切换。

编辑用 `replace rule ... handle N`，handle 不变，规则位置也不变，
所以不会把新规则挪到链尾。

链名只接受 `input` 和 `output` 两个值，其它一律拒绝——`forward` 和 Docker 自己
建的链面板不能碰，误删会让容器网络断掉。

服务带 `AmbientCapabilities=CAP_NET_ADMIN`，靠能力位操作 nft，不以 root 运行。

面板添加的规则只活在当前 ruleset 里，重启 nftables 会丢；只有
`/etc/nftables.d/` 下那条 16888 是持久的。

## 服务

`nura-webui.service` 以 uid 10000 运行，不属于 root：

- 进程列表里只有自己的进程能结束，面板自身和 PID 1 受保护
- 加入 `docker` 组，以便读 `docker.sock`（见下一节）

Nura 用的是 `99-default.preset`（`disable *`）加上按文件名顺序、先命中先生效的
预设，所以 `90-nura-webui.preset` 必须排在它前面，服务才会自动启用。

## Docker 与容器页

Docker 是硬依赖（`depends="python3 nftables docker"`），装面板时一起装好，
并设为开机自启。

这个依赖不只是为了容器页有数据。`nura-webui.service` 里写着
`SupplementaryGroups=docker`，而 systemd 在 fork 之前就要解析这个组名：
**组不存在时整个单元直接启动失败**，日志是

```
Failed to determine supplementary groups: No such process
Failed at step GROUP spawning /usr/bin/python3
status=216/GROUP
```

进程根本没跑起来，面板不监听端口，浏览器打不开。`docker-engine` 包的
pre-install 会 `addgroup -S docker`，所以只要 Docker 装上，组就一定存在。
不要为了“没有容器需求”把依赖去掉——那会让面板彻底起不来。

### 为什么要写进 preset 而不只是 post-install

只在 `post-install` 里 `systemctl enable docker` 是不够的。apk 装完本包后会跑
`postmarketos-base-systemd` 触发器，它按 `99-default.preset` 把整个预设集重新
应用一遍，刚建好的 `docker.service` 启动链接随即被 `disable *` 删掉——面板能起来，
Docker 却停在 `disabled`，容器页永远是空的。

所以 `90-nura-webui.preset` 里同时启用三个单元：

```
enable nura-webui.service
enable docker.service
enable docker.socket
```

`docker.socket` 要单独列出来。实测升级后有 `docker` 为 `enabled`、`docker.socket` 仍为
`disabled` 的情况——预设只在新增单元时才应用，升级时不会重跑。这种状态下把
`docker.service` 停掉再拉起，`dockerd` 会以
`no sockets found via socket activation: make sure the service was started by systemd`
退出，`docker.service` 随即进入 `failed` 并触发重启次数上限。显式启用 socket
可以避免这个状态，所以三个单元一起 enable。

post-install 和 post-upgrade 里仍然各写一遍 `systemctl enable docker.socket
docker.service`，用来兜住升级路径和预设不生效的场合。

### 权限

`docker.sock` 是 `root:docker 0660`，面板靠 `SupplementaryGroups=docker`
拿到组权限，不需要额外能力位。会话里 `id` 看不到 docker 组是正常的——那是
systemd 给服务进程单独加的补充组，不进 `/etc/group` 的成员列表。

容器页可以启动、暂停、停止容器。动作要匹配当前状态，否则直接拒绝；
容器名必须是面板自己列举出来的那一个，不接受任意输入。

## 设置

「设置」页是设备功能开关。开关动的是真实的 systemd 单元，所以关掉之后
重启也不会自己回来。

### 自动熄屏

`blank_screen.service` 睡 15 秒后调 `/usr/libexec/nura-blank on` 熄屏。
开机和面板开关走的是同一条路径，所以**两边都是 15 秒**。

- **开启**：`enable --now` 启动单元，15 秒后熄屏
- **关闭**：`disable --now` 终止仍在计时的 sleep，并立即点亮屏幕

`RemainAfterExit=yes` 让单元熄屏后保持 `active`，这样开关显示的状态和实际情况
一致，`ExecStop` 也有东西可以撤销。`Type=simple` 不会阻塞 `multi-user.target`，
所以开机时的 15 秒不会拖慢启动。

`setterm` 通过 stdin 和 console 对话，所以脚本里显式重定向 `</dev/tty1`，
否则它读到没有终端就直接退出，什么都不做。

### 权限

面板以 uid 10000 运行，改 systemd 单元需要 root。这里不用 sudoers 放行整个
`systemctl`，而是只授权几条完整命令：

```
#10000 ALL=(root) NOPASSWD: \
    /usr/bin/systemctl enable --now blank_screen.service, \
    /usr/bin/systemctl disable --now blank_screen.service, \
    /usr/bin/systemctl enable --now direct_power.service, \
    /usr/bin/systemctl disable --now direct_power.service
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
