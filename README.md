# nura-raphael-ci

给小米 Redmi K20 Pro / Mi 9T Pro（`xiaomi-raphael`）打包 [Nura](https://nura.eco)
镜像。apk 已经是构建好的，这里只做安装和打包，不编译。

## 用法

Actions 里手动跑 `Build Nura image for xiaomi-raphael`。

| 参数 | 默认 | |
|---|---|---|
| `version` | `7.3` | `packages/` 下的目录名 |
| `ui` | `phosh` | 桌面环境 |
| `locale` | `zh_CN.UTF-8` | |
| `timezone` | `Asia/Shanghai` | |
| `use_tuna_mirror` | 开 | 关掉用官方源 |
| `extra_packages` | `none` | 逗号分隔 |
| `user_name` | `user` | |
| `user_password` | `1234` | |
| `publish_release` | 开 | |

### UI 选项

`ui` 是下拉框，可选值就是 pmaports 里的 `postmarketos-ui-*`（去掉前缀）。

尽量选择主流的 UI。列表里的 UI 并不是没有 bug，详细参考 [Nura Wiki](https://wiki.nura.eco/wiki/Category:Interface)

| 值 | 说明 |
|---|---|
| `phosh` | GNOME 组件的手机界面（默认） |
| `gnome-mobile` | 适配手机的 GNOME Shell |
| `plasma-mobile` | KDE Plasma 手机版 |
| `sxmo-de-sway` / `sxmo-de-dwm` / `sxmo-de-i3` / `sxmo-de-river` | SXMO 极简移动环境 |
| `catacomb` | 低端硬件上的轻量界面 |
| `lomiri` | UBports 的融合桌面 |
| `gnome` | GNOME Shell |
| `plasma-desktop` | KDE Plasma 桌面版 |
| `cosmic` | System76 的 COSMIC |
| `sway` / `niri` | Wayland 合成器 |
| `xfce4` / `lxqt` / `mate` | 轻量 X11 桌面 |
| `i3wm` | X11 窗口管理器（需键盘） |
| `console` | 纯控制台 |
| `buffyboard` / `fbkeyboard` | 带触屏键盘的 framebuffer 控制台 |

## 产物

```
xiaomi-raphael-7.3-phosh.img.gz
boot.img
device-xiaomi-raphael-1-r0.apk
firmware-xiaomi-raphael-1-r0.apk
linux-xiaomi-raphael-7.3-r0.apk
```

刷机：

```bash
gunzip xiaomi-raphael-7.3-phosh.img.gz    # 解出 xiaomi-raphael.img
fastboot flash userdata xiaomi-raphael.img
fastboot flash boot boot.img
```

## nura-webui 面板

`nura-webui/` 是设备管理面板（WebUI，端口 16888）的源码，由
`Build nura-webui apk` 这个 workflow 编译成 apk。

不需要配置任何 secret。`abuild` 把签名写死在构建流程里，没有关掉签名的方法，
所以每次构建都现场生成一把**一次性密钥**，构建完随容器销毁。私钥不存在于任何
地方，设备也不认识它，因此装包时要带 `--allow-untrusted`。

### 构建

Actions 里跑 `Build nura-webui apk`：

| 参数 | 默认 | |
|---|---|---|
| `pkgrel` | 空 | 留空用 `nura-webui/APKBUILD` 里的值（初次是 `0`）；填数字则覆盖 |
| `publish_release` | 开 | 发布成 Release，tag 是 `nura-webui-1.0-r0` 这样 |

产物 `nura-webui-1.0-r0.apk` 会随 Release 一起发出来，也可以从 Actions 的
artifact 里下。

### 安装与升级

第一次装（设备上还没有这个包，或者是从旧的测试版降级）：

```bash
apk add --allow-untrusted nura-webui-1.0-r0.apk
```

之后每次发版，在 Actions 里把 `pkgrel` 递增（`1`、`2`、`3`……），得到
`1.0-r1`、`1.0-r2`，然后：

```bash
apk add --allow-untrusted --upgrade nura-webui-1.0-r1.apk
```

**`pkgrel` 必须递增，不能一直用 0。** apk 按版本号判断，同版本号装同版本号会
当成重装，覆盖了文件但不会走 upgrade 流程。

### 升级时会重启服务

`server.py` 只在进程启动时读一次，所以光换文件不会生效。包里带了两个脚本：

- `nura-webui.post-install`：全新安装时执行
- `nura-webui.post-upgrade`：替换已有版本时执行

apk **每次事务只挑其中一个**（源码里是 `(oldpkg == NULL) ? POST_INSTALL :
POST_UPGRADE`），所以两个都必须存在，否则升级会静默跳过重启，面板继续跑旧代码。
两个脚本都会 `systemctl restart nura-webui`，装完不用手动重启。

面板细节见 [`nura-webui/README.md`](nura-webui/README.md)。
