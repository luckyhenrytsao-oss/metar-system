# M2 批量温度接口 —— 外部项目集成参考

本目录提供 M2 `POST /api/v1/metar/batch` 的参考实现与调用示例，
供其他项目（如 M1）按自身工程规范移植时使用。

## 接口地址

```text
POST http://47.251.25.183/api/v1/metar/batch
Content-Type: application/json
```

## 请求体

```json
{
  "icaos": ["VHHH", "KSEA", "EGLL", "ZSPD"]
}
```

## 响应体

```json
{
  "data": [
    {
      "icao": "VHHH",
      "temperature_c": 30.0,
      "dewpoint_c": 26.0,
      "raw_text": "VHHH 051330Z 18016KT 150V210 9999 VCSH FEW010 SCT025 30/26 Q1008  NOSIG",
      "observed_at": "2026-07-05T13:30:00+00:00",
      "updated_at": "2026-07-05T13:41:02.099261+00:00",
      "source": "weather.gov"
    }
  ],
  "missing": ["EGLL"],
  "count": 1
}
```

- `data`：成功获取并解析出温度的机场列表
- `missing`：Redis 中暂无数据、不在监控列表、或无法解析温度的机场
- `count`：`data` 的长度

## 当前监控机场列表

VPS 上 M2 当前监控 M1 的 49 个机场：

```text
ZSPD,ZBAA,EGLC,RKSI,WSSS,LFPB,KSEA,KATL,KORD,KLGA,RJTT,KDAL,ZUUU,ZUCK,ZGGG,
ZGSZ,ZHHH,ZSQD,EPWA,WMKK,RCSS,LLBG,RKPK,LIMC,KMIA,NZWN,KBKF,RPLL,CYYZ,SBGR,
EDDM,KLAX,KAUS,EHAM,SAEZ,KSFO,LTAC,LTFM,FACT,VILK,OPKC,MPMG,KHOU,MMMX,OEJN,
EFHK,LEMD,VHHH,UUWW
```

## 参考实现

- [`metar_client.py`](metar_client.py)：Python `requests` 客户端封装

## 使用示例

```python
from examples.m1_integration.metar_client import MetarClient

client = MetarClient(base_url="http://47.251.25.183")
records = client.fetch_temperatures(["VHHH", "KSEA", "EGLL", "ZSPD"])
for r in records:
    print(r["icao"], r["temperature_c"], r["dewpoint_c"])
```

## 数据源说明

M2 当前从两个数据源独立采集并择优：

- **weather.gov / SynopticData**：`metar:{icao}:source:weathergov`
- **AviationWeather.gov (AWC)**：`metar:{icao}:source:awc`

最终返回给外部项目的 `source` 字段表示该机场当前被采纳的数据源。
`GET /api/v1/metar/sources?icao={icao}` 可查看两个数据源的原始记录。

## 注意事项

- 目标项目应把这个客户端按自己的目录结构、日志、配置管理方式重写，
  而不是直接引用 `examples/` 下的文件。
- 不要把 M2 的调用逻辑直接耦合到目标项目的核心采集循环里，
  建议作为独立的数据源或辅助任务运行。
