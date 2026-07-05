"""Module A: 异步高频 METAR 采集器."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from app.config import Settings, get_settings
from app.database import get_existing_hash, get_redis, set_metar

logger = logging.getLogger(__name__)

# 全局 HTTP 客户端，应用生命周期内复用，启用 Keep-Alive
_http_client: Optional[httpx.AsyncClient] = None

# SynopticData Token 缓存
_weathergov_token: Optional[str] = None
_weathergov_token_expires: Optional[datetime] = None
_token_lock = asyncio.Lock()

# API 常量
AWC_BASE_URL = "https://aviationweather.gov/api/data/metar"
WEATHERGOV_API_URL = "https://api.synopticdata.com/v2/stations/timeseries"
WEATHERGOV_TOKEN_URL = "https://www.weather.gov/source/wrh/apiKey.js"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _get_http_client(settings: Optional[Settings] = None) -> httpx.AsyncClient:
    """获取或创建带 Keep-Alive 的 AsyncClient."""
    global _http_client
    if _http_client is None:
        cfg = settings or get_settings()
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.http_timeout),
            limits=limits,
            headers={
                "User-Agent": cfg.user_agent,
                "Accept": "application/json",
                "Connection": "keep-alive",
            },
            http2=False,  # 上游 API 不一定支持 HTTP/2，保持 HTTP/1.1 Keep-Alive 更稳
        )
    return _http_client


async def close_http_client() -> None:
    """关闭全局 HTTP 客户端."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
        logger.info("HTTP client closed")


async def _get_weathergov_token(settings: Optional[Settings] = None) -> Optional[str]:
    """获取 SynopticData token.

    优先级:
      1. WEATHERGOV_TOKEN 环境变量（稳定，推荐生产使用）
      2. weather.gov 页面内嵌 token（可能被撤销/限 referer）
    """
    cfg = settings or get_settings()
    if cfg.weathergov_token:
        return cfg.weathergov_token

    global _weathergov_token, _weathergov_token_expires
    async with _token_lock:
        now = _now_utc()
        if _weathergov_token and _weathergov_token_expires and now < _weathergov_token_expires:
            return _weathergov_token

        client = _get_http_client(cfg)
        try:
            # 请求 token 页面时不带 Referer，避免被限制；设置独立超时防止 hang 住
            resp = await client.get(WEATHERGOV_TOKEN_URL, timeout=httpx.Timeout(10.0, connect=5.0))
            resp.raise_for_status()
            match = re.search(r"mesoToken\s*=\s*['\"]([A-Fa-f0-9]+)['\"]", resp.text)
            if match:
                _weathergov_token = match.group(1)
                _weathergov_token_expires = now + timedelta(hours=1)
                logger.info("Fetched embedded weather.gov token, expires in 1 hour")
                return _weathergov_token
            logger.warning("Could not parse embedded weather.gov token")
        except httpx.HTTPError as exc:
            logger.error("Failed to fetch weather.gov token: %s", exc)
        return None


def _parse_iso_time(value: str) -> Optional[datetime]:
    """解析 ISO 8601 时间字符串为 UTC datetime."""
    if not value:
        return None
    try:
        # 处理 "2026-07-05T04:55:00Z"
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_metar_time(raw_metar: str, base_time: datetime) -> Optional[datetime]:
    """从 METAR 文本的 DDHHMMZ 字段解析观测时间."""
    if not raw_metar:
        return None
    match = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", raw_metar)
    if not match:
        return None
    day, hour, minute = map(int, match.groups())
    dt = datetime(base_time.year, base_time.month, day, hour, minute, tzinfo=timezone.utc)
    # 处理跨天/跨月边界
    diff = (dt - base_time).total_seconds()
    if diff > 43200:
        dt -= timedelta(days=1)
    elif diff < -43200:
        dt += timedelta(days=1)
    return dt


def _is_metar_origin(value: Any) -> bool:
    """判断 SynopticData 是否标记为真正的 METAR/SPECI 来源."""
    if value is None:
        return False
    try:
        return float(value) == 1.0
    except (ValueError, TypeError):
        return False


def _extract_weathergov_metars(
    data: dict[str, Any],
    requested_codes: set[str],
) -> dict[str, dict[str, Any]]:
    """从 SynopticData 响应中提取每个请求机场的最新真实 METAR."""
    results: dict[str, dict[str, Any]] = {}
    stations = data.get("STATION") or []
    if isinstance(stations, dict):
        stations = [stations]

    for station in stations:
        code = station.get("STID")
        if not code or code not in requested_codes or code in results:
            continue

        obs = station.get("OBSERVATIONS") or {}
        times = obs.get("date_time", [])
        metars = obs.get("metar_set_1", [])
        origins = obs.get("metar_origin_set_1", [])

        if not times or not metars:
            continue

        # 从后往前扫描，取最新一条真正 METAR
        for idx in range(len(times) - 1, -1, -1):
            if idx >= len(metars) or not metars[idx]:
                continue
            origin = origins[idx] if idx < len(origins) else None
            if not _is_metar_origin(origin):
                continue

            raw_metar = str(metars[idx]).strip()
            if not raw_metar:
                continue

            obs_time = _parse_iso_time(times[idx]) or _parse_metar_time(raw_metar, _now_utc())
            if obs_time is None:
                continue

            results[code] = {
                "icao": code,
                "raw_text": raw_metar,
                "observed_at": obs_time.isoformat(),
                "source": "weather.gov",
            }
            break

    return results


async def _fetch_weathergov_batch(
    codes: list[str],
    settings: Optional[Settings] = None,
) -> dict[str, dict[str, Any]]:
    """批量从 weather.gov / SynopticData 获取 METAR."""
    if not codes:
        return {}

    cfg = settings or get_settings()
    token = await _get_weathergov_token(cfg)
    if not token:
        logger.warning("No weather.gov token available, skipping batch fetch")
        return {}

    client = _get_http_client(cfg)
    requested_codes = {code.upper() for code in codes}
    stid = ",".join(sorted(requested_codes))

    params = {
        "STID": stid,
        "showemptystations": "1",
        "units": "metric",
        "recent": "60",
        "complete": "1",
        "obtimezone": "utc",
        "token": token,
    }
    headers = {
        # 带上 Referer 有助于内嵌 token 通过校验
        "Referer": f"https://www.weather.gov/wrh/timeseries?site={list(requested_codes)[0]}",
        "Origin": "https://www.weather.gov",
    }

    try:
        resp = await client.get(
            WEATHERGOV_API_URL,
            params=params,
            headers=headers,
            timeout=httpx.Timeout(cfg.http_timeout, connect=5.0),
        )
        if resp.status_code == 403 and "token" in resp.text.lower():
            logger.warning("weather.gov token rejected, forcing refresh")
            global _weathergov_token, _weathergov_token_expires
            async with _token_lock:
                _weathergov_token = None
                _weathergov_token_expires = None
            return {}
        if resp.status_code == 429:
            logger.warning("weather.gov rate limited (429)")
            return {}
        resp.raise_for_status()
        data = resp.json()
        return _extract_weathergov_metars(data, requested_codes)
    except httpx.HTTPError as exc:
        logger.error("weather.gov batch fetch error: %s", exc)
    except json.JSONDecodeError as exc:
        logger.error("weather.gov invalid JSON: %s", exc)
    return {}


async def _fetch_awc_single(
    code: str,
    settings: Optional[Settings] = None,
) -> Optional[dict[str, Any]]:
    """从 AviationWeather.gov 获取单个机场的 METAR."""
    cfg = settings or get_settings()
    client = _get_http_client(cfg)

    params = {
        "ids": code.upper(),
        "format": "json",
        "hours": "1",
    }
    try:
        resp = await client.get(
            AWC_BASE_URL,
            params=params,
            timeout=httpx.Timeout(cfg.http_timeout, connect=5.0),
        )
        if resp.status_code == 429:
            logger.warning("AviationWeather rate limited (429) for %s", code)
            return None
        resp.raise_for_status()
        data = resp.json()
        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        if not items:
            return None

        # 取最新一条
        item = items[0]
        raw_metar = item.get("rawOb")
        if not raw_metar:
            return None

        obs_time = None
        report_time = item.get("reportTime") or item.get("obsTime")
        if isinstance(report_time, (int, float)):
            obs_time = datetime.fromtimestamp(report_time, tz=timezone.utc)
        elif isinstance(report_time, str):
            obs_time = _parse_iso_time(report_time)
        if obs_time is None:
            obs_time = _parse_metar_time(raw_metar, _now_utc())
        if obs_time is None:
            obs_time = _now_utc()

        return {
            "icao": code.upper(),
            "raw_text": str(raw_metar).strip(),
            "observed_at": obs_time.isoformat(),
            "source": "aviationweather.gov",
        }
    except httpx.HTTPError as exc:
        logger.error("AviationWeather fetch error for %s: %s", code, exc)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("AviationWeather invalid JSON for %s: %s", code, exc)
    return None


async def _fetch_airport(
    code: str,
    weathergov_batch: dict[str, dict[str, Any]],
    settings: Optional[Settings] = None,
) -> Optional[dict[str, Any]]:
    """获取单个机场的 METAR：优先使用 weather.gov 批量结果，否则回退 AWC."""
    code = code.upper()
    if code in weathergov_batch:
        return weathergov_batch[code]

    # weather.gov 批量中没有该机场，尝试 AWC
    return await _fetch_awc_single(code, settings)


def _compute_hash(raw_text: str) -> str:
    """计算 METAR 文本的 SHA1 哈希."""
    return hashlib.sha1(raw_text.encode("utf-8")).hexdigest()


async def _store_if_changed(
    redis_client: Any,
    icao: str,
    metar_data: dict[str, Any],
    settings: Optional[Settings] = None,
) -> bool:
    """如果 METAR 文本有变化，则写入 Redis 并返回 True."""
    cfg = settings or get_settings()
    icao = icao.upper()
    raw_text = metar_data["raw_text"]
    new_hash = _compute_hash(raw_text)

    existing_hash = await get_existing_hash(redis_client, icao)
    if existing_hash == new_hash:
        logger.debug("METAR unchanged for %s, skipping write", icao)
        return False

    payload = {
        "icao": icao,
        "raw_text": raw_text,
        "observed_at": metar_data.get("observed_at") or _now_utc().isoformat(),
        "updated_at": _now_utc().isoformat(),
        "hash": new_hash,
        "source": metar_data.get("source", "unknown"),
    }
    await set_metar(redis_client, icao, payload, cfg.metar_ttl_seconds)
    logger.info("METAR updated for %s (hash=%s... source=%s)", icao, new_hash[:8], payload["source"])
    return True


async def _poll_cycle(settings: Optional[Settings] = None) -> None:
    """执行一轮采集：先批量 weather.gov，再为缺失机场回退 AWC."""
    cfg = settings or get_settings()
    redis_client = await get_redis(cfg)

    # 1. 批量从 weather.gov 获取所有监控机场
    weathergov_results = await _fetch_weathergov_batch(cfg.monitor_airports_list, cfg)

    # 2. 对每个机场并发回退 AWC
    tasks = [
        _fetch_airport(code, weathergov_results, cfg)
        for code in cfg.monitor_airports_list
    ]
    airport_results = await asyncio.gather(*tasks, return_exceptions=True)

    # 3. 写入 Redis（去重）
    for code, result in zip(cfg.monitor_airports_list, airport_results):
        if isinstance(result, Exception):
            logger.error("Unexpected exception fetching %s: %s", code, result)
            continue
        if result is None:
            logger.warning("No METAR data available for %s", code)
            continue
        try:
            await _store_if_changed(redis_client, code, result, cfg)
        except Exception as exc:
            logger.error("Failed to store METAR for %s: %s", code, exc)


async def start_collector_loop(settings: Optional[Settings] = None) -> None:
    """启动后台采集循环，永不退出.

    外层 try-except 捕获所有异常，保证网络抖动、500、429、Timeout 等
    都不会导致循环崩溃。
    """
    cfg = settings or get_settings()
    logger.info(
        "Starting METAR collector loop for %d airports (interval=%.1fs)",
        len(cfg.monitor_airports_list),
        cfg.poll_interval_seconds,
    )

    while True:
        try:
            await _poll_cycle(cfg)
        except Exception as exc:
            logger.error("Collector cycle crashed, recovering: %s", exc)

        await asyncio.sleep(cfg.poll_interval_seconds)
