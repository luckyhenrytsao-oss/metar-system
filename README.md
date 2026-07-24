# METAR 高频采集与分发系统

从美国气象局 [weather.gov / SynopticData](https://www.weather.gov)、[AviationWeather.gov (AWC)](https://aviationweather.gov) 以及 [IEM/Unidata LDM](https://mesonet.agron.iastate.edu/) GTS 数据流高频采集航空 METAR 报文，通过 **FastAPI + Redis** 向客户端极速分发。

## 核心特性

- **三源独立采集**：weather.gov、AWC 与 IEM LDM 每轮各自独立采集，互不影响。LDM 为近实时 GTS 推送，通常比轮询源更快。
- **择优合并**：三个数据源分别存入 `metar:{icao}:source:{weathergov|awc|iem}`，系统按 `observed_at` 取最新、同时间按延迟取最快、仍相同则默认 weather.gov > AWC > IEM，最终写入 `metar:{icao}`。
- **数据源透明度**：`GET /api/v1/metar/sources?icao=X` 可查看三个数据源原始记录及当前被选中的 winner。
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
├── docker-compose.yml     # FastAPI + Redis 7 + IEM LDM 编排
├── m2-ldm/                # IEM LDM 配置文件
│   ├── ldmd.conf          # LDM 上游请求配置
│   └── pqact.conf         # METAR 落盘配置
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
- `GET /api/v1/metar/sources?icao={ICAO_CODE}` —— 获取三个数据源的最新原始记录及当前 winner
- `GET /api/v1/metar/sources/history?icao={ICAO_CODE}&hours={N}` —— 按时间窗口查询历史三源对比（支持 `start`/`end`）
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

# SSE 高频心跳（适合 Clash/VPN/NAT 环境）
curl -N "http://127.0.0.1:8000/api/v1/metar/stream?icaos=KSEA,KJFK&heartbeat=5"
```

## Docker 本地运行

```bash
# 构建并启动（默认不启用 IEM LDM；LDM 需要 VPS IP 已在 IEM 白名单）
docker compose up -d --build

# 只启动 web + redis（不启动 m2-ldm）
docker compose up -d --build web redis

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
| `IEM_LDM_ENABLED` | `false` | 是否启用 IEM LDM 采集（仅在已加入 IEM 白名单的 VPS IP 上有效） |
| `IEM_LDM_FILE_PATH` | `/app/ldm_data/metar/metars.txt` | LDM METAR 文件在 M2 容器内路径 |
| `IEM_LDM_TRUNCATE_INTERVAL_HOURS` | `1` | METAR 文件截断清理间隔（小时） |

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

# IEM LDM 验证（如已在 .env 中启用）
docker compose logs -f m2-ldm
docker compose exec m2-ldm ls -l /home/ldm/var/data/metar/metars.txt
curl -i "http://47.251.25.183/api/v1/metar/sources?icao=VHHH"
# 应返回包含 weathergov / awc / iem 三个源的结构
```

## Redis Key 设计

| Key | 类型 | 含义 |
|-----|------|------|
| `metar:{icao}` | String (JSON) | 择优后的最终 METAR 记录，API 返回此记录 |
| `metar:{icao}:source:weathergov` | String (JSON) | weather.gov / SynopticData 最新原始记录 |
| `metar:{icao}:source:awc` | String (JSON) | AviationWeather.gov 最新原始记录 |
| `metar:{icao}:source:iem` | String (JSON) | IEM LDM 最新原始记录 |
| `history:metar:{icao}:source:weathergov` | Sorted Set (JSON) | weather.gov 历史记录，score 为 observed_at 时间戳 |
| `history:metar:{icao}:source:awc` | Sorted Set (JSON) | AWC 历史记录，score 为 observed_at 时间戳 |
| `history:metar:{icao}:source:iem` | Sorted Set (JSON) | IEM LDM 历史记录，score 为 observed_at 时间戳 |

所有 Key 强制 TTL = `METAR_TTL_SECONDS`（默认 7200 秒），防止采集器崩溃时客户端读到陈旧数据。
历史 Sorted Set 默认保留 7 天，并自动清理过期数据。

## 采集与择优逻辑

每轮轮询（默认 1 秒）执行：

1. **并发三源采集**
   - weather.gov：HTTP 批量请求全部监控机场
   - AWC：HTTP 批量请求全部监控机场；`AUTO` 报文保留；`UUWW / LTFM / LLBG` 的 `SPECI` 报文跳过
   - IEM LDM：本地文件增量读取，解析 GTS METAR/SPECI bulletin
2. **分别写入 source-specific Key**
   - 仅当该数据源的 METAR 文本 hash 变化时才写入，避免无意义刷新
3. **追加到历史记录**
   - 每次 source-specific Key 更新时，同时写入 `history:metar:{icao}:source:{source}` Sorted Set
   - 以 `observed_at` 时间戳作为 score，便于按时间窗口查询
   - 自动清理超过 7 天的旧记录
4. **逐机场择优**
   - 比较 `metar:{icao}:source:weathergov`、`metar:{icao}:source:awc` 与 `metar:{icao}:source:iem`
   - 优先选择 `observed_at` 更晚的记录（更新鲜）
   - 若 `observed_at` 相同，选择延迟 `updated_at - observed_at` 更小的记录
   - 若仍相同，默认选择 weather.gov > AWC > IEM
5. **写入最终 Key**
   - 若择优结果 hash 变化，覆盖 `metar:{icao}`
6. **LDM 文件清理**
   - 按 `IEM_LDM_TRUNCATE_INTERVAL_HOURS` 周期清空 LDM 文件，防止磁盘无限增长

## SSE 实时推送

`GET /api/v1/metar/stream?icaos={逗号分隔的ICAO代码}`

- 连接建立后先发送一次 `snapshot` 事件，包含当前所有请求机场的择优 METAR。
- 之后每当 M2 采集到新的 METAR 数据，会实时推送以下事件：
  - `source_update`：某个数据源（AWC / weather.gov / IEM LDM）有新数据
  - `winner_update`：M2 择优后的最终 METAR 发生变化
  - `correction`：官方修正事件（同一 observed_at 出现不同 hash）
- 每 20 秒发送一次 heartbeat 注释保持连接。
- 不指定 `icaos` 时推送全部监控机场的事件。

每个事件都包含 `temperature_c` 和 `dewpoint_c`，方便下游直接消费。

### 事件字段说明

#### `snapshot`

```json
{
  "event_type": "snapshot",
  "count": 2,
  "data": [
    {
      "icao": "KSEA",
      "temperature_c": 13.9,
      "dewpoint_c": 11.7,
      "raw_text": "METAR KSEA 101053Z 25004KT 10SM SCT060 14/12 A2997 RMK AO2 T01390117",
      "observed_at": "2026-07-10T10:53:00+00:00",
      "updated_at": "2026-07-10T10:55:20+00:00",
      "source": "aviationweather.gov",
      "source_key": "awc",
      "hash": "a1b2c3d4..."
    }
  ]
}
```

#### `winner_update`（M2 择优后的最终 METAR）

推荐 T0TX 只消费此事件，把 M2 当作一个独立数据源：

```json
{
  "event_type": "winner_update",
  "icao": "KSEA",
  "source_key": "awc",
  "source": "aviationweather.gov",
  "observed_at": "2026-07-10T10:53:00+00:00",
  "updated_at": "2026-07-10T10:55:20+00:00",
  "raw_text": "METAR KSEA 101053Z 25004KT 10SM SCT060 14/12 A2997 RMK AO2 T01390117",
  "hash": "a1b2c3d4...",
  "previous_hash": "e5f6g7h8...",
  "temperature_c": 13.9,
  "dewpoint_c": 11.7
}
```

#### `source_update`（数据源原始更新）

```json
{
  "event_type": "source_update",
  "icao": "KSEA",
  "source_key": "weathergov",
  "source": "weather.gov",
  "observed_at": "2026-07-10T10:53:00+00:00",
  "updated_at": "2026-07-10T10:55:22+00:00",
  "raw_text": "METAR KSEA 101053Z 25004KT 10SM SCT060 14/12 A2997 RMK AO2 T01390117",
  "hash": "x1y2z3...",
  "previous_hash": "p9q8r7...",
  "temperature_c": 13.9,
  "dewpoint_c": 11.7
}
```

#### `correction`（官方修正事件）

```json
{
  "event_type": "correction",
  "icao": "VILK",
  "source_key": "weathergov",
  "source": "weather.gov",
  "observed_at": "2026-07-12T21:30:00+00:00",
  "updated_at": "2026-07-12T21:35:22+00:00",
  "raw_text": "METAR VILK 122130Z ... 30/24 Q0997 NOSIG",
  "hash": "newhash...",
  "previous_hash": "oldhash...",
  "previous_raw_text": "METAR VILK 122130Z ... 29/24 Q0997 NOSIG",
  "temperature_c": 30.0,
  "dewpoint_c": 24.0
}
```

### 为什么用 SSE 而不是 WebSocket

METAR 场景是单向 server→client 推送，SSE 基于 HTTP，Nginx 原生支持，客户端重连简单，跨洋/跨 VPN 稳定性更好。

### 心跳与断线重连

- 默认每 **10 秒** 发送一次 heartbeat（`: heartbeat` 注释）。
- 如果中间代理（Clash Verge、VPN、NAT）对空闲连接更敏感，可通过 `?heartbeat=5` 缩短到 5 秒。
- 客户端应实现**断线自动重连**：SSE 连接被代理切断是正常现象，重连后会收到新的 `snapshot`，之后继续接收增量事件。

### T0TX 消费建议

T0TX 可以只监听 `winner_update`，把 M2 当作一个独立数据源：

```python
import requests
import json

url = "http://47.251.25.183/api/v1/metar/stream?icaos=KSEA,KJFK&heartbeat=5"
while True:
    try:
        with requests.get(url, stream=True, timeout=30) as resp:
            for line in resp.iter_lines():
                if not line:
                    continue
                text = line.decode("utf-8")
                if text.startswith("event: winner_update"):
                    data_line = next(resp.iter_lines()).decode("utf-8")
                    event = json.loads(data_line[len("data: "):])
                    print(event["icao"], event["temperature_c"], event["observed_at"])
                elif text.startswith("event: snapshot"):
                    # 重连后的初始状态，可按需初始化本地缓存
                    pass
    except Exception as exc:
        print("SSE disconnected, reconnecting:", exc)
        time.sleep(1)
```

## 历史接口

`GET /api/v1/metar/sources/history?icao={ICAO}&hours={N}`

返回指定机场过去 N 小时内每条 METAR 在三个数据源中的发现时间及 winner：

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

- **IEM LDM (GTS METAR/SPECI 推送)**：
  - 上游：`mesonet-ah.agron.iastate.edu`（需 IP 白名单）
  - 协议：Unidata LDM，出站 TCP 388
  - 数据：原始 GTS `SA` / `SP` bulletin，由 LDM 容器写入本地文件，M2 增量读取
  - 解析：跳过 WMO 报头，提取所有 `(METAR|SPECI) AAAA ddHHMMZ` 报文；一个 bulletin 可含多个机场
  - 清理：M2 按 `IEM_LDM_TRUNCATE_INTERVAL_HOURS` 周期清空文件，防止磁盘无限增长
  - 注意：IEM 上游采用 IP 白名单，因此 LDM 容器**只能在已加入白名单的 VPS 上有效工作**

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
