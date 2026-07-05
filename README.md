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

CI/CD 工作流 `.github/workflows/deploy.yml` 会在 `push` 到 `main` 分支时：

1. 运行 pytest 测试
2. 构建 Docker 镜像并推送到 GHCR
3. 通过 SSH 登录 VPS 执行滚动更新

需要在 GitHub 仓库 Settings -> Secrets and variables -> Actions 中配置：

| Secret | 说明 |
|--------|------|
| `GHCR_TOKEN` | GitHub Personal Access Token，需 `write:packages` 权限 |
| `VPS_HOST` | VPS 公网 IP，参考 M1 为 `47.251.25.183` |
| `VPS_PORT` | SSH 端口，参考 M1 为 `2222` |
| `VPS_USER` | SSH 用户，参考 M1 为 `root` |
| `VPS_SSH_KEY` | SSH 私钥全文（含 BEGIN/END） |

### VPS 首次准备

确保 VPS 上已创建项目目录并放置 `docker-compose.yml`：

```bash
mkdir -p /opt/metar-system
cd /opt/metar-system
# 将 docker-compose.yml 放至此目录
docker compose pull
docker compose up -d
```

后续 Push 到 `main` 后将自动部署。

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
