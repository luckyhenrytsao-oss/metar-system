# 部署备忘

## 已配置的固定信息

- **联系邮箱**：`htsao2000@gmail.com`（已写入 `app/config.py`、`.env.example`、`docker-compose.yml`、`scripts/vps-first-deploy.sh`）
- **GitHub 账户**：`https://github.com/luckyhenrytsao-oss`
- **仓库名**：`metar-system`
- **GHCR 镜像地址**：`ghcr.io/luckyhenrytsao-oss/metar-system:latest`
- **VPS 公网 IP**：`47.251.25.183`
- **VPS SSH 端口**：`2222`
- **VPS 用户**：`root`

## 已完成的部署状态

- [x] GitHub 仓库已创建：`https://github.com/luckyhenrytsao-oss/metar-system`
- [x] 本地代码已 push 到 `main`
- [x] GitHub Actions Secrets 已配置（`GHCR_TOKEN`、`VPS_HOST`、`VPS_PORT`、`VPS_USER`、`VPS_SSH_KEY`）
- [x] VPS 首次手动部署已完成
- [x] Nginx 反向代理已配置：
  - 公网 80 → `127.0.0.1:8000`（M2 API `/api/v1/*` 与 `/health`）
  - 公网 443 → `127.0.0.1:8080`（dabolo.org Dashboard）
  - 未匹配域名的 80 请求由 M2 默认 server 处理，保留 IP 直接访问
- [x] GitHub Actions 自动部署已成功跑通多次

## GitHub Secrets 配置

在仓库页面 `Settings -> Secrets and variables -> Actions -> New repository secret` 添加：

| Secret | 值 |
|---|---|
| `GHCR_TOKEN` | GitHub PAT（需 `repo` + `workflow` + `write:packages`） |
| `VPS_HOST` | `47.251.25.183` |
| `VPS_PORT` | `2222` |
| `VPS_USER` | `root` |
| `VPS_SSH_KEY` | SSH 私钥全文（含 `-----BEGIN...` 和 `-----END...`） |

## 当前架构要点（已落地）

1. **双源独立采集**：weather.gov 与 AWC 每轮各自批量请求全部监控机场。
2. **AWC 黑名单**：`UUWW` 不请求 AWC，但仍请求 weather.gov。
3. **择优合并**：按 `observed_at` 最新 -> 延迟最小 -> weather.gov 默认，写入 `metar:{icao}`。
4. **source-specific 存储**：
   - `metar:{icao}:source:weathergov`
   - `metar:{icao}:source:awc`
5. **METAR 时间统一**：从 `rawOb` 的 `ddHHMMZ` 解析 `observed_at`。
6. **轮询间隔**：`POLL_INTERVAL_SECONDS=1.0`（高频）。

## 验证命令

```bash
# VPS 上
ssh -p 2222 root@47.251.25.183
cd /opt/metar-system
docker compose ps
curl http://127.0.0.1:8000/health
curl 'http://127.0.0.1:8000/api/v1/metar?icao=KJFK'
curl 'http://127.0.0.1:8000/api/v1/metar/sources?icao=KSEA'

# 本地跨洋访问
curl 'http://47.251.25.183/api/v1/metar?icao=VHHH'

# 本地通过 SSH 隧道测试
ssh -p 2222 -L 8000:127.0.0.1:8000 root@47.251.25.183 -N
curl http://127.0.0.1:8000/health
```

## 后续变更记录

| 时间 | 变更 | Commit |
|---|---|---|
| 2026-07-10 | 将 `POLL_INTERVAL_SECONDS` 默认调整为 `1.0` | `2d6ef95` |
| 2026-07-10 | `observed_at` 统一从 `rawOb` 的 `ddHHMMZ` 解析 | `01fea9e` |
| 2026-07-10 | 双源独立采集 + source-specific Key + 择优合并 | `23b8e85` |
| 2026-07-13 | 新增 METAR 官方修正事件检测与 `/api/v1/metar/corrections` 接口 | `9608d59` |
| 2026-07-14 | 修复 Nginx 配置：80 端口保留 IP 访问 M2 API，443 端口托管 dabolo.org Dashboard | - |

## 可选但推荐

- 到 https://synopticdata.com 申请独立 Token，填入 VPS 的 `/opt/metar-system/.env` 中的 `WEATHERGOV_TOKEN`
- 配置域名 + HTTPS（Let's Encrypt）
- 配置日志聚合与容器监控
