# Cloudflare 隧道部署（演示用）

> 目标：把本机 Docker 里跑的系统暴露成一个公网 HTTPS 地址，供评委/他人访问。
> **业务代码零改动**，只动部署层。

---

## 一、为什么用隧道，而不是把后端搬上云

Cloudflare Containers 确实能跑 Docker 镜像（本项目的 backend 镜像 3.23 GB、
单 worker 实测 407 MB 内存，都在限额内）。但有两处硬伤：

| 问题 | 官方说明 | 对本项目的后果 |
|---|---|---|
| **磁盘是临时的** | *"All disk is ephemeral. When a Container instance goes to sleep, the next time it is started, it will have a fresh disk."* | SQLite 库、模型文件、`data/.auth_secret` 全部丢失 |
| **没有 docker-compose** | 每个容器是独立 Durable Object，`redis://redis:6379` 解析不了 | 5 个 Celery 接口（重训/批量打分/KMeans/肘部法/聚类保存）全废 |

**实测（用全新磁盘走启动流程）**：

```
customers   96418   ← 播种回来了（7.7 秒）
users           4   ← 播种回来了
work_orders     0   ← 空的
audit_logs      0   ← 空的
```

启动链路里**没有任何地方调用 `seed_work_orders`**
（已 grep `main.py` / `preseed.py` / `run.py`）。
即云端冷启动后「全部工单 / 已闭环工单 / ROI / 挽留成功率」全是 0。

> ⚠ 这是一个**独立于部署方式**的真实缺口：**任何**新建数据库都会遇到。
> 与是否上云无关，值得单独修。

所以演示场景选择隧道：后端留在本机，逻辑一行不动。

---

## 二、前置条件

- 本机已 `docker compose up -d`，且 4 个容器 running
- `cloudflared` 可执行文件（见下）

---

## 三、一次性准备

### 1. 允许隧道域名访问 Vite

Vite 默认只接受 `localhost` / IP 的 Host 头，其余返回：

```
403 Blocked request. This host ("xxx.trycloudflare.com") is not allowed.
```

`frontend/vite.config.js` 的 `server` 里加：

```js
// 前导点 = 通配子域。不要用 `true` —— 那等于接受任意 Host 头。
allowedHosts: ['.trycloudflare.com'],
```

⚠ **必须同时把它挂进容器**。`docker-compose.yml` 里 frontend 原先只挂了
`./frontend/src`，改配置文件不会生效（表现为「明明加了却仍 403」）：

```yaml
volumes:
  - ./frontend/src:/app/src
  - ./frontend/vite.config.js:/app/vite.config.js   # 新增
```

生效需重建容器：

```powershell
docker compose up -d frontend
```

### 2. 安装 cloudflared

```powershell
$dir = "$env:LOCALAPPDATA\cloudflared"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Invoke-WebRequest `
  -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
  -OutFile "$dir\cloudflared.exe" -UseBasicParsing
```

> 实测：**直连可下（HTTP 200），走系统代理反而超时**。
> 下载失败时先试去掉代理。

---

## 四、启动隧道

```powershell
& "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" tunnel --url http://localhost:5173 --no-autoupdate
```

输出里找这一行即公网地址：

```
|  https://<随机词>.<随机词>-<随机词>-<随机词>.trycloudflare.com  |
```

**只转发 5173（前端）**，不转发 8000。前端 `/api` 由 Vite 代理到
`backend:8000`（容器内网），浏览器只看到同源请求，**CORS 无需改动**。

---

## 五、验收

```powershell
# 1) 首页应 200
Invoke-WebRequest "https://<你的地址>" -UseBasicParsing | Select-Object StatusCode

# 2) 未鉴权访问业务接口应 401（证明后端连通且鉴权生效）
Invoke-WebRequest "https://<你的地址>/api/customers?limit=1" -UseBasicParsing

# 3) 浏览器端完整链路
Copy-Item docs\audit\verify-tunnel.mjs "$env:TEMP\pw-probe\" -Force
$env:CHROME_PATH="$env:USERPROFILE\AppData\Local\ms-playwright\chromium-1243\chrome-win64\chrome.exe"
cd "$env:TEMP\pw-probe"; node verify-tunnel.mjs "https://<你的地址>"
```

`verify-tunnel.mjs` 是**只读**脚本：不建单、不改单、不删单，
并会断言「业务写请求 = 0」。实测 9/9 通过。

---

## 六、必须知道的三件事

### 1. 电脑必须开着

隧道进程和后端都在你本机。合盖休眠 = 地址失效。
Quick Tunnel 还有 **200 并发上限**。

### 2. URL 每次重启都变

`*.trycloudflare.com` 是随机子域，**重启即换**。
要固定地址 → 需要一个托管在 Cloudflare 的域名 + 命名隧道（NS 必须改到 Cloudflare）。

### 3. ⚠ 安全：挂上公网前请先堵这三个口子

当前配置是为**演示**打开的，公开部署后风险显著上升：

| 项 | 现状（实测） | 风险 |
|---|---|---|
| **Redis 6379** | `protected-mode no`、`requirepass` 空、`bind *` | 攻击者可直接操作 Celery 队列 → **在容器内执行任意代码**。这是最严重的一项 |
| **`AUTH_DEMO_SHOW_TOTP`** | `.env` 里 `true`，登录页还写着 `zhaomin / Bank@2026` | 拿到口令 → 换临时票据 → `/api/auth/demo/totp` 拿动态口令（该端点**在免鉴权白名单里**）→ 登录成功。**第二因子等于不存在**，且它连 `secret` 原始密钥一起返回 |
| **审计日志** | 380 条，含真人登录 IP/时间 | 真实数据（其余为客户模拟数据） |

加固都很小，且**不碰业务逻辑**：

1. Redis 加密码 + 只绑回环（`docker-compose.yml`）
2. 关掉 `AUTH_DEMO_SHOW_TOTP`（`.env` 一行）
3. 登录页「演示账号」面板收进可配置开关

参考：`docs/audit/DEFECT-REPORT.md` §21.15（默认密钥漏洞）、§21.17（RBAC 缺口）。

> 系统本身底子是好的——实测未鉴权访问业务接口/Dashboard **均 401**，
> JWT 密钥随机落盘且权限 600，登录失败措辞不区分「用户不存在/口令错」，
> `pre_totp` 票据无法访问业务接口。问题只在上述三个**演示便利开关**。

---

## 七、停止

```powershell
Get-Process cloudflared | Stop-Process      # 停隧道（地址随即失效）
docker compose down                          # 停整套服务
```
