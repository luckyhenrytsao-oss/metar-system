"""FastAPI 入口：挂载路由与后台异步采集任务."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from app.collector import close_http_client, start_collector_loop
from app.config import Settings, get_settings
from app.database import (
    close_redis,
    get_correction_events,
    get_metar,
    get_redis,
    get_source_history,
    get_source_metar,
    parse_iso,
)
from app.events import publish_event, subscribe, unsubscribe

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# 后台采集任务引用
_collector_task: Optional[asyncio.Task] = None  # type: ignore[name-defined]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动采集器，关闭连接."""
    global _collector_task
    settings = get_settings()
    logging.getLogger().setLevel(getattr(logging, settings.log_level, logging.INFO))

    # 预热 Redis 连接
    await get_redis(settings)

    # 启动后台采集循环
    _collector_task = asyncio.create_task(start_collector_loop(settings))
    logger.info("FastAPI startup complete, collector loop started")

    yield

    # 关闭阶段
    if _collector_task and not _collector_task.done():
        _collector_task.cancel()
        try:
            await _collector_task
        except asyncio.CancelledError:
            pass

    await close_http_client()
    await close_redis()
    logger.info("FastAPI shutdown complete")


app = FastAPI(
    title="METAR High-Speed Distribution System",
    version="0.1.0",
    lifespan=lifespan,
)


async def _get_redis_dependency():
    """FastAPI 依赖：注入 Redis 连接."""
    return await get_redis()


@app.get("/api/v1/metar")
async def get_metar_endpoint(
    icao: str = Query(..., min_length=3, max_length=4, description="ICAO 机场代码"),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
    settings: Settings = Depends(get_settings),
    redis_client: Any = Depends(_get_redis_dependency),
):
    """获取指定机场的 METAR 数据.

    - 若 ICAO 不在监控列表，返回 404
    - 若 Redis 中无数据，返回 404
    - 若 If-None-Match 与当前 hash 一致，返回 304（空 Body）
    - 否则返回 JSON，并带上 ETag 头
    """
    icao = icao.upper()

    # 校验机场是否在监控列表
    if icao not in {code.upper() for code in settings.monitor_airports_list}:
        raise HTTPException(
            status_code=404,
            detail=f"ICAO code {icao} is not in the monitored airport list",
        )

    data = await get_metar(redis_client, icao)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"No METAR data available for {icao} yet",
        )

    current_hash = data.get("hash", "")

    # HTTP 304 优化：客户端已有最新数据，直接返回空 Body
    if if_none_match and if_none_match.strip('"') == current_hash:
        return Response(status_code=304)

    return JSONResponse(
        content=data,
        headers={"ETag": f'"{current_hash}"'},
    )


@app.get("/api/v1/metar/sources")
async def get_metar_sources(
    icao: str = Query(..., min_length=3, max_length=4, description="ICAO 机场代码"),
    settings: Settings = Depends(get_settings),
    redis_client: Any = Depends(_get_redis_dependency),
):
    """获取指定机场两个数据源各自的原始 METAR 记录以及当前择优后的最终记录.

    - 若 ICAO 不在监控列表，返回 404
    - 返回结构:
        {
            "icao": "KSEA",
            "winner": { ... },
            "weathergov": { ... } | null,
            "awc": { ... } | null
        }
    """
    icao = icao.upper()

    if icao not in {code.upper() for code in settings.monitor_airports_list}:
        raise HTTPException(
            status_code=404,
            detail=f"ICAO code {icao} is not in the monitored airport list",
        )

    winner = await get_metar(redis_client, icao)
    weathergov = await get_source_metar(redis_client, icao, "weathergov")
    awc = await get_source_metar(redis_client, icao, "awc")

    return JSONResponse(
        content={
            "icao": icao,
            "winner": winner,
            "weathergov": weathergov,
            "awc": awc,
        }
    )


@app.get("/api/v1/metar/sources/history")
async def get_metar_sources_history(
    icao: str = Query(..., min_length=3, max_length=4, description="ICAO 机场代码"),
    hours: Optional[int] = Query(None, ge=1, le=168, description="过去多少小时（1~168），与 start/end 二选一"),
    start: Optional[str] = Query(None, description="ISO 8601 起始时间，UTC，与 hours 二选一"),
    end: Optional[str] = Query(None, description="ISO 8601 结束时间，UTC，默认当前时间"),
    settings: Settings = Depends(get_settings),
    redis_client: Any = Depends(_get_redis_dependency),
):
    """获取指定机场在一段时间内多条 METAR 的双源速度对比历史.

    支持两种参数组合：
      - icao + hours：查询过去 hours 小时
      - icao + start + [end]：查询指定时间窗口

    返回按 observed_at 降序排列的记录数组，每条记录包含该 METAR
    在 AWC 与 weather.gov 中的发现时间及当前 M2 采用的 winner。
    """
    icao = icao.upper()

    if icao not in {code.upper() for code in settings.monitor_airports_list}:
        raise HTTPException(
            status_code=404,
            detail=f"ICAO code {icao} is not in the monitored airport list",
        )

    # 解析时间窗口
    now = datetime.now(timezone.utc)
    if hours is not None:
        if start is not None or end is not None:
            raise HTTPException(
                status_code=422,
                detail="hours 与 start/end 不能同时使用",
            )
        start_dt = now - timedelta(hours=hours)
        end_dt = now
    else:
        if start is None:
            raise HTTPException(
                status_code=422,
                detail="必须提供 hours 或 start 参数",
            )
        start_dt = parse_iso(start)
        if start_dt is None:
            raise HTTPException(
                status_code=422,
                detail=f"无法解析 start 时间: {start}",
            )
        end_dt = parse_iso(end) if end else now
        if end_dt is None:
            raise HTTPException(
                status_code=422,
                detail=f"无法解析 end 时间: {end}",
            )

    if end_dt < start_dt:
        raise HTTPException(
            status_code=422,
            detail="end 必须晚于 start",
        )

    # 读取两个数据源的历史记录
    weathergov_records = await get_source_history(redis_client, icao, "weathergov", start_dt, end_dt)
    awc_records = await get_source_history(redis_client, icao, "awc", start_dt, end_dt)

    # 合并：按 observed_at 分组
    grouped: dict[str, dict[str, Any]] = {}

    def _group_record(record: dict[str, Any], source_key: str) -> None:
        obs = record.get("observed_at")
        if not obs:
            return
        item = grouped.setdefault(
            obs,
            {
                "observed_at": obs,
                "weathergov": None,
                "awc": None,
            },
        )
        # 补充 temperature_c
        record_out = dict(record)
        record_out["temperature_c"], record_out["dewpoint_c"] = parse_temperature(record_out.get("raw_text", ""))
        item[source_key] = record_out

    for r in weathergov_records:
        _group_record(r, "weathergov")
    for r in awc_records:
        _group_record(r, "awc")

    # 为每个 observed_at 计算 winner
    records: list[dict[str, Any]] = []
    for obs, item in grouped.items():
        winner = _select_history_winner(item["weathergov"], item["awc"])
        item["winner"] = winner
        records.append(item)

    # 按 observed_at 降序
    records.sort(key=lambda x: x["observed_at"], reverse=True)

    return JSONResponse(
        content={
            "icao": icao,
            "count": len(records),
            "records": records,
        }
    )


def _select_history_winner(
    weathergov_record: Optional[dict[str, Any]],
    awc_record: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """从历史记录的两个数据源条目中选出优胜者.

    规则与采集器一致：observed_at 更晚优先；相同时延迟更低优先；
    还相同则默认 weather.gov。history 接口中同一 observed_at 分组内
    observed_at 必然相同，因此实际比较延迟。
    """
    if weathergov_record is None:
        return awc_record
    if awc_record is None:
        return weathergov_record

    wg_updated = parse_iso(weathergov_record.get("updated_at"))
    awc_updated = parse_iso(awc_record.get("updated_at"))

    if wg_updated is None:
        return awc_record
    if awc_updated is None:
        return weathergov_record

    if awc_updated < wg_updated:
        return awc_record
    return weathergov_record


@app.get("/api/v1/metar/corrections")
async def get_metar_corrections(
    hours: Optional[int] = Query(None, ge=1, le=720, description="过去多少小时（1~720，默认24），与 start/end 二选一"),
    start: Optional[str] = Query(None, description="ISO 8601 起始时间，UTC，与 hours 二选一"),
    end: Optional[str] = Query(None, description="ISO 8601 结束时间，UTC，默认当前时间"),
    icao: Optional[str] = Query(None, min_length=3, max_length=4, description="可选：按机场过滤"),
    source: Optional[str] = Query(None, description="可选：按数据源过滤，weathergov 或 awc"),
    limit: int = Query(100, ge=1, le=1000, description="最多返回条数"),
    redis_client: Any = Depends(_get_redis_dependency),
):
    """查询 METAR 官方修正事件.

    触发条件：同一个数据源、同一个机场、同一个 observed_at，
    后续收到了与之前不同 hash 的官方 METAR/SPECI 报文。

    支持参数：
      - hours / start / end：时间窗口
      - icao：按机场过滤
      - source：按数据源过滤（weathergov / awc）
      - limit：最多返回条数
    """
    # 解析时间窗口
    now = datetime.now(timezone.utc)
    if hours is not None:
        if start is not None or end is not None:
            raise HTTPException(
                status_code=422,
                detail="hours 与 start/end 不能同时使用",
            )
        start_dt = now - timedelta(hours=hours)
        end_dt = now
    else:
        if start is None:
            start_dt = now - timedelta(hours=24)
        else:
            start_dt = parse_iso(start)
            if start_dt is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"无法解析 start 时间: {start}",
                )
        end_dt = parse_iso(end) if end else now
        if end_dt is None:
            raise HTTPException(
                status_code=422,
                detail=f"无法解析 end 时间: {end}",
            )

    if end_dt < start_dt:
        raise HTTPException(
            status_code=422,
            detail="end 必须晚于 start",
        )

    events = await get_correction_events(
        redis_client,
        start_dt=start_dt,
        end_dt=end_dt,
        icao=icao.upper() if icao else None,
        source=source,
        limit=limit,
    )

    return JSONResponse(
        content={
            "count": len(events),
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "events": events,
        }
    )


@app.get("/health")
async def health_check():
    """健康检查接口."""
    return {"status": "ok"}


# 导入 asyncio 用于 lifespan（必须在模块末尾或开头，避免循环）
import asyncio  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402


def parse_temperature(raw_text: str) -> tuple[Optional[float], Optional[float]]:
    """从 METAR raw_text 解析温度与露点（摄氏度）.

    解析优先级：
      1. RMK 中的 T 组精确温度（T01330100），按符号+4位解析，除以 10
      2. METAR 主体中的 TT/DD（25/18），负数用 M 前缀

    返回 (temperature_c, dewpoint_c)；解析失败返回 (None, None)。
    """
    if not raw_text:
        return None, None

    # 1. 优先解析 RMK 中的精确 T 组：T 后跟 8 位数字（气温4位+露点4位）
    #    每位第一位是符号位：1 表示负，0 表示正
    rmk_match = re.search(r"\bT(0|1)(\d{3})(0|1)(\d{3})\b", raw_text)
    if rmk_match:
        temp_sign = -1 if rmk_match.group(1) == "1" else 1
        temp_val = int(rmk_match.group(2))
        dew_sign = -1 if rmk_match.group(3) == "1" else 1
        dew_val = int(rmk_match.group(4))
        return (
            temp_sign * temp_val / 10.0,
            dew_sign * dew_val / 10.0,
        )

    # 2. 退回到 METAR 主体中的 TT/DD
    match = re.search(r"\s([M]?\d{2})/([M]?\d{2})\s", raw_text)
    if not match:
        return None, None

    def _to_celsius(value: str) -> float:
        if value.startswith("M"):
            return -float(value[1:])
        return float(value)

    return _to_celsius(match.group(1)), _to_celsius(match.group(2))


class BatchMetarRequest(BaseModel):
    """批量请求体."""

    icaos: list[str]


async def _event_stream(
    icaos: Optional[set[str]],
    redis_client: Any,
    heartbeat_interval: float = 10.0,
) -> Any:
    """SSE 事件流生成器.

    - 连接建立时发送当前所有监控机场的 snapshot
    - 之后监听采集器发布的事件, 有过滤条件时只推送匹配的机场
    - 定期发送 heartbeat 注释保持连接（默认 10s，可调）
    - 内部异常会被捕获并记录, 避免连接异常断开而不留日志
    """
    queue = subscribe()
    try:
        # 发送初始 snapshot
        snapshot: list[dict[str, Any]] = []
        try:
            for code in sorted(icaos or set()):
                data = await get_metar(redis_client, code)
                if data is None:
                    continue
                temp, dewpoint = parse_temperature(data.get("raw_text", ""))
                snapshot.append(
                    {
                        "icao": code,
                        "temperature_c": temp,
                        "dewpoint_c": dewpoint,
                        "raw_text": data.get("raw_text"),
                        "observed_at": data.get("observed_at"),
                        "updated_at": data.get("updated_at"),
                        "source": data.get("source"),
                        "source_key": data.get("source_key"),
                        "hash": data.get("hash"),
                    }
                )
        except Exception as exc:
            logger.error("Failed to build SSE snapshot: %s", exc)
            snapshot = []

        yield f"event: snapshot\ndata: {json.dumps({'event_type': 'snapshot', 'count': len(snapshot), 'data': snapshot})}\n\n"

        while True:
            try:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=heartbeat_interval
                    )
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue

                if icaos and event.get("icao", "").upper() not in icaos:
                    continue

                # 解析温度, 方便消费者直接拿到温度
                temp, dewpoint = parse_temperature(event.get("raw_text", ""))
                event["temperature_c"] = temp
                event["dewpoint_c"] = dewpoint

                yield f"event: {event['event_type']}\ndata: {json.dumps(event)}\n\n"
            except Exception as exc:
                logger.error("SSE event loop error: %s", exc)
                # 发送一个 error 事件后优雅关闭, 让客户端有机会重连
                try:
                    yield f"event: error\ndata: {json.dumps({'event_type': 'error', 'message': str(exc)})}\n\n"
                except Exception:
                    pass
                break
    finally:
        unsubscribe(queue)


@app.get("/api/v1/metar/stream")
async def metar_event_stream(
    icaos: Optional[str] = Query(
        None,
        description="逗号分隔的 ICAO 机场代码, 只推送这些机场的事件; 为空则推送全部",
    ),
    heartbeat: float = Query(
        10.0,
        ge=1.0,
        le=60.0,
        description="心跳间隔秒数(1~60, 默认 10), 用于兼容不同代理/NAT 超时",
    ),
    settings: Settings = Depends(get_settings),
    redis_client: Any = Depends(_get_redis_dependency),
):
    """SSE 实时流：推送 METAR 新数据事件.

    事件类型:
      - snapshot: 连接建立时的当前状态快照
      - source_update: 某个数据源有新 METAR 数据
      - winner_update: M2 择优后的最终 METAR 发生变化
      - correction: 官方修正事件(同一 observed_at 出现不同 hash)

    参数:
      - icaos: 逗号分隔的 ICAO 机场代码
      - heartbeat: 心跳间隔秒数(1~60, 默认 10)

    示例:
      curl -N "http://localhost:8000/api/v1/metar/stream?icaos=KSEA,KJFK&heartbeat=5"
    """
    monitored = {code.upper() for code in settings.monitor_airports_list}

    requested: Optional[set[str]] = None
    if icaos:
        requested = {code.strip().upper() for code in icaos.split(",") if code.strip()}
        invalid = requested - monitored
        if invalid:
            raise HTTPException(
                status_code=404,
                detail=f"ICAO codes not monitored: {', '.join(sorted(invalid))}",
            )

    return StreamingResponse(
        _event_stream(requested, redis_client, heartbeat_interval=heartbeat),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
        },
    )


@app.post("/api/v1/metar/batch")
async def get_metar_batch(
    request: BatchMetarRequest,
    settings: Settings = Depends(get_settings),
    redis_client: Any = Depends(_get_redis_dependency),
):
    """批量获取多个机场的 METAR 与温度信息.

    - 只返回 Redis 中已存在且能解析出温度的机场
    - `missing` 字段列出无数据或温度解析失败的机场
    """
    monitored = {code.upper() for code in settings.monitor_airports_list}
    data: list[dict[str, Any]] = []
    missing: list[str] = []

    for icao in request.icaos:
        code = icao.upper()
        if code not in monitored:
            missing.append(code)
            continue

        metar = await get_metar(redis_client, code)
        if metar is None:
            missing.append(code)
            continue

        temp, dewpoint = parse_temperature(metar.get("raw_text", ""))
        if temp is None:
            missing.append(code)
            continue

        data.append(
            {
                "icao": code,
                "temperature_c": temp,
                "dewpoint_c": dewpoint,
                "raw_text": metar["raw_text"],
                "observed_at": metar.get("observed_at"),
                "updated_at": metar.get("updated_at"),
                "source": metar.get("source"),
            }
        )

    return JSONResponse(
        content={
            "data": data,
            "missing": missing,
            "count": len(data),
        }
    )
