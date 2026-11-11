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
