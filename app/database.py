"""Module B: Redis 异步客户端连接池."""

from __future__ import annotations

import json
import logging
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
    """从 Redis 读取单个机场的 METAR 数据."""
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
