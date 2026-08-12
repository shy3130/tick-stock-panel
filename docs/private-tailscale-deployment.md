# Tailscale 私网 HTTPS 部署

本方案只把 TickFlow 发布到用户自己的 Tailscale 私网，不开放公网端口，不使用
Tailscale Funnel，也不修改路由器端口映射。生产应用继续只绑定宿主机
`127.0.0.1:3018`，并保留 TickFlow 自己的密码认证。

## 一次性接入

1. 在 Windows、手机和平板安装 Tailscale，并使用同一个私人账户登录。
2. Windows 完成官方授权后确认状态：

   ```powershell
   & 'C:\Program Files\Tailscale\tailscale.exe' status --json
   ```

3. 仅在状态为 `Running` 后启用私网 Serve：

   ```powershell
   & 'C:\Program Files\Tailscale\tailscale.exe' serve --https=443 --bg http://127.0.0.1:3018
   ```

4. 核验 Serve 已配置、Funnel 为空：

   ```powershell
   & 'C:\Program Files\Tailscale\tailscale.exe' serve status --json
   & 'C:\Program Files\Tailscale\tailscale.exe' funnel status --json
   ```

首次启用 HTTPS 时，Tailscale 可能要求用户在官方网页确认 HTTPS/MagicDNS。这个
确认涉及私人 tailnet，必须由账户本人完成。

## 安全边界

- Docker 宿主端口必须保持 `127.0.0.1:3018`，不得改成 `0.0.0.0`。
- `FORWARDED_ALLOW_IPS` 只允许 `127.0.0.1,172.21.0.1`；不得使用 `*` 或整个私网。
- Docker 默认网络固定为 `172.21.0.0/16`，网关为 `172.21.0.1`，与可信代理列表一致。
- 生产前端与 API 同源，Tailscale 的 `*.ts.net` 域名不应加入 CORS。
- CORS 只用于本机 Vite 开发来源：`http://127.0.0.1:3011` 和
  `http://localhost:3011`。
- HTTPS 登录 Cookie 必须包含 `Secure; HttpOnly; SameSite=Lax`；直接本机 HTTP
  开发则不带 `Secure`。

## 手机和平板验收

关闭手机 Wi-Fi、使用移动数据，确认 Tailscale 已连接后打开 Serve 给出的 HTTPS
地址。验收需要同时满足：证书正常、出现 TickFlow 登录页、密码登录成功、刷新后
会话仍有效。本方案不授权访问券商账户或提交任何交易。
