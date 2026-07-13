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
        if (
            _weathergov_token
            and _weathergov_token_expires
            and now < _weathergov_token_expires
        ):
            return _weathergov_token

        client = _get_http_client(cfg)
        try:
            # 请求 token 页面时不带 Referer，避免被限制；设置独立超时防止 hang 住
            resp = await client.get(
                WEATHERGOV_TOKEN_URL, timeout=httpx.Timeout(10.0, connect=5.0)
            )
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
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
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
    dt = datetime(
        base_time.year, base_time.month, day, hour, minute, tzinfo=timezone.utc
    )
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

        # 从后往前扫描，取最新一条真正 METAR/SPECI；
        # 优先选择含 RMK+T 精确温度组的，没有则保留普通 METAR/SPECI。
        selected_idx: Optional[int] = None
        fallback_idx: Optional[int] = None
        for idx in range(len(times) - 1, -1, -1):
            if idx >= len(metars) or not metars[idx]:
                continue
            origin = origins[idx] if idx < len(origins) else None
            if not _is_metar_origin(origin):
                continue

            raw_metar = str(metars[idx]).strip()
            if not raw_metar:
                continue

            if _has_precision_temp(raw_metar):
                # 找到带精确温度组的，立即采用
                selected_idx = idx
                break

            if fallback_idx is None:
                fallback_idx = idx
        else:
            # for 循环没有 break，使用 fallback
            selected_idx = fallback_idx

        if selected_idx is None:
            continue

        raw_metar = str(metars[selected_idx]).strip()

        # 统一从 rawOb 中的 ddHHMMZ 解析真实 METAR 时间
        obs_time = _parse_metar_time(raw_metar, _now_utc())
        if obs_time is None:
            logger.warning(
                "Could not parse METAR time from rawOb for %s: %s", code, raw_metar[:80]
            )
            continue

        results[code] = {
            "icao": code,
            "raw_text": raw_metar,
            "observed_at": obs_time.isoformat(),
            "source": "weather.gov",
        }

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


def _has_precision_temp(raw_text: str) -> bool:
    """判断报文是否包含 RMK 精确温度组 Txxxx/Txxxxxxxx."""
    return bool(raw_text and re.search(r"\bT[01]\d{3}[01]\d{3}\b", raw_text))


# 禁用 AviationWeather.gov 的机场列表（某些机场 AWC 数据质量不佳或不可用）
AWC_DISABLED_AIRPORTS = {"UUWW"}


async def _fetch_awc_batch(
    codes: list[str],
    settings: Optional[Settings] = None,
) -> dict[str, dict[str, Any]]:
    """从 AviationWeather.gov 批量获取多个机场的 METAR.

    只接收 METAR/SPECI 报文；AUTO 报直接跳过。
    优先选择含 RMK+T 精确温度组的报文，没有则保留普通 METAR/SPECI。
    """
    cfg = settings or get_settings()
    client = _get_http_client(cfg)

    requested_codes = {
        code.upper() for code in codes if code.upper() not in AWC_DISABLED_AIRPORTS
    }
    if not requested_codes:
        return {}

    params = {
        "ids": ",".join(sorted(requested_codes)),
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
            logger.warning("AviationWeather rate limited (429) for batch")
            return {}
        resp.raise_for_status()
        data = resp.json()
        items = (
            data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        )

        results: dict[str, dict[str, Any]] = {}
        for item in items:
            raw_metar = item.get("rawOb")
            icao = item.get("icaoId", "").upper()
            if not raw_metar or icao not in requested_codes:
                continue

            metar_type = item.get("metarType", "")
            if metar_type not in {"METAR", "SPECI"}:
                continue

            # 同一机场可能返回多条记录，优先保留带 RMK+T 的
            existing = results.get(icao)
            if existing and _has_precision_temp(existing["raw_text"]):
                continue

            if _has_precision_temp(raw_metar):
                selected = item
                fallback = None
            elif existing is None:
                selected = None
                fallback = item
            else:
                continue

            chosen = selected or fallback
            if chosen is None:
                continue

            chosen_raw = chosen["rawOb"]

            # 统一从 rawOb 中的 ddHHMMZ 解析真实 METAR 时间
            obs_time = _parse_metar_time(chosen_raw, _now_utc())
            if obs_time is None:
                logger.warning(
                    "Could not parse METAR time from rawOb for %s: %s",
                    icao,
                    chosen_raw[:80],
                )
                continue

            results[icao] = {
                "icao": icao,
                "raw_text": str(chosen_raw).strip(),
                "observed_at": obs_time.isoformat(),
                "source": "aviationweather.gov",
            }

        return results
    except httpx.HTTPError as exc:
        logger.error("AviationWeather batch fetch error: %s", exc)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("AviationWeather invalid JSON: %s", exc)
    return {}


async def _fetch_airport(
    code: str,
    weathergov_batch: dict[str, dict[str, Any]],
    awc_batch: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """获取单个机场的 METAR：优先 weather.gov，其次 AWC."""
    code = code.upper()
    if code in weathergov_batch:
        return weathergov_batch[code]
    return awc_batch.get(code)


def _compute_hash(raw_text: str) -> str:
    """计算 METAR 文本的 SHA1 哈希."""
    return hashlib.sha1(raw_text.encode("utf-8")).hexdigest()


def _parse_observed_at(value: Any) -> Optional[datetime]:
    """将 observed_at 字符串解析为 UTC datetime."""
    if not value:
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


def _select_winner(
    weathergov_record: Optional[dict[str, Any]],
    awc_record: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """从两个数据源记录中选择优胜者.

    选择规则:
      1. 优先比较 observed_at：时间更晚（更新）的记录胜出
      2. 若 observed_at 相同，比较入库延迟（updated_at - observed_at），延迟更小的胜出
      3. 若仍相同，默认优先 weather.gov
      4. 只有一方有数据时，直接使用该方
    """
    if weathergov_record is None:
        return awc_record
    if awc_record is None:
        return weathergov_record

    wg_obs = _parse_observed_at(weathergov_record.get("observed_at"))
    awc_obs = _parse_observed_at(awc_record.get("observed_at"))

    if wg_obs is None and awc_obs is None:
        return weathergov_record
    if wg_obs is None:
        return awc_record
    if awc_obs is None:
        return weathergov_record

    # 规则 1：更新鲜的 METAR 胜出
    if wg_obs > awc_obs:
        return weathergov_record
    if awc_obs > wg_obs:
        return awc_record

    # observed_at 相同，比较延迟
    wg_updated = _parse_observed_at(weathergov_record.get("updated_at"))
    awc_updated = _parse_observed_at(awc_record.get("updated_at"))

    wg_latency = (wg_updated - wg_obs).total_seconds() if wg_updated else float("inf")
    awc_latency = (
        (awc_updated - awc_obs).total_seconds() if awc_updated else float("inf")
    )

    if awc_latency < wg_latency:
        return awc_record
    return weathergov_record


async def _store_source_if_changed(
    redis_client: Any,
    icao: str,
    source: str,
    metar_data: dict[str, Any],
    settings: Optional[Settings] = None,
) -> bool:
    """如果某个数据源的 METAR 文本有变化，则写入该数据源专属 Key 并返回 True.

    同时检测是否存在同一 observed_at 的官方 METAR 修正事件：
    如果同数据源同机场同一 METAR 时间，后续出现了不同 hash 的报文，
    则记录为一次 correction event。
    """
    cfg = settings or get_settings()
    icao = icao.upper()
    raw_text = metar_data["raw_text"]
    new_hash = _compute_hash(raw_text)

    from app.database import (
        add_source_history,
        get_existing_source_hash,
        get_source_history,
        record_correction_event,
        set_source_metar,
    )

    existing_hash = await get_existing_source_hash(redis_client, icao, source)
    if existing_hash == new_hash:
        logger.debug("METAR unchanged for %s/%s, skipping write", icao, source)
        return False

    payload = {
        "icao": icao,
        "raw_text": raw_text,
        "observed_at": metar_data.get("observed_at") or _now_utc().isoformat(),
        "updated_at": _now_utc().isoformat(),
        "hash": new_hash,
        "source": metar_data.get("source", "unknown"),
        "source_key": source,
    }

    await set_source_metar(redis_client, icao, source, payload, cfg.metar_ttl_seconds)
    # 同时追加到历史记录，用于 Dashboard 按时间窗口回溯
    is_new_history = await add_source_history(redis_client, icao, source, payload)

    # 官方修正事件检测：同一 observed_at 出现了新的 hash
    if is_new_history:
        obs_dt = _parse_observed_at(payload["observed_at"])
        if obs_dt is not None:
            prior_records = await get_source_history(
                redis_client, icao, source, obs_dt, obs_dt
            )
            # 排除当前这条自己，取同一 observed_at 下的其他 hash
            prior_different = [
                r
                for r in prior_records
                if r.get("hash") != new_hash
            ]
            if prior_different:
                # 按 updated_at 排序，取最早出现的那条作为 first_record
                prior_different.sort(key=lambda r: r.get("updated_at") or "")
                await record_correction_event(
                    redis_client,
                    icao,
                    source,
                    obs_dt,
                    first_record=prior_different[0],
                    corrected_record=payload,
                )

    logger.info(
        "Source METAR updated for %s/%s (hash=%s... source=%s)",
        icao,
        source,
        new_hash[:8],
        payload["source"],
    )
    return True


async def _store_winner_if_changed(
    redis_client: Any,
    icao: str,
    winner: dict[str, Any],
    settings: Optional[Settings] = None,
) -> bool:
    """如果择优后的 METAR 文本有变化，则写入最终 Key 并返回 True."""
    cfg = settings or get_settings()
    icao = icao.upper()
    raw_text = winner["raw_text"]
    new_hash = _compute_hash(raw_text)

    existing_hash = await get_existing_hash(redis_client, icao)
    if existing_hash == new_hash:
        logger.debug("Adopted METAR unchanged for %s, skipping write", icao)
        return False

    payload = {
        "icao": icao,
        "raw_text": raw_text,
        "observed_at": winner.get("observed_at") or _now_utc().isoformat(),
        "updated_at": _now_utc().isoformat(),
        "hash": new_hash,
        "source": winner.get("source", "unknown"),
        "source_key": winner.get("source_key", "unknown"),
    }
    await set_metar(redis_client, icao, payload, cfg.metar_ttl_seconds)
    logger.info(
        "Adopted METAR updated for %s (hash=%s... source=%s/%s)",
        icao,
        new_hash[:8],
        payload["source"],
        payload["source_key"],
    )
    return True


async def _merge_and_store_winners(
    redis_client: Any,
    settings: Optional[Settings] = None,
) -> None:
    """为每个监控机场读取两个数据源记录，择优后写入最终 Key."""
    cfg = settings or get_settings()
    from app.database import get_source_metar

    for code in cfg.monitor_airports_list:
        icao = code.upper()
        try:
            weathergov_record = await get_source_metar(redis_client, icao, "weathergov")
            awc_record = await get_source_metar(redis_client, icao, "awc")
            winner = _select_winner(weathergov_record, awc_record)
            if winner is None:
                logger.warning("No METAR data available for %s", icao)
                continue
            await _store_winner_if_changed(redis_client, icao, winner, cfg)
        except Exception as exc:
            logger.error("Failed to merge/store winner for %s: %s", icao, exc)


async def _poll_cycle(settings: Optional[Settings] = None) -> None:
    """执行一轮采集：两个数据源独立批量采集，分别入库，再择优合并."""
    cfg = settings or get_settings()
    redis_client = await get_redis(cfg)

    # 1. 独立批量采集：weather.gov 采集全部机场，AWC 采集全部机场（除黑名单）
    weathergov_results, awc_results = await asyncio.gather(
        _fetch_weathergov_batch(cfg.monitor_airports_list, cfg),
        _fetch_awc_batch(cfg.monitor_airports_list, cfg),
    )

    # 2. 将 weather.gov 结果写入 source-specific Key
    for code, result in weathergov_results.items():
        try:
            await _store_source_if_changed(
                redis_client, code, "weathergov", result, cfg
            )
        except Exception as exc:
            logger.error("Failed to store weather.gov METAR for %s: %s", code, exc)

    # 3. 将 AWC 结果写入 source-specific Key
    for code, result in awc_results.items():
        try:
            await _store_source_if_changed(redis_client, code, "awc", result, cfg)
        except Exception as exc:
            logger.error("Failed to store AWC METAR for %s: %s", code, exc)

    # 4. 为每个机场择优并写入最终 Key
    await _merge_and_store_winners(redis_client, cfg)


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
