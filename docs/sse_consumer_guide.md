# M2 SSE 实时数据流接入指南

本文档面向希望从 M2 实时接收 METAR 数据更新的事件消费者（如 T0TX 交易机器人、监控面板等）。

## 1. 接口端点

```text
GET /api/v1/metar/stream
```

### 1.1 请求参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `icaos` | string | 否 | 全部机场 | 逗号分隔的 ICAO 机场代码，只推送这些机场的事件 |
| `heartbeat` | float | 否 | 10 | 心跳间隔秒数，范围 1~60，用于保持长连接 |

### 1.2 请求示例

```bash
# 本地开发/测试
curl -N "http://localhost:8000/api/v1/metar/stream?icaos=ZBAA,ZGGG,ZSPD&heartbeat=5"

# PRD：请将 YOUR_M2_SSE_URL 替换为管理员私下提供的 VPS IP（Nginx 80 端口）
curl -N "http://YOUR_M2_SSE_URL/api/v1/metar/stream?icaos=ZBAA,ZGGG,ZSPD&heartbeat=5"
```

> **PRD 地址说明**：M2 部署在 VPS 上，通过 Nginx 80 端口对外暴露 SSE。由于当前使用裸 IP 且无认证，具体 IP 由管理员私下告知接入方，不在公开文档中写明。
>
> 建议接入方将地址配置为环境变量，例如：
> ```bash
> export M2_SSE_URL="http://YOUR_M2_SSE_URL"
> ```

如果请求的 `icaos` 包含 M2 未监控的机场，会返回 `404`：

```json
{"detail":"ICAO codes not monitored: XXX"}
```

## 2. SSE 事件格式

M2 使用标准 Server-Sent Events（`text/event-stream`）推送事件。每个事件包含：

```text
event: <event_type>
data: <json_payload>

```

### 2.1 事件类型

| event | 说明 | 触发时机 |
|---|---|---|
| `snapshot` | 连接建立时的当前状态快照 | 客户端刚连上时 |
| `source_update` | 某个数据源产生新数据 | 任一数据源写入 Redis 时 |
| `winner_update` | M2 择优后的最终 METAR 发生变化 | 多源择优后 winner 改变时 |
| `correction` | 官方修正事件 | 同一 `observed_at` 出现不同 hash 时 |
| `error` | 服务端内部错误 | 事件循环异常时 |

### 2.2 通用字段

所有事件（除 heartbeat 外）均包含以下字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `event_id` | string | UUID，用于延迟测量和幂等去重 |
| `event_type` | string | 事件类型 |
| `event_published_at` | string (ISO 8601) | M2 发出该事件的 UTC 时间 |
| `icao` | string | 机场 ICAO 代码 |
| `raw_text` | string | 原始 METAR 文本 |
| `observed_at` | string (ISO 8601) | 观测时间（METAR 中的时间） |
| `updated_at` | string (ISO 8601) | M2 最近一次更新时间 |
| `source` | string | 数据源可读名称，如 `Skyviewor fast-METAR` |
| `source_key` | string | 数据源标识：`skyviewor` / `awc` / `weathergov` / `iem` |
| `hash` | string | METAR 文本的 SHA1 hash，用于去重和修正检测 |
| `temperature_c` | float / null | 解析出的温度（摄氏度） |
| `dewpoint_c` | float / null | 解析出的露点温度（摄氏度） |

### 2.3 snapshot 事件

连接建立时发送，携带当前已缓存的所有请求机场的最新状态。

```json
{
  "event_type": "snapshot",
  "count": 3,
  "data": [
    {
      "icao": "ZBAA",
      "temperature_c": 32.0,
      "dewpoint_c": 25.0,
      "raw_text": "METAR ZBAA 041130Z VRB03MPS CAVOK 32/25 Q1008 NOSIG",
      "observed_at": "2026-08-04T11:30:00+00:00",
      "updated_at": "2026-08-04T11:30:15+00:00",
      "source": "Skyviewor fast-METAR",
      "source_key": "skyviewor",
      "hash": "..."
    }
  ]
}
```

### 2.4 winner_update 事件示例

这是大多数消费者最关注的事件：M2 已经完成多源择优，该机场的最新有效 METAR 已确定。

```json
{
  "event_id": "d59699dc-bb47-49cb-bb09-c65506952c87",
  "event_type": "winner_update",
  "event_published_at": "2026-08-05T00:00:58.305778+00:00",
  "icao": "ZSPD",
  "raw_text": "METAR ZSPD 050000Z 11003MPS 060V150 9999 FEW013 30/28 Q1008 NOSIG",
  "observed_at": "2026-08-05T00:00:00+00:00",
  "updated_at": "2026-08-05T00:00:58.302702+00:00",
  "source": "Skyviewor fast-METAR",
  "source_key": "skyviewor",
  "hash": "ead77f14d4bf268fa21f6540bdbeed3085b729b7",
  "temperature_c": 30.0,
  "dewpoint_c": 28.0,
  "previous_hash": "..."
}
```

### 2.5 source_update 事件示例

表示某个具体数据源有新数据写入，但未必改变 winner。

```json
{
  "event_id": "26b5e7c8-9912-4c50-a646-45b408116c68",
  "event_type": "source_update",
  "event_published_at": "2026-08-05T00:00:58.305778+00:00",
  "icao": "ZSPD",
  "source_key": "skyviewor",
  "raw_text": "METAR ZSPD 050000Z 11003MPS 060V150 9999 FEW013 30/28 Q1008 NOSIG",
  "observed_at": "2026-08-05T00:00:00+00:00",
  "hash": "ead77f14d4bf268fa21f6540bdbeed3085b729b7",
  "temperature_c": 30.0,
  "dewpoint_c": 28.0
}
```

### 2.6 correction 事件示例

当同一 `observed_at` 出现不同 hash 时触发，表示数据源对历史观测做了修正。

```json
{
  "event_id": "...",
  "event_type": "correction",
  "event_published_at": "2026-08-04T05:40:12+00:00",
  "icao": "RJTT",
  "observed_at": "2026-08-04T05:30:00+00:00",
  "source_key": "awc",
  "previous_hash": "...",
  "corrected_hash": "...",
  "previous_raw_text": "...",
  "corrected_raw_text": "..."
}
```

## 3. 心跳机制

M2 会定期发送 SSE 注释维持连接：

```text
: heartbeat

```

- 默认间隔 10 秒，可通过 `heartbeat` 参数调整
- 消费者应忽略以 `:` 开头的注释行
- 若超过 `heartbeat × 3` 未收到任何数据，建议断开重连

## 4. 消费者最佳实践

### 4.1 按机场过滤

建议只订阅需要的机场，减少网络流量和处理压力：

```python
import requests

M2_SSE_URL = "http://YOUR_M2_SSE_URL"  # 管理员私下提供 VPS IP
url = f"{M2_SSE_URL}/api/v1/metar/stream?icaos=ZBAA,ZGGG,ZSPD,ZUCK,ZUUU,ZHHH,ZSQD&heartbeat=5"
response = requests.get(url, stream=True)
for line in response.iter_lines():
    if not line:
        continue
    print(line.decode("utf-8"))
```

### 4.2 断线自动重连

SSE 连接可能因网络抖动、M2 重启、代理超时等原因断开。消费者应实现指数退避重连：

```python
import time
import requests

icaos = "ZBAA,ZGGG,ZSPD"
backoff = 1
max_backoff = 60

while True:
    try:
        M2_SSE_URL = "http://YOUR_M2_SSE_URL"  # 管理员私下提供 VPS IP
        resp = requests.get(
            f"{M2_SSE_URL}/api/v1/metar/stream?icaos={icaos}&heartbeat=5",
            stream=True,
            timeout=30,
        )
        resp.raise_for_status()
        backoff = 1
        for line in resp.iter_lines():
            if not line:
                continue
            # 处理事件...
    except Exception as exc:
        print(f"SSE disconnected: {exc}, reconnect in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)
```

### 4.3 利用 snapshot 恢复状态

重连后会先收到 `snapshot` 事件。消费者应使用 snapshot 恢复当前各机场最新状态，避免在断线期间遗漏数据。

### 4.4 幂等处理

同一 METAR 可能因多个数据源到达而触发多次 `source_update`，但 `winner_update` 只在 hash 变化时触发。消费者可：

- 使用 `event_id` 去重
- 使用 `hash` 判断内容是否真正改变
- 使用 `observed_at` + `icao` 作为业务主键

## 5. 延迟测量字段

M2 与 T0TX 联合进行端到端延迟测量时，建议消费者记录以下时间戳：

| 时间戳 | 含义 | 记录方 |
|---|---|---|
| `observed_at` | METAR 观测时间 | M2 |
| `event_published_at` | M2 发出 SSE 的时间 | M2 |
| `event_id` | 唯一事件 ID | M2 |
| 消费者收到时间 | 消费者本地收到 SSE 的时间 | 消费者 |

典型链路耗时：

```text
observed_at (00:00:00)
  → source_received_at (M2 收到数据, 如 00:00:58.30)
  → winner_published_at (M2 发出 SSE, 如 00:00:58.31)
  → 消费者收到 SSE (如 00:00:58.63)
```

## 6. 部署与代理注意

### 6.1 Nginx 配置

M2 已在 SSE 响应头中设置 `X-Accel-Buffering: no`，防止 Nginx 缓冲事件。如果前端仍有代理，请确保：

```nginx
location /api/v1/metar/stream {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 86400s;
}
```

### 6.2 公网访问

PRD 上 M2 监听 `127.0.0.1:8000`，外部流量通过 Nginx 反向代理进入。消费者应访问 Nginx 暴露的域名/端口，而非直接访问 8000。

## 7. 错误处理

| HTTP 状态码 | 场景 | 建议 |
|---|---|---|
| 200 | 正常，开始接收 SSE | — |
| 404 | `icaos` 参数包含未监控机场 | 检查 ICAO 代码是否在 M2 监控列表 |
| 500 | 服务端内部错误 | 查看 M2 日志，稍后重连 |

连接过程中收到 `event: error` 事件时，建议关闭连接并按指数退避重连。

## 8. 相关文档

- `app/events.py` - M2 内部事件总线实现
- `app/main.py` - SSE 端点实现
- `docs/proposed_time_parsing_alignment.md` - METAR 时间解析相关设计讨论
