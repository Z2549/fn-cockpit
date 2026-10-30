# fn-cockpit — 飞牛 fnOS 的 systemd 服务图形管理面板

[![Release](https://img.shields.io/github/v/release/Z2549/fn-cockpit?label=%E7%89%88%E6%9C%AC)](../../releases/latest)
[![Platform](https://img.shields.io/badge/%E5%B9%B3%E5%8F%B0-%E9%A3%9E%E7%89%9B%20fnOS%20x86%2FARM-blue)](https://www.fnnas.com)

基于 [Cockpit](https://cockpit-project.org/) 的飞牛 fnOS 应用（fpk）：在飞牛网页桌面里图形化管理 systemd 服务——启停、开机自启、journal 日志、Web 终端、存储与网络一览。

**零端口暴露**：走飞牛统一应用网关（Unix Socket），不占用任何 TCP 端口，只有登录飞牛的用户才能访问。

## 功能

- 飞牛桌面图标点开即用（内嵌微应用窗口），与其他官方应用体验一致
- 服务启停 / 重启 / 开机自启开关
- 任意服务的 journal 日志查看、Web 终端
- Cockpit 自带的存储、网络、账户等页面同样可用
- 任意飞牛用户以其系统账号密码登录，无需额外配置

## 安装

1. 从 [Releases](../../releases/latest) 下载 `fn-cockpit-vX.Y.Z.fpk`
2. 飞牛「应用中心 → 手动安装」上传 fpk
3. 安装完成后桌面出现「服务管理 Cockpit」图标，点击即用

> 安装脚本会通过 apt 自动安装 Debian 官方源的 `cockpit` 套件；如系统已有 Cockpit 也会直接复用。

## 卸载

应用中心卸载即可：自动停止服务、删除 systemd drop-in 与 `cockpit.conf` 受管段（恢复出厂），并保留 cockpit 系统包；如需彻底移除请再执行 `apt remove cockpit cockpit-ws`。

## 工作原理

```
浏览器 /app/fn-cockpit/*
  → 飞牛统一应用网关（校验登录态，剥掉 /app/fn-cockpit 前缀）
    → app.sock (Unix Socket)
      → relay.py（补回前缀、重写 Origin、WebSocket 盲转发）
        → cockpit.sock (Unix Socket, systemd socket activation)
          → cockpit-ws（UrlRoot=/app/fn-cockpit）
```

全程无 TCP 监听；访问控制由飞牛网关的登录态把守，cockpit 内再用系统账号鉴权。

关键适配（踩坑实录，详见代码注释）：

| 坑 | 解法 |
|---|---|
| ui/config 的 url 不带尾斜杠会被网关兜底成 SPA 桌面页（假黑屏） | `url` 写成 `/app/fn-cockpit/` |
| 网关转发时改写 Host，cockpit 校验 Origin-vs-Host 失败，登录后 WebSocket 被拒 | relay 将 `Origin` 重写为 `https://<Host>` |
| Debian 12 的 cockpit.socket（cockpit-tls）只讲 TLS | relay/precheck 先试 TLS、失败自动回退明文 |
| UrlRoot 剥离后空路径触发 cockpit-ws 断言 ABRT | 空路径映射为 `PREFIX + "/"` |
| 桌面不以内嵌窗口打开 | manifest 声明 `micro_app="true"` |
| lifecycle 脚本用 `set -e` 会让 appcenter panic | 全部脚本禁用 `set -e`，失败仅记日志 |

## 目录结构

```
├── manifest              # fpk 包描述（micro_app、checkport=false 等）
├── app/
│   ├── ui/config         # 桌面条目（iframe + 网关 socket）
│   ├── server/relay.py   # 网关 ⇆ cockpit 的本地 HTTP/WS 中继
│   └── server/precheck.py# cockpit.sock 直连健康预检
├── cmd/                  # fpk 生命周期脚本（install/main/uninstall/upgrade…）
├── config/privilege      # 以 root 运行（管理 systemd 所需）
└── tests/test_relay.py   # relay 回归测试（Windows/TCP 环境亦可跑）
```

## 开发

```bash
# 打包（飞牛官方 fnpack）
fnpack build -d native-ui-application-sample

# 本地回归测试（无需 NAS）
python tests/test_relay.py
```

## License

MIT © 2026 Z2549
