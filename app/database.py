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
        return False

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

    # 检查同 observed_at 是否已有不同 hash 的记录（用于官方修正检测）
    existing_members = await redis_client.zrangebyscore(key, score, score, withscores=False)
    existing_hashes = {
        _parse_history_member(m).get("hash")
        for m in existing_members
        if _parse_history_member(m) is not None
    }
    is_new_hash = record_hash not in existing_hashes

    pipe = redis_client.pipeline()
    if is_new_hash:
        # 新 hash：直接追加
        pipe.zadd(key, {member: score})
    else:
        # 已存在则更新 member（让 updated_at 保持最新，用于延迟分析）
        pipe.zremrangebyscore(key, score, score)
        pipe.zadd(key, {member: score})
    # 清理超过 retention 的旧记录
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    pipe.zremrangebyscore(key, "-inf", _dt_to_score(cutoff))
    # 给整个 key 设置 TTL，防止冷机场从监控列表移除后残留
    pipe.expire(key, (retention_days + 1) * 86400)
    await pipe.execute()

    return is_new_hash


def _correction_event_key(icao: str, source: str, observed_at: datetime) -> str:
    """生成官方修正事件的 Hash Key."""
    ts = int(_dt_to_score(observed_at))
    return f"events:metar:correction:{icao.upper()}:{source}:{ts}"


async def record_correction_event(
    redis_client: redis.Redis,
    icao: str,
    source: str,
    observed_at: datetime,
    first_record: dict[str, Any],
    corrected_record: dict[str, Any],
    retention_days: int = 360,
) -> None:
    """记录一次 METAR 官方修正事件.

    触发条件：同一个数据源、同一个机场、同一个 observed_at，
    后续收到了与之前不同 hash 的官方 METAR/SPECI 报文。

    每次新的修正都会生成一条独立的 event 记录，保留完整原始报文。
    """
    icao = icao.upper()
    detected_at = datetime.now(timezone.utc)

    first_updated = parse_iso(first_record.get("updated_at")) or detected_at
    corrected_updated = parse_iso(corrected_record.get("updated_at")) or detected_at
    correction_delay = (corrected_updated - first_updated).total_seconds()

    event = {
        "icao": icao,
        "source": first_record.get("source", "unknown"),
        "source_key": source,
        "observed_at": observed_at.isoformat(),
        "first_updated_at": first_updated.isoformat(),
        "first_hash": first_record.get("hash", ""),
        "first_raw_text": first_record.get("raw_text", ""),
        "corrected_updated_at": corrected_updated.isoformat(),
        "corrected_hash": corrected_record.get("hash", ""),
        "corrected_raw_text": corrected_record.get("raw_text", ""),
        "detected_at": detected_at.isoformat(),
        "correction_delay_seconds": correction_delay,
    }

    event_key = _correction_event_key(icao, source, observed_at)
    index_key = "events:metar:correction:index"

    # 为同一个 observed_at 的多次修正生成唯一 member：追加 corrected_hash
    index_member = f"{event_key}:{corrected_record.get('hash', '')}"

    pipe = redis_client.pipeline()
    # 用 List 追加同一 observed_at 下的所有修正事件，保留完整历史
    pipe.rpush(event_key, json.dumps(event, ensure_ascii=False, sort_keys=True))
    pipe.expire(event_key, retention_days * 86400)
    # 全局时间索引
    pipe.zadd(index_key, {index_member: _dt_to_score(detected_at)})
    pipe.expire(index_key, retention_days * 86400)
    await pipe.execute()

    logger.warning(
        "METAR correction event detected: %s/%s at %s delay=%.1fs "
        "first_hash=%s corrected_hash=%s",
        icao,
        source,
        observed_at.isoformat(),
        correction_delay,
        first_record.get("hash", "")[:8],
        corrected_record.get("hash", "")[:8],
    )


async def get_correction_events(
    redis_client: redis.Redis,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    icao: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """查询 METAR 官方修正事件.

    支持按时间窗口、机场、数据源过滤。返回按 detected_at 降序排列的事件列表。
    """
    index_key = "events:metar:correction:index"
    min_score = _dt_to_score(start_dt) if start_dt else "-inf"
    max_score = _dt_to_score(end_dt) if end_dt else "+inf"

    members = await redis_client.zrevrangebyscore(
        index_key, max_score, min_score, start=0, num=limit * 2
    )

    prefix_filter = None
    if icao and source:
        prefix_filter = f"events:metar:correction:{icao.upper()}:{source}:"
    elif icao:
        prefix_filter = f"events:metar:correction:{icao.upper()}:"

    events: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for member in members:
        # member 格式: events:metar:correction:{icao}:{source}:{ts}:{hash}
        parts = member.rsplit(":", 1)
        event_key = parts[0]

        if prefix_filter and not event_key.startswith(prefix_filter):
            continue
        if event_key in seen_keys:
            continue
        seen_keys.add(event_key)

        raw_events = await redis_client.lrange(event_key, 0, -1)
        for raw in raw_events:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Corrupted correction event: %s", raw[:80])
                continue
            # 二次过滤
            if icao and event.get("icao", "").upper() != icao.upper():
                continue
            if source and event.get("source_key", "") != source:
                continue
            events.append(event)

        if len(events) >= limit:
            break

    # 按 detected_at 降序，截断到 limit
    events.sort(key=lambda x: x.get("detected_at", ""), reverse=True)
    return events[:limit]


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
