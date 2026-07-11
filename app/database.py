"""Module B: Redis 异步客户端连接池."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import redis.asyncio as redis

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# 全局 Redis 连接池，应用生命周期内复用
_redis_pool: Optional[redis.Redis] = None


async def get_redis(settings: Optional[Settings] = None) -> redis.Redis:
    """获取或创建 Redis 异步连接池.

    首次调用时根据 settings.redis_url 创建连接；后续调用复用同一连接。
    """
    global _redis_pool
    if _redis_pool is None:
        cfg = settings or get_settings()
        _redis_pool = redis.Redis.from_url(
            str(cfg.redis_url),
            decode_responses=True,
            socket_connect_timeout=5,
            socket_keepalive=True,
        )
        logger.info("Redis connection pool initialized: %s", cfg.redis_url)
    return _redis_pool


async def close_redis() -> None:
    """关闭 Redis 连接池，通常在应用 shutdown 时调用."""
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.aclose()
        _redis_pool = None
        logger.info("Redis connection pool closed")


async def get_metar(redis_client: redis.Redis, icao: str) -> Optional[dict[str, Any]]:
    """从 Redis 读取单个机场的 METAR 数据（已择优后的最终记录）."""
    raw = await redis_client.get(f"metar:{icao.upper()}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Corrupted METAR data in Redis for %s", icao)
        return None


async def set_metar(
    redis_client: redis.Redis,
    icao: str,
    data: dict[str, Any],
    ttl_seconds: int,
) -> None:
    """写入 METAR 数据并设置 TTL."""
    await redis_client.set(
        f"metar:{icao.upper()}",
        json.dumps(data, ensure_ascii=False, sort_keys=True),
        ex=ttl_seconds,
    )


async def get_existing_hash(redis_client: redis.Redis, icao: str) -> Optional[str]:
    """读取某机场当前存储的 hash，用于去重."""
    data = await get_metar(redis_client, icao)
    if data is None:
        return None
    return data.get("hash")


async def get_source_metar(
    redis_client: redis.Redis,
    icao: str,
    source: str,
) -> Optional[dict[str, Any]]:
    """读取指定数据源的最新 METAR 记录.

    source 取值:
      - "weathergov" -> weather.gov / SynopticData
      - "awc" -> aviationweather.gov
    """
    raw = await redis_client.get(f"metar:{icao.upper()}:source:{source}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Corrupted source METAR data in Redis for %s/%s", icao, source)
        return None


async def set_source_metar(
    redis_client: redis.Redis,
    icao: str,
    source: str,
    data: dict[str, Any],
    ttl_seconds: int,
) -> None:
    """写入指定数据源的 METAR 记录并设置 TTL."""
    await redis_client.set(
        f"metar:{icao.upper()}:source:{source}",
        json.dumps(data, ensure_ascii=False, sort_keys=True),
        ex=ttl_seconds,
    )


async def get_existing_source_hash(
    redis_client: redis.Redis,
    icao: str,
    source: str,
) -> Optional[str]:
    """读取某机场指定数据源当前存储的 hash，用于去重."""
    data = await get_source_metar(redis_client, icao, source)
    if data is None:
        return None
    return data.get("hash")


def _history_key(icao: str, source: str) -> str:
    """生成数据源历史记录的 Redis Sorted Set Key."""
    return f"history:metar:{icao.upper()}:source:{source}"


def _dt_to_score(dt: datetime) -> float:
    """将 datetime 转为 Redis Sorted Set 的 score（秒级时间戳）."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _score_to_dt(score: float) -> datetime:
    """将 Redis Sorted Set 的 score 转回 UTC datetime."""
    return datetime.fromtimestamp(score, tz=timezone.utc)


def _parse_history_member(member: str) -> Optional[dict[str, Any]]:
    """解析历史记录 Sorted Set 中的 JSON member."""
    try:
        return json.loads(member)
    except json.JSONDecodeError:
        logger.warning("Corrupted history member: %s", member[:80])
        return None


async def add_source_history(
    redis_client: redis.Redis,
    icao: str,
    source: str,
    data: dict[str, Any],
    retention_days: int = 7,
) -> None:
    """将一条数据源 METAR 记录追加到历史 Sorted Set.

    使用 observed_at 的 UTC 时间戳作为 score，便于按时间窗口查询。
    写入后会自动清理超过 retention_days 的旧记录。
    整个 Sorted Set 设置 TTL 为 retention_days + 1 天，防止冷机场无限制增长。
    """
    icao = icao.upper()
    observed_at = data.get("observed_at")
    if not observed_at:
        logger.warning("Cannot add history for %s/%s without observed_at", icao, source)
        return

    try:
        obs_dt = parse_iso(observed_at)
    except (ValueError, TypeError):
        logger.warning("Invalid observed_at for history %s/%s: %s", icao, source, observed_at)
        return

    if obs_dt is None:
        return

    score = _dt_to_score(obs_dt)
    key = _history_key(icao, source)

    # member 使用 hash 作为唯一标识，payload 携带完整信息
    record_hash = data.get("hash") or hashlib.sha1(
        data.get("raw_text", "").encode("utf-8")
    ).hexdigest()

    history_record = {
        "icao": icao,
        "source": data.get("source", "unknown"),
        "source_key": source,
        "observed_at": obs_dt.isoformat(),
        "updated_at": data.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        "raw_text": data.get("raw_text", ""),
        "hash": record_hash,
    }

    member = json.dumps(history_record, ensure_ascii=False, sort_keys=True)

    pipe = redis_client.pipeline()
    # 添加或更新该 hash 对应的历史记录（score 以 observed_at 为准）
    pipe.zadd(key, {member: score}, nx=False, gt=False)
    # 清理超过 retention 的旧记录
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    pipe.zremrangebyscore(key, "-inf", _dt_to_score(cutoff))
    # 给整个 key 设置 TTL，防止机场从监控列表移除后残留
    pipe.expire(key, (retention_days + 1) * 86400)
    await pipe.execute()


async def get_source_history(
    redis_client: redis.Redis,
    icao: str,
    source: str,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """查询某机场指定数据源在指定时间窗口内的历史记录.

    返回按 observed_at 升序排列的记录列表。
    """
    key = _history_key(icao, source)
    min_score = _dt_to_score(start_dt) if start_dt else "-inf"
    max_score = _dt_to_score(end_dt) if end_dt else "+inf"

    members = await redis_client.zrangebyscore(key, min_score, max_score, withscores=False)
    records: list[dict[str, Any]] = []
    for member in members:
        parsed = _parse_history_member(member)
        if parsed is not None:
            records.append(parsed)
    return records


def parse_iso(value: Any) -> Optional[datetime]:
    """解析 ISO 8601 时间字符串为 UTC datetime.

    同时兼容 database.py 内部调用与 collector.py 中的同名函数。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
