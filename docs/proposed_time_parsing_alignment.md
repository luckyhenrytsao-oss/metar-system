# M2 时间解析对齐方案设计文档

> 状态：设计完成，**暂不实施**  
> 目的：响应 T0TX 在 `metar_fetcher.py` / `weathergov_client.py` 中修复的 misdate bug，评估 M2 是否需要同类修改。

---

## 1. 背景

T0TX 近期修复了一个 METAR 时间解析 bug：

- **AWC**：旧代码从 `rawOb` 文本解析 `ddHHMMZ`，在某些场景下会 misdate。修复后改为优先使用 API 返回的 `obsTime`（Unix 秒绝对时间），缺失时才 fallback 到 `rawOb` 解析。
- **weather.gov（SynopticData）**：旧代码从 `rawOb` 解析时间并做 ±12h wrap，导致跨天/跨月边界 misdate。修复后直接使用 `date_time` 作为 UTC 时间，并删除了 `_parse_raw_metar_time`。

M2 目前**没有使用** AWC 的 `obsTime` 和 WG 的 `date_time`，而是统一从 `rawOb` / `raw_text` 的 `ddHHMMZ` 解析时间。本方案讨论是否应将 M2 改为与上游 API 的绝对时间对齐。

---

## 2. 当前 M2 行为

### 2.1 统一时间解析函数

```python
def _parse_metar_time(raw_metar: str, base_time: datetime) -> Optional[datetime]:
    # 从 raw_metar 匹配 ddHHMMZ
    # 用 base_time 的年月，尝试上月/当月/下月三个候选
    # 选与 base_time 绝对差最小的那个
```

特点：
- 尊重 METAR 文本中的明确日期（日）。
- 用服务器当前时间 `base_time` 推断月份/年份。
- 跨月边界逻辑比简单的 ±12h wrap 更稳健。

### 2.2 各数据源现状

| 源 | 当前做法 | 上游可用绝对时间 | 风险 |
|---|---|---|---|
| **weather.gov** | 从 `metar_set_1[i]` 的 `rawOb` 解析 | `OBSERVATIONS.date_time[i]` | 中：rawOb 与 date_time 理论上应一致，但上游异常时可能 misdate |
| **AWC** | 从 `rawOb` 解析 | `obsTime`（Unix 秒） | 中：同上 |
| **IEM LDM** | 从 bulletin 的 `raw_text` 解析 | 无精确到达时间 | 低~中：只能解析 raw_text；未来可用 LDM 到达时间辅助校验 |

---

## 3. 实际数据对比结果

2026-08-02 对当前 49 个监控机场做一次抽样对比：

| 源 | 对比对数 | 不一致（>60s） | 结论 |
|---|---|---|---|
| **AWC** | 77 | 0 | 当前 `obsTime` 与 rawOb 解析结果完全一致 |
| **weather.gov** | 168 | 0 | 当前 `date_time` 与 rawOb 解析结果完全一致 |

> 注：当前一致不代表未来永远一致。上游 API 完全可能在单条报文中出现 `date_time`/`obsTime` 与 `rawOb` 不一致。

---

## 4. 建议修改方案

### 4.1 weather.gov（SynopticData）

修改位置：`app/collector.py` 中 `_extract_weathergov_metars`。

当前逻辑（节选）：

```python
raw_metar = str(metars[selected_idx]).strip()
obs_time = _parse_metar_time(raw_metar, _now_utc())
```

建议改为：

```python
raw_metar = str(metars[selected_idx]).strip()

# 优先使用 API 返回的绝对时间 date_time
api_time = _parse_iso_time(times[selected_idx]) if selected_idx < len(times) else None
if api_time:
    obs_time = api_time
else:
    obs_time = _parse_metar_time(raw_metar, _now_utc())
```

注意点：
- `date_time` 是 ISO 8601 字符串，使用现有 `_parse_iso_time` 即可。
- 保留 rawOb 解析作为 fallback，防止 `date_time` 缺失。
- 可额外增加一致性校验：若 `api_time` 与 `parsed_time` 差异超过阈值（如 1 小时），记录 warning。

### 4.2 AWC（AviationWeather.gov）

修改位置：`app/collector.py` 中 `_fetch_awc_batch`。

当前逻辑（节选）：

```python
chosen_raw = chosen["rawOb"]
obs_time = _parse_metar_time(chosen_raw, _now_utc())
```

建议改为：

```python
chosen_raw = chosen["rawOb"]
obs_time_from_api = None
obs_time_val = chosen.get("obsTime")
if obs_time_val is not None:
    try:
        obs_time_from_api = datetime.fromtimestamp(int(obs_time_val), tz=timezone.utc)
    except (ValueError, TypeError):
        pass

if obs_time_from_api:
    obs_time = obs_time_from_api
else:
    obs_time = _parse_metar_time(chosen_raw, _now_utc())
```

注意点：
- AWC 的 `obsTime` 是 Unix 秒级时间戳。
- 同样保留 rawOb 解析作为 fallback。
- 可增加与 WG 类似的一致性 warning。

### 4.3 IEM LDM

IEM 原始数据没有 API 提供的绝对时间戳，只能继续从 `raw_text` 解析。

但可作为本方案的相关改进：
- 在 TODO #4（LDM 精确到达时间记录）落地后，可用 `receive_time - obs_time` 做异常检测。
- 若某条 bulletin 的 `obs_time` 与 `receive_time` 差距异常大（如 >30 分钟），可标记为潜在 misdate。

---

## 5. 不变与保留

以下逻辑**不需要改**：

- `_parse_metar_time` 函数本身：保留作为 IEM 和 fallback 场景使用。
- `_is_observed_at_valid` 新鲜度校验：保留，用于过滤未来/过期数据。
- 择优逻辑：不受时间解析方式影响，只要 `observed_at` 一致即可。

---

## 6. 测试方案

1. **单元测试**
   - 构造 `date_time` / `obsTime` 与 `rawOb` 一致的 mock 响应，验证解析结果不变。
   - 构造 `date_time` / `obsTime` 与 `rawOb` 不一致的 mock 响应，验证优先采用 API 绝对时间。
   - 构造 `date_time` / `obsTime` 缺失的 mock 响应，验证 fallback 到 rawOb 解析。

2. **集成测试**
   - 在测试环境或本地 Docker 中运行 1 小时，对比修改前后 `observed_at` 分布是否一致。
   - 重点观察跨 UTC 00:00 时段的机场。

3. **生产灰度**
   - 若实施，建议先在非交易时段部署，观察 24 小时无异常后再全面切换。

---

## 7. 回滚策略

改动范围小，仅涉及 `app/collector.py` 中两个函数的时间解析逻辑：

- 回滚方式：直接 revert 对应 commit，或把优先级判断封装成配置开关（如 `USE_API_ABSOLUTE_TIME=true/false`）。
- 建议增加配置开关，便于线上快速切换而不需要重新发版。

---

## 8. 决策触发条件

**本次修改暂不实施。** 建议等待 T0TX 在新代码上运行一段时间（建议 1~2 周），观察：

1. T0TX 是否还出现类似的 misdate 交易事件；
2. T0TX 是否发现 AWC / WG 的 `obsTime` / `date_time` 与 `rawOb` 确实存在不一致的实例；
3. M2 是否出现因时间解析导致的异常 correction 或 winner 跳变。

若 T0TX 运行稳定且未发现新问题，可继续暂缓；若再次出现 misdate 事件，或 M2 监测到上游时间不一致，则启动本方案实施。

---

## 9. 相关文件

- `app/collector.py`：主要修改文件
- `tests/test_collector.py`：需要补充测试
- `HANDOFF.md`：已加入 TODO 列表
