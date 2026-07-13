# M2 METAR 官方修正事件 Dashboard 集成文档

## 背景

weather.gov / SynopticData 等官方数据源，偶尔会对**同一个观测时间**的 METAR 发布修正版本（retroactive correction）。例如 VILK 在 2026-07-12 21:30Z 先发布 30°C 版本，约 20 分钟后又改为 29°C 版本。这类事件罕见但可能导致严重后果，M2 需要发现并记录。

---

## 1. 修正事件的定义

### 触发条件

同时满足以下四点：

1. **同一机场**（icao）
2. **同一数据源**（source_key：`weathergov` 或 `awc`）
3. **同一观测时间**（observed_at，即从 METAR 报文 `ddHHMMZ` 解析出的真实时间）
4. **不同 hash 的官方 METAR/SPECI 报文**：后续收到的报文与之前已记录报文的 SHA1 hash 不同

> 注：只检测**同一数据源内部**的修正。AWC 与 weather.gov 之间的内容差异属于“双源不一致”，由 `/api/v1/metar/sources/history` 接口覆盖。

---

## 2. 查询接口

### `GET /api/v1/metar/corrections`

查询 M2 检测到的 METAR 官方修正事件。

#### 请求参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `hours` | int | 否 | 过去多少小时（1~720），默认 24；与 `start`/`end` 二选一 |
| `start` | string | 否 | ISO 8601 起始时间，UTC；与 `hours` 二选一 |
| `end` | string | 否 | ISO 8601 结束时间，UTC；默认当前时间 |
| `icao` | string | 否 | 按机场过滤，如 `VILK` |
| `source` | string | 否 | 按数据源过滤：`weathergov` 或 `awc` |
| `limit` | int | 否 | 最多返回条数，默认 100，最大 1000 |

#### 返回示例

```json
{
  "count": 1,
  "start": "2026-07-12T02:21:00+00:00",
  "end": "2026-07-13T02:21:00+00:00",
  "events": [
    {
      "icao": "VILK",
      "source": "weather.gov",
      "source_key": "weathergov",
      "observed_at": "2026-07-12T21:30:00+00:00",
      "first_updated_at": "2026-07-12T21:35:25.455557+00:00",
      "first_hash": "73e53c754b7851f2a3127de264e0d9b6f3274e49",
      "first_raw_text": "METAR VILK 122130Z 22008KT 4000 HZ FEW020 SCT100 30/24 Q0997 NOSIG",
      "corrected_updated_at": "2026-07-12T21:59:24.288383+00:00",
      "corrected_hash": "76f72f755ab02e09981a2ebedb647d93cfcba228",
      "corrected_raw_text": "METAR VILK 122130Z 23006KT 4000 HZ FEW020 SCT100 29/24 Q0997 NOSIG",
      "detected_at": "2026-07-12T21:59:24.300000+00:00",
      "correction_delay_seconds": 1438.83
    }
  ]
}
```

#### 字段说明

| 字段 | 含义 |
|------|------|
| `icao` | 机场 ICAO 代码 |
| `source` | 数据源可读名称，如 `weather.gov` |
| `source_key` | 数据源键，`weathergov` 或 `awc` |
| `observed_at` | METAR 报文本身的观测时间（UTC） |
| `first_updated_at` | M2 首次采集到该 observed_at 版本的时间 |
| `first_raw_text` | 首次版本的完整 METAR 报文 |
| `corrected_updated_at` | M2 采集到修正版本的时间 |
| `corrected_raw_text` | 修正版本的完整 METAR 报文 |
| `detected_at` | M2 检测到本次修正事件的时间 |
| `correction_delay_seconds` | 从首次版本到修正版本的时间差（秒） |

---

## 3. Dashboard 推荐展示方式

### 3.1 独立页面：METAR 修正事件列表

建议新增一个页面 `/dashboard/m2-corrections`（路径可自定），核心元素：

- **时间范围选择器**：过去 24h / 7d / 30d / 自定义
- **机场过滤输入框**：可选，输入 ICAO 代码
- **数据源过滤下拉框**：全部 / weather.gov / AWC
- **事件表格**：
  - 发生时间（detected_at）
  - 机场
  - 数据源
  - 观测时间（observed_at）
  - 修正延迟（correction_delay_seconds，换算为分钟）
  - 首次温度 vs 修正后温度（解析 `first_raw_text` 和 `corrected_raw_text` 中的温度）
  - 操作列：展开查看两条完整报文对比

### 3.2 温度变化高亮

建议计算并展示：

```python
from app.main import parse_temperature

first_temp, first_dew = parse_temperature(first_raw_text)
corrected_temp, corrected_dew = parse_temperature(corrected_raw_text)
temp_delta = corrected_temp - first_temp
```

- `temp_delta == 0`：报文修正但温度未变
- `temp_delta != 0`：**温度被修正**，需要重点关注

### 3.3 与历史接口联动

点击某行事件时，可跳转调用：

```
GET /api/v1/metar/sources/history?icao={icao}&start={observed_at-1h}&end={observed_at+1h}
```

查看同一时刻 AWC 与 weather.gov 的双源对比。

---

## 4. 推荐调用示例

### 查询过去 24 小时所有修正事件

```bash
curl 'http://47.251.25.183/api/v1/metar/corrections?hours=24'
```

### 查询 VILK 过去 7 天

```bash
curl 'http://47.251.25.183/api/v1/metar/corrections?icao=VILK&hours=168'
```

### 查询 weather.gov 源过去 30 天

```bash
curl 'http://47.251.25.183/api/v1/metar/corrections?source=weathergov&hours=720&limit=500'
```

---

## 5. 注意事项

1. **保留时长**：修正事件默认保留 **360 天**。
2. **事件唯一性**：同一 `observed_at` + `source` 可能出现多次修正，每次都会独立记录。
3. **不会重复记录**：如果后续 batch 再次带回已记录过的旧版本，不会触发新的修正事件。
4. **首次版本判定**：以 M2 首次采集到该 hash 的 `updated_at` 为准，不一定是数据源真正首次发布时间。
5. **性能**：索引使用 Redis Sorted Set，按 `detected_at` 查询效率高，可放心高频调用。

---

## 6. 后续可扩展

- 当检测到温度变化非零的修正事件时，发送 Telegram / 邮件告警。
- 对经常发生修正的机场或数据源做统计排名。
- 将修正事件与 M2 当前对外提供的 winner 关联，分析修正是否导致 M2 最终温度发生变化。
