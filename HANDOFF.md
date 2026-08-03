# M2 METAR 系统交接文档

> 本文档面向后续接手的 AI / 开发者，用于快速了解项目背景、当前状态、已知的待办事项以及继续工作的注意事项。
> 生成时间：2026-07-30（基于当前会话的工作成果）

---

## 1. 项目目标

M2（metar-system）是一套部署在美国 VPS 上的高频 METAR 采集与分发系统，核心目标：

- 从美国本土网络位置，高速、稳定地采集全球机场的 METAR 数据。
- 同时聚合 **3 个数据源**：
  1. **weather.gov**（SynopticData API）
  2. **AviationWeather.gov（AWC）**
  3. **IEM LDM**（Iowa Environmental Mesonet 的 LDM GTS 数据流）
- 通过 FastAPI 提供 REST 接口，并通过 SSE 长连接实时推送 `winner_update`、`source_update`、`correction` 事件。
- 为下游（如 T0TX、M1 Dashboard）提供低延迟、可归因的 METAR 数据。

---

## 2. 仓库与部署信息

| 项目 | 值 |
|---|---|
| GitHub 仓库 | `https://github.com/luckyhenrytsao-oss/metar-system` |
| GHCR 镜像 | `ghcr.io/luckyhenrytsao-oss/metar-system:latest` |
| VPS 公网 IP | `47.251.25.183` |
| VPS SSH 端口 | `2222` |
| VPS 登录用户 | `root` |
| VPS 项目目录 | `/opt/metar-system` |
| 联系邮箱 | `htsao2000@gmail.com` |
| GitHub 账户 | `https://github.com/luckyhenrytsao-oss` |

### GitHub Actions Secrets

在仓库 `Settings -> Secrets and variables -> Actions` 中配置：

- `GHCR_TOKEN`：需 `repo` + `workflow` + `write:packages`
- `VPS_HOST`：`47.251.25.183`
- `VPS_PORT`：`2222`
- `VPS_USER`：`root`
- `VPS_SSH_KEY`：SSH 私钥全文

### Nginx（VPS 上）

- 公网 **80** → `127.0.0.1:8000`（M2 API `/api/v1/*`、`/health`、`/api/v1/metar/stream`）
- 公网 **443** → `127.0.0.1:8080`（dabolo.org Dashboard）
- 保留 IP 直接访问 80 端口到 M2

Nginx 配置文件位于 `nginx/`，CI/CD 部署时会自动同步到 `/etc/nginx/`。

---

## 3. 目录结构与核心文件

```text
metar-system/
├── app/
│   ├── __init__.py
│   ├── config.py          # Pydantic Settings，管理所有环境变量
│   ├── collector.py       # 核心采集器：HTTP 轮询 + IEM LDM 文件读取 + 去重 + 择优
│   ├── database.py        # Redis 异步客户端、source history、correction events
│   ├── events.py          # 内存事件总线，供 SSE 消费
│   └── main.py            # FastAPI 入口、路由、SSE stream、生命周期管理
├── tests/                 # Pytest 测试，使用 fakeredis，无需外部服务
├── m2-ldm/
│   ├── ldmd.conf          # LDM 上游请求配置
│   ├── pqact.conf         # 将收到的 bulletin 追加到文件
│   └── data/metar/metars.txt   # LDM 写入的原始 METAR 文件
├── nginx/                 # Nginx 配置，部署时同步到 VPS
├── .github/workflows/
│   └── deploy.yml         # CI/CD：test -> build & push GHCR -> deploy to VPS
├── docker-compose.yml     # 编排 web / redis / m2-ldm
├── Dockerfile             # 多阶段构建
├── requirements.txt
├── .env.example           # 环境变量模板
├── DEPLOY_NOTES.md        # 部署备忘
└── HANDOFF.md             # 本文件
```

---

## 4. 核心设计要点

### 数据源与选择策略

- **weather.gov**：通过 SynopticData API 批量获取；自动抓取或 env `WEATHERGOV_TOKEN`。
- **AWC**：`https://aviationweather.gov/api/data/metar?ids=...&format=json&hours=1`。
- **IEM LDM**：VPS 上运行 `unidata/ldm-docker` 容器，订阅 `IDS|DDPLUS ^S[AP]`，把 bulletin 追加到 `m2-ldm/data/metar/metars.txt`。

**择优规则**（`_select_winner`）：

1. `observed_at` 更新者优先
2. 相同时，`updated_at - observed_at` 延迟更小者优先
3. 还相同则优先级：`weather.gov` > `AWC` > `IEM`

### 去重与 Source History

- 每个数据源有独立 Redis key：`metar:{icao}:source:{weathergov|awc|iem}`。
- 每次数据变化会追加到 `history:metar:{icao}:source:{source}`（按 `observed_at` 排序）。
- 同一 `observed_at` 出现不同 hash 会触发 `correction` 事件，保存 360 天。

### IEM 文件读取

- `_IemLdmReader` 维护 `inode` + `offset`，增量读取新增内容。
- `_parse_iem_bulletins` 按 `=` 分隔 bulletin，跳过 WMO 报头，支持一个 bulletin 含多个机场。
- `_maybe_truncate_iem_file()` 每小时清空一次文件，防止磁盘无限增长。

### SSE 事件

- 端点：`GET /api/v1/metar/stream?icaos=WMKK,WSSS`
- 事件类型：`snapshot`、`winner_update`、`source_update`、`correction`
- 事件字段示例见 `app/main.py`。

---

## 5. 当前运行状态（截至最近验证）

- ✅ VPS 上 M2 容器 `healthy`。
- ✅ Redis、LDM 容器稳定运行。
- ✅ `metars.txt` 每小时自动 truncate，大小维持在几十 KB ~ 1 MB，不再无限膨胀。
- ✅ CI/CD 最近一次运行成功（Build / Push / Deploy 全绿）。
- ✅ 公网 API 可访问：`http://47.251.25.183/api/v1/metar?icao=WMKK`。

### 已落地的重要修复

1. **`docker-compose.yml` 中 LDM 挂载去掉 `:ro`**，并设置 `user: "999:1000"`，使 M2 容器能 truncate LDM 文件。
2. **`app/collector.py` 移除了 IEM 解析缓冲区的 1MB 上限**，避免截断导致温度解析错误。
3. **`.github/workflows/deploy.yml` 增加 `actions/checkout` 和 `appleboy/scp-action`**，部署时自动同步 `docker-compose.yml`、`nginx/`、`m2-ldm/*.conf`，并 `--force-recreate web`。

---

## 6. 已知现象与问题（接手前必读）

### 6.1 IEM 延迟存在显著的区域差异

最近 6 小时的实测数据（`first_iem_updated_at - observed_at`）：

| 区域 | 机场 | 延迟 |
|---|---|---|
| US-KWBC | KSEA / KORD / KATL | ~3 分钟 |
| EU-EDZW | EFHK / EHAM / LFPB | ~2 ~ 4 分钟 |
| EU-EDZW | LEMD / LIMC | ~5 ~ 6 分钟 |
| Asia | RPLL | ~1 分钟 |
| Asia | VHHH / RKSI | ~3 ~ 4 分钟 |
| Asia | **WMKK / WSSS / RJTT** | **~8 ~ 9 分钟** |

**结论**：延迟不是 M2 轮询或解析造成的（M2 轮询仅 1 秒），大概率在 **IEM/GTS 上游路径**，且与 originating WMO 中心强相关。

### 6.2 IEM 同一 METAR 会出现两种格式

IEM 会先发来带 `METAR` 前缀的版本，随后又来一个不带前缀的版本，例如：

```text
METAR WMKK 300300Z ...
WMKK 300300Z ...
```

两者 hash 不同，M2 会误判为一次 `correction` 事件。这是噪音，不是真正的官方修正。

### 6.3 `metars.txt` 本身没有精确到达时间戳

文件里只有：

- LDM 序列号（如 `000`、`003`）
- WMO 头（如 `SAMS32 WMKK 300300`）
- METAR 内容

无法直接判断某条 bulletin 是几点几分进入 LDM 的，目前只能用 M2 `updated_at` 反推。

---

## 7. 待办事项（TODO）

以下已加入 TODO List，**目前均未实施**，是后续优先方向：

1. **研究 LDM → pipe → M2 stdin 方案**
   - 替代文件轮询，降低 0~1 秒轮询延迟和磁盘 I/O。
   - 需要解决容器编排问题（LDM 与 M2 进程如何共享 pipe）。

2. **IEM 数据增加内部异步队列**
   - 把“读取原始 bytes → 解析 → 入库 → 发 SSE”解耦。
   - 避免突发大量 bulletin 时阻塞采集循环。

3. **评估先推 SSE 再异步落库**
   - 对交易场景，先发送 `temperature_c` 等关键字段，再异步写 Redis。
   - 需权衡数据一致性。

4. **建立 LDM 精确到达时间记录，绘制全球 METAR 传播延迟地图**
   - 写一个 LDM receiver（pipe 或嵌入 pqact），记录每条 bulletin 的：
     - `receive_time`
     - `wmo_header`
     - `station`
     - `obs_time`
     - `temperature_c`
   - 存储到 Redis/时序数据库，用于分析 `arrival_time - obs_time` 的全球分布。

5. **对齐 AWC / weather.gov 时间解析，优先使用上游 API 绝对时间**
   - 响应 T0TX 修复的 misdate bug：AWC 优先使用 `obsTime`，weather.gov 优先使用 `date_time`，缺失时 fallback 到 rawOb 解析。
   - 设计方案见 `docs/proposed_time_parsing_alignment.md`。
   - **暂不实施**：当前 M2 运行正常，且抽样对比显示 `obsTime`/`date_time` 与 rawOb 解析结果一致。
   - **决策条件**：先让 T0TX 在新代码上运行 1~2 周，观察是否仍出现 misdate 事件，再决定是否启动修改。

6. **接入 Skyviewor fast-METAR 作为中国机场第四数据源**
   - 已实现：`app/skyviewor.py` WebSocket 采集器 + `app/config.py` 配置 + `app/main.py` 生命周期集成 + `app/database.py` 审计存储。
   - 默认**禁用**（`SKYVIEWOR_ENABLED=false`），未配置 API Key 时不启动。
   - 采信规则（v1.0，可配置）：
     - METAR：ZBAA/ZGGG/ZSPD 的 `:00` 和 `:30` 采信；其他中国机场仅 `:00` 采信。
     - SPECI：仅 ZBAA 采信。
   - 不可信数据写入独立 Redis key `skyviewor:audit:{icao}`，不进入标准 history，不触发 SSE。
   - **暂不部署到 PRD**：等待用户提供 `SKYVIEWOR_API_KEY` 后，先在本地/测试环境验证，再决定是否部署。
   - 详细设计见 `docs/skyviewor_integration_for_m2.md`（M1 文档）和本次提交的代码。

---

## 8. 常用命令速查

### 本地开发

```bash
# 安装依赖
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 运行测试
pytest -q --tb=short

# 本地启动
# 1. 复制 .env.example 为 .env 并填写
# 2. docker compose up -d --build
```

### VPS 运维

```bash
# SSH 登录
ssh -p 2222 -i ~/.ssh/id_ed25519 root@47.251.25.183

# 查看容器状态
cd /opt/metar-system
docker compose ps
docker compose logs -f web --tail=50

# 健康检查
curl -fsS http://127.0.0.1:8000/health

# 查单机场数据
curl 'http://127.0.0.1:8000/api/v1/metar?icao=WMKK'
curl 'http://127.0.0.1:8000/api/v1/metar/sources?icao=WMKK'
curl 'http://127.0.0.1:8000/api/v1/metar/sources/history?icao=WMKK&hours=2'

# 进入 Redis
docker compose exec -T redis redis-cli

# 查看 LDM 文件大小
ls -lh m2-ldm/data/metar/metars.txt

# 查看 IEM 某机场历史（精确到每次 hash 变化）
docker compose exec -T redis redis-cli ZRANGEBYSCORE "history:metar:WMKK:source:iem" 1785380400 1785380400

# 手动 force-recreate web（compose 配置改动后）
docker compose up -d --force-recreate web
```

---

## 9. 给接手 AI 的工作建议

1. **先跑通本地测试**：任何改动前先 `pytest -q`，确保基线通过。
2. **改 IEM 解析要格外小心**：WMO 头、多机场 bulletin、`METAR` 前缀缺失、多行续行、`=` 分隔符都是坑。改完后用真实 `metars.txt` 片段测试。
3. **不要裸推 main**：任何提交都会触发 CI/CD 自动部署到 VPS。小改动可以先在分支验证，或在 `deploy.yml` 里加条件限制。
4. **区分“M2 延迟”和“上游延迟”**：遇到某机场变慢，先用 `/api/v1/metar/sources/history` 或 Redis history 比较三源，再下结论。
5. **记录 LDM 精确到达时间是高优先级**：这是目前最大的数据盲区，做完后很多 latency 问题会豁然开朗。
6. **安全与敏感信息**：不要在本仓库中提交 `.env`、SSH 私钥、GitHub Token。这些只应存在于 VPS 和 GitHub Secrets 中。

---

## 10. 参考文档

- `DEPLOY_NOTES.md`：部署备忘、Secrets 配置、验证命令
- `docs/m2_corrections_dashboard_integration.md`：correction 接口文档
- `README.md`：本地运行说明
- `.env.example`：环境变量模板

---

如有疑问，先检查 VPS 上 `/opt/metar-system` 的实时状态和 `docker compose logs`，再决定是否需要改代码。
