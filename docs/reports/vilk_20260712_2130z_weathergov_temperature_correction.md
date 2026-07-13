# VILK 2026-07-12 21:30Z weather.gov 温度修正事件报告

## 事件摘要

| 项目 | 内容 |
|------|------|
| 机场 | VILK（Lucknow / 印度） |
| 观测时间 | 2026-07-12 21:30:00 UTC（METAR 时间 `122130Z`） |
| 数据源 | weather.gov / SynopticData |
| 首次版本温度 | **30.0°C** |
| 修正后温度 | **29.0°C** |
| 温度变化 | **-1.0°C** |
| 风向变化 | 220°/8KT → 230°/6KT |
| 修正延迟 | 约 **23 分 59 秒**（21:35:25 → 21:59:24） |

---

## 两条报文对比

| 字段 | 首次版本 | 修正版本 |
|------|----------|----------|
| 来源 | weather.gov | weather.gov |
| hash | `73e53c75` | `76f72f75` |
| METAR 时间 | 122130Z | 122130Z |
| 风向风速 | 22008KT | 23006KT |
| 能见度 | 4000 HZ | 4000 HZ |
| 云 | FEW020 SCT100 | FEW020 SCT100 |
| **温度/露点** | **30/24** | **29/24** |
| 气压 | Q0997 | Q0997 |
| 趋势 | NOSIG | NOSIG |
| 完整报文 | `METAR VILK 122130Z 22008KT 4000 HZ FEW020 SCT100 30/24 Q0997 NOSIG` | `METAR VILK 122130Z 23006KT 4000 HZ FEW020 SCT100 29/24 Q0997 NOSIG` |
| M2 采集时间 | 2026-07-12 21:35:25 UTC | 2026-07-12 21:59:24 UTC |

---

## 三方数据源交叉验证

| 数据源 | 122130Z 报文 | 温度 | 风向 | 与 M2 关系 |
|--------|--------------|------|------|-----------|
| **M2 / weather.gov（首次）** | `METAR VILK 122130Z 22008KT 4000 HZ FEW020 SCT100 30/24 Q0997 NOSIG` | 30°C | 220° | M2 于 21:35:25 采集 |
| **M2 / weather.gov（修正）** | `METAR VILK 122130Z 23006KT 4000 HZ FEW020 SCT100 29/24 Q0997 NOSIG` | 29°C | 230° | M2 于 21:59:24 采集 |
| **AWC** | `METAR VILK 122130Z 22008KT 4000 HZ FEW020 SCT100 30/24 Q0997 NOSIG` | 30°C | 220° | 与 weather.gov 首次版本一致 |
| **IEM (Iowa)** | `VILK 122130Z 22008KT 4000 HZ FEW020 SCT100 30/24 Q0997 NOSIG` | 30°C | 220° | 独立第三方，与 AWC / WG 首次一致 |

---

## 核心结论

1. **weather.gov 对同一观测时间发布了两个不同版本**
   - 21:35Z 左右给出 30°C / 22008KT 版本
   - 21:59Z 左右同一 `122130Z` 时间改为 29°C / 23006KT 版本
   - 两条报文都不是 `COR`、`SPECI` 或 `AUTO`，均为普通 `METAR`

2. **AWC 与第三方 IEM 均支持首次版本（30°C）**
   - 这说明 weather.gov 的修正版本（29°C）与 AWC / IEM 不一致
   - 不能简单认为修正后的版本就是“更正后正确版本”

3. **M2 的 winner 分析被历史重复写入污染**
   - 当前 `history` 接口显示 weather.gov 29°C 为 winner
   - 实际上 AWC 30°C 版本在 21:32:31 就已到达 M2，早于 weather.gov 修正版本
   - 因 AWC 后续 batch 重复带回同一报文，导致 `updated_at` 被刷新到 22:03:03，在 latency 比较中吃亏
   - 本次 M2 代码更新已修复历史去重逻辑（同 observed_at + 同 hash 不再重复追加）

4. **M1 在 21:32:36 读到 30°C 是合理的**
   - 当时 M2 对外提供的 winner 确实是 AWC 的 30°C 版本
   - 与 M1 采集记录完全一致

---

## 风险与启示

1. **weather.gov 会 retroactively 修正已发布 METAR**
   - 不是 COR/SPECI，而是静默替换同一 `observed_at` 的内容
   - 对依赖历史时间戳做对比的系统会造成困惑

2. **单一数据源不可盲目信任**
   - 即使是官方 weather.gov，也可能出现与 AWC / IEM 不一致的版本
   - 双源或多源仲裁机制很重要

3. **需要持续监控修正事件**
   - 本次事件促使 M2 新增 `/api/v1/metar/corrections` 接口
   - 后续可据此分析哪些机场、哪些数据源最容易出现修正

---

## 相关代码变更

- `app/collector.py`：新增同一数据源内 METAR 修正事件检测
- `app/database.py`：新增 `record_correction_event`、`get_correction_events`，并修复历史记录重复写入
- `app/main.py`：新增 `GET /api/v1/metar/corrections`
- `docs/m2_corrections_dashboard_integration.md`：Dashboard 集成说明

---

## 附录：查询命令

```bash
# M2 双源历史
http://47.251.25.183/api/v1/metar/sources/history?icao=VILK&hours=48

# M2 修正事件（本事件因发生在功能上线前，无法回溯）
http://47.251.25.183/api/v1/metar/corrections?icao=VILK&source=weathergov&hours=720

# AWC raw
https://aviationweather.gov/api/data/metar?ids=VILK&format=raw&hours=24

# AWC JSON
https://aviationweather.gov/api/data/metar?ids=VILK&format=json&hours=24
```

---

*报告生成时间：2026-07-13 UTC*
