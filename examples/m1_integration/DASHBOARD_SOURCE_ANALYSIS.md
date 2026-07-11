# M2 数据源速度分析 —— Dashboard 集成参考文档

本文档供本地 Dashboard 项目参考，用于展示 **M2 两个 METAR 数据源（weather.gov vs AWC）谁更快拿到新数据**。

---

## 1. 数据源背景

M2 当前同时从两个数据源独立采集 METAR：

| 数据源 | 标识 | Redis Key 示例 |
|---|---|---|
| weather.gov / SynopticData | `weathergov` | `metar:KSEA:source:weathergov` |
| AviationWeather.gov (AWC) | `awc` | `metar:KSEA:source:awc` |

M2 每轮轮询（默认 1 秒）会：
1. 同时向两个源批量请求全部监控机场
2. 将各自最新记录写入 source-specific Redis Key
3. 对每个机场择优：优先 `observed_at` 更新，其次延迟更低，默认 weather.gov
4. 将优胜者写入 `metar:{icao}`

---

## 2. Dashboard 推荐使用的接口

### 2.1 单机场最新双源详情

```text
GET http://47.251.25.183/api/v1/metar/sources?icao={ICAO_CODE}
```

**用途**：展示某个机场两个数据源的当前最新记录及当前被选中的 winner。

**响应示例**（KSEA）：

```json
{
  "icao": "KSEA",
  "winner": { ... },
  "weathergov": { ... },
  "awc": { ... }
}
```

### 2.2 单机场历史双源详情

```text
GET http://47.251.25.183/api/v1/metar/sources/history?icao={ICAO_CODE}&hours={N}
```

或

```text
GET http://47.251.25.183/api/v1/metar/sources/history?icao={ICAO_CODE}&start={iso_datetime}&end={iso_datetime}
```

**用途**：按时间窗口展示某机场的多条 METAR 历史记录，每条记录包含两个数据源的发现时间、原始报文、温度及 winner。

**响应示例**（KORD，hours=24）：

```json
{
  "icao": "KORD",
  "count": 3,
  "records": [
    {
      "observed_at": "2026-07-11T01:51:00+00:00",
      "winner": {
        "source": "aviationweather.gov",
        "source_key": "awc",
        "observed_at": "2026-07-11T01:51:00+00:00",
        "updated_at": "2026-07-11T01:53:54.935425+00:00",
        "raw_text": "METAR KORD 110151Z ...",
        "temperature_c": 23.3
      },
      "awc": { ... },
      "weathergov": { ... }
    }
  ]
}
```

**字段说明**：

| 字段 | 说明 |
|---|---|
| `winner` | 当前 M2 择优后采用的记录（与 `GET /api/v1/metar?icao=X` 一致） |
| `weathergov` | weather.gov 数据源最新原始记录 |
| `awc` | AWC 数据源最新原始记录 |
| `observed_at` | METAR 报文自身的观测时间（从 `raw_text` 的 `ddHHMMZ` 解析） |
| `updated_at` | 该记录写入 Redis 的时间 |
| `source_key` | 数据源内部标识：`weathergov` 或 `awc` |

---

## 3. Dashboard 页面建议展示内容

### 3.1 页面名称建议

- "M2 数据源速度对比"
- "METAR 双源竞技分析"
- "M2 采集源延迟监控"

### 3.2 核心指标

#### A. 实时单机场对比

对每个机场，调用 `/api/v1/metar/sources?icao=X`，比较两个数据源：

```
latency_weathergov = weathergov.updated_at - weathergov.observed_at
latency_awc          = awc.updated_at          - awc.observed_at
```

判断规则（与 M2 择优逻辑一致）：

1. 如果 `weathergov.observed_at > awc.observed_at` → weather.gov 拿到了更新的 METAR
2. 如果 `awc.observed_at > weathergov.observed_at` → AWC 拿到了更新的 METAR
3. 如果 `observed_at` 相同：
   - `latency` 更小的一方胜出
4. 还相同 → 平局

**建议展示**：

- 机场代码
- weather.gov 延迟（秒）
- AWC 延迟（秒）
- 当前更快数据源
- 快的秒数
- 当前 M2 采用的 winner 数据源

#### B. 全局胜负统计

对监控列表中的所有机场批量查询后统计：

| 指标 | 说明 |
|---|---|
| AWC 胜出场次 | AWC 更快拿到新数据的机场数 |
| weather.gov 胜出场次 | weather.gov 更快拿到新数据的机场数 |
| 平局场次 | 两源时间/延迟完全一致 |
| AWC 平均领先 | AWC 获胜时，平均快多少秒 |
| weather.gov 平均领先 | weather.gov 获胜时，平均快多少秒 |

#### C. 历史趋势（可选）

如果需要历史趋势，Dashboard 需要自行定时采样（例如每 30 秒）并存储到本地数据库：

```sql
CREATE TABLE metar_source_latency (
    id SERIAL PRIMARY KEY,
    sampled_at TIMESTAMP NOT NULL,
    icao VARCHAR(4) NOT NULL,
    source VARCHAR(20) NOT NULL,        -- 'weathergov' 或 'awc'
    observed_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    latency_seconds FLOAT NOT NULL
);
```

然后可以绘制：
- 各机场两源延迟折线图
- 全局胜负比例饼图/柱状图
- AWC vs weather.gov 平均延迟趋势

---

## 4. 批量获取方案

### 4.1 方式一：循环调用单机场接口（推荐，简单）

```python
import requests

BASE_URL = "http://47.251.25.183"
AIRPORTS = ["KSEA", "VHHH", "EGLL", "ZSPD", "ZBAA", ...]  # 49 个机场

results = []
for icao in AIRPORTS:
    resp = requests.get(f"{BASE_URL}/api/v1/metar/sources?icao={icao}", timeout=10)
    if resp.status_code == 200:
        results.append(resp.json())
```

**注意**：49 个机场循环调用约 49 次 HTTP 请求，建议加并发（如 `asyncio` + `aiohttp`）或缓存。

### 4.2 方式二：直接读 Redis（适合本地同机或内网）

如果 Dashboard 能访问 M2 VPS 的 Redis（默认不对外开放），可以直接读取：

```bash
# 读取全部 source-specific keys
redis-cli --json KEYS 'metar:*:source:*'
redis-cli --json MGET metar:KSEA:source:weathergov metar:KSEA:source:awc
```

Python 示例：

```python
import redis
import json

r = redis.Redis(host='47.251.25.183', port=6379, db=0, decode_responses=True)
keys = r.keys('metar:*:source:*')
values = r.mget(keys)
records = [json.loads(v) for v in values if v]
```

**安全提示**：生产环境不建议将 Redis 端口暴露到公网，建议通过 SSH 隧道或 M2 API 访问。

---

## 5. 参考代码片段

### 5.1 判断某个机场谁更快

```python
from datetime import datetime


def parse_dt(value: str) -> datetime:
    """解析 ISO 8601 时间字符串。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def compare_sources(data: dict) -> dict:
    """比较两个数据源，返回胜负信息。"""
    icao = data["icao"]
    wg = data.get("weathergov")
    awc = data.get("awc")

    if not wg or not awc:
        return {"icao": icao, "winner": "N/A", "reason": "缺少一方数据"}

    wg_obs = parse_dt(wg["observed_at"])
    awc_obs = parse_dt(awc["observed_at"])
    wg_upd = parse_dt(wg["updated_at"])
    awc_upd = parse_dt(awc["updated_at"])

    wg_latency = (wg_upd - wg_obs).total_seconds()
    awc_latency = (awc_upd - awc_obs).total_seconds()

    if wg_obs > awc_obs:
        winner = "weather.gov"
        diff = (wg_obs - awc_obs).total_seconds()
        reason = f"拿到更新的 METAR，领先 {diff:.0f} 秒"
    elif awc_obs > wg_obs:
        winner = "AWC"
        diff = (awc_obs - wg_obs).total_seconds()
        reason = f"拿到更新的 METAR，领先 {diff:.0f} 秒"
    elif wg_latency < awc_latency:
        winner = "weather.gov"
        diff = awc_latency - wg_latency
        reason = f"同一条 METAR，延迟更低，领先 {diff:.0f} 秒"
    elif awc_latency < wg_latency:
        winner = "AWC"
        diff = wg_latency - awc_latency
        reason = f"同一条 METAR，延迟更低，领先 {diff:.0f} 秒"
    else:
        winner = "平局"
        reason = "时间和延迟完全相同"

    return {
        "icao": icao,
        "winner": winner,
        "reason": reason,
        "weathergov_latency_s": wg_latency,
        "awc_latency_s": awc_latency,
        "weathergov_observed_at": wg_obs,
        "awc_observed_at": awc_obs,
    }
```

### 5.2 全局统计

```python
from collections import Counter


def summarize(results: list[dict]) -> dict:
    """统计多个机场的胜负情况。"""
    winners = [r["winner"] for r in results if r["winner"] != "N/A"]
    counts = Counter(winners)

    awc_wins = [r for r in results if r["winner"] == "AWC"]
    wg_wins = [r for r in results if r["winner"] == "weather.gov"]

    def avg_lead(wins, target):
        diffs = []
        for r in wins:
            if target == "AWC":
                diffs.append(r["weathergov_latency_s"] - r["awc_latency_s"])
            else:
                diffs.append(r["awc_latency_s"] - r["weathergov_latency_s"])
        return sum(diffs) / len(diffs) if diffs else 0

    return {
        "total_compared": len(winners),
        "awc_wins": counts.get("AWC", 0),
        "weathergov_wins": counts.get("weather.gov", 0),
        "ties": counts.get("平局", 0),
        "awc_avg_lead_seconds": avg_lead(awc_wins, "AWC"),
        "weathergov_avg_lead_seconds": avg_lead(wg_wins, "weather.gov"),
    }
```

---

## 6. 注意事项

1. **UUWW 特殊处理**：AWC 黑名单包含 `UUWW`，因此 UUWW 只有 weather.gov 数据，不存在双源对比。
2. **时间基准统一**：M2 统一从 `raw_text` 的 `ddHHMMZ` 解析 `observed_at`，不要与数据源的 `reportTime` / `date_time` 混用。
3. **延迟计算**：
   - `latency = updated_at - observed_at`
   - 反映的是 "METAR 发出后多久被 M2 写入 Redis"
   - 不是纯网络延迟，包含数据源自身的发布延迟 + 请求间隔 + 解析时间
4. **采样频率**：Dashboard 刷新建议 10~30 秒一次，不要低于 M2 的轮询间隔（1 秒）。
5. **缓存建议**：如果同时展示 49 个机场，建议将 `/api/v1/metar/sources` 结果缓存 5~10 秒，避免重复请求。

---

## 7. 接口变更追踪

| 时间 | 变更 |
|---|---|
| 2026-07-10 | 新增 `GET /api/v1/metar/sources?icao=X` |
| 2026-07-10 | M2 改为双源独立采集，新增 source-specific Redis keys |

如有接口变更，本文档会同步更新。
