# METAR 高频采集与分发系统

从美国气象局 [weather.gov / SynopticData](https://www.weather.gov) 和 [AviationWeather.gov (AWC)](https://aviationweather.gov) 高频采集航空 METAR 报文，通过 **FastAPI + Redis** 向客户端极速分发。

## 核心特性

- **双源采集**：同时轮询 AWC 与 weather.gov / SynopticData，取最先成功响应的真实 METAR。
- **METAR-only 过滤**：对 SynopticData 数据过滤 `metar_origin_set_1 == 1`，避免美国站混入 ASOS/AWOS 5 分钟自动观测。
- **去重写入**：计算 METAR 文本 SHA1 hash，仅在有变化（如 SPECI）时覆盖 Redis，每次刷新 2 小时 TTL。
- **HTTP 304 优化**：客户端携带 `If-None-Match` 匹配 hash 时返回 304 空 Body，最大限度压缩跨洋带宽。
- **永不崩溃的采集循环**：外层 `try-except` 捕获所有网络异常，500/429/Timeout 均不会终止后台任务。

## 项目结构

```text
metar-system/
├── app/
│   ├── __init__.py
│   ├── config.py          # Pydantic Settings 环境变量管理
│   ├── database.py        # Redis 异步连接池与数据读写
│   ├── collector.py       # 异步高频采集器与去重逻辑
│   └── main.py            # FastAPI 入口与后台任务
├── tests/
│   ├── __init__.py
│   ├── conftest.py        # Pytest fixtures（fakeredis + TestClient）
│   ├── test_collector.py  # 采集器单元测试
│   └── test_api.py        # FastAPI 接口测试
├── .github/workflows/
│   └── deploy.yml         # CI/CD：测试 -> 构建镜像 -> 推送 GHCR -> SSH 部署
├── Dockerfile             # 多阶段构建
├── docker-compose.yml     # FastAPI + Redis 7 编排
├── requirements.txt       # Python 依赖
└── README.md
```

## 本地开发

### 1. 创建虚拟环境并安装依赖

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 运行测试

测试使用 `fakeredis[asyncio]` 和 `respx`，无需真实 Redis 和外部网络，30 秒内完成。

```bash
pytest -q
```

### 3. 本地启动 FastAPI

需要本地 Redis（可用 Docker 启动）：

```bash
docker run -d --name redis -p 6379:6379 redis:7-alpine

# 启动应用
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

接口：

- `GET /api/v1/metar?icao={ICAO_CODE}`
- `GET /health`

示例：

```bash
curl -i "http://127.0.0.1:8000/api/v1/metar?icao=KJFK"
curl -i "http://127.0.0.1:8000/api/v1/metar?icao=KJFK" -H 'If-None-Match: "hashvalue"'
```

## Docker 本地运行

```bash
# 构建并启动
docker compose up -d --build

# 查看日志
docker compose logs -f web

# 停止
docker compose down
```

访问：http://localhost:8000/api/v1/metar?icao=KJFK

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接地址 |
| `MONITOR_AIRPORTS` | M1 49 个机场 | 逗号分隔的 ICAO 代码 |
| `POLL_INTERVAL_SECONDS` | `1.5` | 采集轮询间隔（1.0~2.0） |
| `METAR_TTL_SECONDS` | `7200` | Redis 数据 TTL（秒） |
| `USER_AGENT` | `MyMetarApp/1.0 (htsao2000@gmail.com)` | 请求头，含可联系邮箱 |
| `WEATHERGOV_TOKEN` | 空 | 可选 SynopticData 独立 Token |
| `HTTP_TIMEOUT` | `15.0` | HTTP 请求超时 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 生产部署与 GitHub Secrets

> **注意**：以下步骤我已经在 `47.251.25.183:2222` 上手动跑通并验证（HTTP 200 / 304 均正常）。

### 1. 配置 GitHub Secrets

在仓库 `Settings -> Secrets and variables -> Actions` 中配置：

| Secret | 说明 |
|--------|------|
| `GHCR_TOKEN` | GitHub PAT，需勾选 `repo` + `workflow` + `write:packages` |
| `VPS_HOST` | VPS 公网 IP，例如 `47.251.25.183` |
| `VPS_PORT` | SSH 端口，例如 `2222` |
| `VPS_USER` | SSH 用户，例如 `root` |
| `VPS_SSH_KEY` | SSH 私钥全文（含 `BEGIN/END`） |

### 2. VPS 首次手动部署

最快的方式是下载并执行仓库中的 [`scripts/vps-first-deploy.sh`](scripts/vps-first-deploy.sh)：

```bash
# 在 VPS 上
mkdir -p /opt/metar-system
cd /opt/metar-system

# 下载脚本（或本地上传）
curl -fsSL https://raw.githubusercontent.com/luckyhenrytsao-oss/metar-system/main/scripts/vps-first-deploy.sh -o vps-first-deploy.sh
chmod +x vps-first-deploy.sh

# 编辑脚本开头的 GHCR_TOKEN 等变量
vim vps-first-deploy.sh

# 执行
./vps-first-deploy.sh
```

脚本会完成：安装 Docker / Nginx、创建 `/opt/metar-system`、生成 `.env`、登录 GHCR、拉取镜像、启动容器、配置 Nginx、健康检查。

### 3. 手动分步清单（若不想用脚本）

```bash
# 1. 安装 Docker + Nginx
dnf install -y docker nginx curl
systemctl enable docker --now

# 2. 创建项目目录
mkdir -p /opt/metar-system
cd /opt/metar-system

# 3. 下载 docker-compose.yml 并生成 .env
curl -fsSL https://raw.githubusercontent.com/luckyhenrytsao-oss/metar-system/main/docker-compose.yml -o docker-compose.yml
cp .env.example .env   # 按需修改

# 4. 登录 GHCR 并启动
echo "ghp_xxxxxxxx" | docker login ghcr.io -u luckyhenrytsao-oss --password-stdin
docker compose pull
docker compose up -d

# 5. 配置 Nginx
curl -fsSL https://raw.githubusercontent.com/luckyhenrytsao-oss/metar-system/main/nginx/metar.conf \
  -o /etc/nginx/default.d/metar.conf
nginx -t
systemctl enable nginx --now

# 6. 验证
curl http://127.0.0.1:8000/health
curl http://47.251.25.183/api/v1/metar?icao=VHHH
```

### 4. 部署后验证

```bash
# 查看容器
docker compose ps

# 查看实时日志
docker compose logs -f web

# 公网接口测试（从本地跨洋访问）
curl -i "http://47.251.25.183/api/v1/metar?icao=VHHH"

# 304 测试
HASH=$(curl -fsS "http://47.251.25.183/api/v1/metar?icao=VHHH" | python3 -c 'import sys,json; print(json.load(sys.stdin)["hash"])')
curl -fsS -o /dev/null -w '%{http_code}' -H "If-None-Match: $HASH" "http://47.251.25.183/api/v1/metar?icao=VHHH"
# 期望输出：304
```

### 5. 自动部署

首次手动部署完成后，后续 Push 到 `main` 分支将触发 GitHub Actions：

1. 运行 pytest 测试
2. 构建并推送镜像到 `ghcr.io/luckyhenrytsao-oss/metar-system:latest`
3. SSH 登录 VPS 执行 `docker compose pull && docker compose up -d`
4. 重新加载 Nginx 并健康检查

也可以手动触发：`Actions -> Build, Test and Deploy METAR System -> Run workflow`。

## 当前代码上 VPS 前的检查清单

- [x] `USER_AGENT` 已替换为 `htsao2000@gmail.com`
- [x] GHCR 路径已替换为 `ghcr.io/luckyhenrytsao-oss/metar-system`
- [x] `.env.example` 已包含 49 个默认机场
- [x] `docker-compose.yml` 中 `web` 服务仅绑定 `127.0.0.1:8000`，不直接暴露公网
- [x] Nginx 配置已纳入版本控制（`nginx/metar.conf`）
- [x] 首次部署脚本已纳入版本控制（`scripts/vps-first-deploy.sh`）
- [x] Redis 开启 AOF 持久化，数据 2 小时 TTL
- [x] 采集器异常捕获完整，后台循环不会崩溃

## 安全与优化建议

1. **HTTPS**：生产环境建议申请域名并配置 Let's Encrypt SSL，将 `nginx/metar.conf` 中的 80 端口重定向到 443。
2. **防火墙**：仅开放 80/443/SSH 端口，可使用 `firewalld` 或云厂商安全组。
3. **Rate Limit**：49 个机场每 1.5 秒轮询 SynopticData，建议申请独立 Token 并监控 API 配额。
4. **监控**：可配置 `docker compose logs` 转发到 Loki / CloudWatch，或安装 `cadvisor` 监控容器。
5. **备份**：Redis 数据通过 AOF 持久化到 Docker volume，可定期 `docker cp` 或 snapshot 备份。

## 数据源说明

- **AWC (AviationWeather.gov)**：
  - URL: `https://aviationweather.gov/api/data/metar?ids={icao}&format=json&hours=1`
  - 直接返回标准 METAR，无需 Token。

- **weather.gov / SynopticData**：
  - API URL: `https://api.synopticdata.com/v2/stations/timeseries`
  - Token：优先使用 `WEATHERGOV_TOKEN`；未配置时从 `https://www.weather.gov/source/wrh/apiKey.js` 抓取内嵌 Token。
  - **注意**：美国站（如 KJFK、KORD）返回 ASOS/AWOS 5 分钟观测 + METAR 混合，必须过滤 `metar_origin_set_1 == 1` 才得到真正 METAR。

## 参考

- M1 项目：`D:\Henry_Project\M1`
- [SynopticData API Docs](https://docs.synopticdata.com/services/api-performance-and-limits)
- [AviationWeather API Docs](https://aviationweather.gov/data/api/)
