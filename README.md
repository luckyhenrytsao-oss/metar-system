# METAR 高频采集与分发系统

从美国气象局 [weather.gov / SynopticData](https://www.weather.gov) 和 [AviationWeather.gov (AWC)](https://aviationweather.gov) 高频采集航空 METAR 报文，通过 **FastAPI + Redis** 向客户端极速分发。

## 核心特性

- **双源独立采集**：weather.gov 与 AWC 每轮各自批量请求全部监控机场，互相独立、互不影响。
- **择优合并**：两个数据源分别存入 `metar:{icao}:source:weathergov` 与 `metar:{icao}:source:awc`，系统按 `observed_at` 取最新、同时间按延迟取最快、仍相同则默认 weather.gov，最终写入 `metar:{icao}`。
- **数据源透明度**：新增 `GET /api/v1/metar/sources?icao=X` 可查看两个数据源原始记录及当前被选中的 winner。
- **METAR-only 过滤**：
  - AWC 接收 `METAR`、`SPECI`、`AUTO` 报文（AUTO 视为有效 METAR，与 T0TX 口径一致）。
  - 对 `UUWW / LTFM / LLBG` 三个机场，AWC 中的 `SPECI` 报文会被跳过（与 weather.gov 结算口径保持一致）。
  - SynopticData 仅保留 `metar_origin_set_1 == 1` 的真正 METAR/SPECI，避免混入 ASOS/AWOS 自动观测。
- **精确温度解析**：优先解析 RMK 中的 `Txxxx/Txxxxxxxx` 精确温度组（精度 0.1°C），否则回退到 METAR 主体整数温度。
- **真实 METAR 时间**：统一从 `rawOb` 文本中的 `ddHHMMZ` 解析 `observed_at`，不再使用数据源的 `reportTime` / `date_time`；解析失败则跳过该条。
- **去重写入**：计算 METAR 文本 SHA1 hash，仅在有变化时覆盖 Redis，每次刷新 2 小时 TTL。
- **HTTP 304 优化**：客户端携带 `If-None-Match` 匹配 hash 时返回 304 空 Body，最大限度压缩跨洋带宽。
- **SSE 实时推送**：新增 `GET /api/v1/metar/stream`，一条长连接即可实时接收 METAR 更新、数据源更新和官方修正事件，显著降低 T0TX/M1 等下游消费者的端口消耗。
- **永不崩溃的采集循环**：外层 `try-except` 捕获所有网络异常，500/429/Timeout 均不会终止后台任务。

## 项目结构

```text
metar-system/
├── app/
│   ├── __init__.py
│   ├── config.py          # Pydantic Settings 环境变量管理
│   ├── database.py        # Redis 异步连接池与数据读写
│   ├── collector.py       # 异步高频双源采集器、去重与择优逻辑
│   └── main.py            # FastAPI 入口与后台任务
├── tests/
│   ├── __init__.py
│   ├── conftest.py        # Pytest fixtures（fakeredis + TestClient）
│   ├── test_collector.py  # 采集器单元测试
│   └── test_api.py        # FastAPI 接口测试
├── examples/m1_integration/  # 外部项目调用参考
│   ├── README.md
│   └── metar_client.py
├── .github/workflows/
│   └── deploy.yml         # CI/CD：测试 -> 构建镜像 -> 推送 GHCR -> SSH 部署
├── scripts/
│   ├── vps-first-deploy.sh   # VPS 首次手动部署脚本
│   └── analyze_source_latency.py  # 双源延迟分析脚本（可选工具）
├── Dockerfile             # 多阶段构建
├── docker-compose.yml     # FastAPI + Redis 7 编排
├── requirements.txt       # Python 依赖
├── .env.example           # 环境变量模板
├── DEPLOY_NOTES.md        # 部署备忘与首次部署清单
└── README.md              # 本文件
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

- `GET /api/v1/metar?icao={ICAO_CODE}` —— 获取择优后的最终 METAR
- `GET /api/v1/metar/sources?icao={ICAO_CODE}` —— 获取两个数据源的最新原始记录及当前 winner
- `GET /api/v1/metar/sources/history?icao={ICAO_CODE}&hours={N}` —— 按时间窗口查询历史双源对比（支持 `start`/`end`）
- `GET /api/v1/metar/stream?icaos={ICAO1,ICAO2}` —— SSE 实时流：推送 snapshot + 新 METAR 事件
- `POST /api/v1/metar/batch` —— 批量获取温度解析结果
- `GET /health` —— 健康检查

示例：

```bash
curl -i "http://127.0.0.1:8000/api/v1/metar?icao=KJFK"
curl -i "http://127.0.0.1:8000/api/v1/metar?icao=KJFK" -H 'If-None-Match: "hashvalue"'
curl -i "http://127.0.0.1:8000/api/v1/metar/sources?icao=KSEA"
curl -i "http://127.0.0.1:8000/api/v1/metar/sources/history?icao=KSEA&hours=24"
curl -i "http://127.0.0.1:8000/api/v1/metar/sources/history?icao=KSEA&start=2026-07-10T00:00:00Z&end=2026-07-11T00:00:00Z"

# SSE 实时流（推荐 T0TX/M1 使用）
curl -N "http://127.0.0.1:8000/api/v1/metar/stream?icaos=KSEA,KJFK"
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
| `POLL_INTERVAL_SECONDS` | `1.0` | 采集轮询间隔（1.0~2.0） |
| `METAR_TTL_SECONDS` | `7200` | Redis 数据 TTL（秒） |
| `USER_AGENT` | `MyMetarApp/1.0 (htsao2000@gmail.com)` | 请求头，含可联系邮箱 |
| `WEATHERGOV_TOKEN` | 空 | 可选 SynopticData 独立 Token |
| `HTTP_TIMEOUT` | `15.0` | HTTP 请求超时 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 生产部署

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

下载并执行仓库中的 [`scripts/vps-first-deploy.sh`](scripts/vps-first-deploy.sh)：

```bash
mkdir -p /opt/metar-system
cd /opt/metar-system
curl -fsSL https://raw.githubusercontent.com/luckyhenrytsao-oss/metar-system/main/scripts/vps-first-deploy.sh -o vps-first-deploy.sh
chmod +x vps-first-deploy.sh
vim vps-first-deploy.sh   # 填入 GHCR_TOKEN 等变量
./vps-first-deploy.sh
```

脚本会完成：安装 Docker / Nginx、创建 `/opt/metar-system`、生成 `.env`、登录 GHCR、拉取镜像、启动容器、配置 Nginx、健康检查。

详细分步清单与验证命令见 [`DEPLOY_NOTES.md`](DEPLOY_NOTES.md)。

### 3. 自动部署

首次手动部署完成后，后续 Push 到 `main` 分支将触发 GitHub Actions：

1. 运行 pytest 测试
2. 构建并推送镜像到 `ghcr.io/luckyhenrytsao-oss/metar-system:latest`
3. SSH 登录 VPS 执行 `docker compose pull && docker compose up -d`
4. 重新加载 Nginx 并健康检查

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

## Redis Key 设计

| Key | 类型 | 含义 |
|-----|------|------|
| `metar:{icao}` | String (JSON) | 择优后的最终 METAR 记录，API 返回此记录 |
| `metar:{icao}:source:weathergov` | String (JSON) | weather.gov / SynopticData 最新原始记录 |
| `metar:{icao}:source:awc` | String (JSON) | AviationWeather.gov 最新原始记录 |
| `history:metar:{icao}:source:weathergov` | Sorted Set (JSON) | weather.gov 历史记录，score 为 observed_at 时间戳 |
| `history:metar:{icao}:source:awc` | Sorted Set (JSON) | AWC 历史记录，score 为 observed_at 时间戳 |

所有 Key 强制 TTL = `METAR_TTL_SECONDS`（默认 7200 秒），防止采集器崩溃时客户端读到陈旧数据。
历史 Sorted Set 默认保留 7 天，并自动清理过期数据。

## 采集与择优逻辑

每轮轮询（默认 1 秒）执行：

1. **并发双源批量请求**
   - weather.gov：请求全部监控机场
   - AWC：请求全部监控机场；`AUTO` 报文保留；`UUWW / LTFM / LLBG` 的 `SPECI` 报文跳过
2. **分别写入 source-specific Key**
   - 仅当该数据源的 METAR 文本 hash 变化时才写入，避免无意义刷新
3. **追加到历史记录**
   - 每次 source-specific Key 更新时，同时写入 `history:metar:{icao}:source:{source}` Sorted Set
   - 以 `observed_at` 时间戳作为 score，便于按时间窗口查询
   - 自动清理超过 7 天的旧记录
4. **逐机场择优**
   - 比较 `metar:{icao}:source:weathergov` 与 `metar:{icao}:source:awc`
   - 优先选择 `observed_at` 更晚的记录（更新鲜）
   - 若 `observed_at` 相同，选择延迟 `updated_at - observed_at` 更小的记录
   - 若仍相同，默认选择 weather.gov
5. **写入最终 Key**
   - 若择优结果 hash 变化，覆盖 `metar:{icao}`

## SSE 实时推送

`GET /api/v1/metar/stream?icaos={逗号分隔的ICAO代码}`

- 连接建立后先发送一次 `snapshot` 事件，包含当前所有请求机场的择优 METAR。
- 之后每当 M2 采集到新的 METAR 数据，会实时推送以下事件：
  - `source_update`：某个数据源（AWC / weather.gov）有新数据
  - `winner_update`：M2 择优后的最终 METAR 发生变化
  - `correction`：官方修正事件（同一 observed_at 出现不同 hash）
- 每 20 秒发送一次 heartbeat 注释保持连接。
- 不指定 `icaos` 时推送全部监控机场的事件。

每个事件都包含 `temperature_c` 和 `dewpoint_c`，方便下游直接消费。

```text
event: snapshot
data: {"event_type": "snapshot", "count": 2, "data": [...]}

event: winner_update
data: {"event_type": "winner_update", "icao": "KSEA", "temperature_c": 23.3, ...}
```

**为什么用 SSE 而不是 WebSocket**：METAR 场景是单向 server→client 推送，SSE 基于 HTTP，Nginx 原生支持，客户端重连简单，跨洋/跨 VPN 稳定性更好。

## 历史接口

`GET /api/v1/metar/sources/history?icao={ICAO}&hours={N}`

返回指定机场过去 N 小时内每条 METAR 在两个数据源中的发现时间及 winner：

```json
{
  "icao": "KORD",
  "count": 3,
  "records": [
    {
      "observed_at": "2026-07-11T01:51:00+00:00",
      "winner": { "source_key": "awc", "updated_at": "...", "raw_text": "...", "temperature_c": 23.3 },
      "awc": { ... },
      "weathergov": { ... }
    }
  ]
}
```

也支持 `start`/`end` 参数：

```text
GET /api/v1/metar/sources/history?icao=KORD&start=2026-07-10T00:00:00Z&end=2026-07-11T00:00:00Z
```

## 数据源说明

- **AWC (AviationWeather.gov)**：
  - URL: `https://aviationweather.gov/api/data/metar?ids={icaos}&format=json&hours=1`
  - 直接返回标准 METAR/SPECI/AUTO，无需 Token。
  - `AUTO` 报文视为有效 METAR，正常采集。
  - 对 `UUWW / LTFM / LLBG` 三个机场，跳过 AWC 来源的 `SPECI` 报文（与 weather.gov 结算口径对齐）。

- **weather.gov / SynopticData**：
  - API URL: `https://api.synopticdata.com/v2/stations/timeseries`
  - Token：优先使用 `WEATHERGOV_TOKEN`；未配置时从 `https://www.weather.gov/source/wrh/apiKey.js` 抓取内嵌 Token。
  - 必须过滤 `metar_origin_set_1 == 1` 才得到真正 METAR/SPECI。

## 外部项目集成

M1 或其他项目调用示例见 [`examples/m1_integration/README.md`](examples/m1_integration/README.md)。

## 安全与优化建议

1. **HTTPS**：生产环境建议申请域名并配置 Let's Encrypt SSL。
2. **防火墙**：仅开放 80/443/SSH 端口。
3. **Rate Limit**：49 个机场每 1 秒轮询，建议申请 SynopticData 独立 Token 并监控配额。
4. **监控**：可将 `docker compose logs` 转发到 Loki / CloudWatch。
5. **备份**：Redis AOF 持久化到 Docker volume，可定期 snapshot。

## 参考

- M1 项目：`D:\Henry_Project\M1`
- [SynopticData API Docs](https://docs.synopticdata.com/services/api-performance-and-limits)
- [AviationWeather API Docs](https://aviationweather.gov/data/api/)
