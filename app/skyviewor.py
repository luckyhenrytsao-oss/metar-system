"""Skyviewor fast-METAR WebSocket 数据源采集器.

按 M1 技术参考文档 (docs/skyviewor_integration_for_m2.md) 实现：
- 使用长期 API Key 换取短命 Token
- WebSocket 长连接订阅中国机场
- 响应服务端 ping 心跳
- 断线自动重连
- 对 METAR/SPECI 应用采信规则，可信数据进入 M2 标准流程，
  不可信数据写入独立审计 Key
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import websockets

from app.config import Settings, get_settings
from app.database import add_skyviewor_audit, get_redis
from app.latency_logger import log_latency

logger = logging.getLogger(__name__)

# 模块级重连状态
_skyviewor_stop_event: Optional[asyncio.Event] = None


async def close_skyviewor_loop() -> None:
    """通知 Skyviewor 采集循环退出，用于应用关闭时."""
    global _skyviewor_stop_event
    if _skyviewor_stop_event is not None:
        _skyviewor_stop_event.set()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _log_skyviewor_received(
    icao: str,
    obs_time: Optional[datetime],
    raw_text: str,
    report_type: str,
    skyviewor_received_at: str,
    trusted: bool,
    reject_reason: Optional[str],
) -> None:
    """记录 Skyviewor 消息到达事件，无论后续是否被采信."""
    await log_latency({
        "host": "m2",
        "icao": icao,
        "observed_at": obs_time.isoformat() if obs_time else None,
        "event_type": "skyviewor_received",
        "source_key": "skyviewor",
        "timestamps": {"source_received_at": skyviewor_received_at},
        "trusted": trusted,
        "reject_reason": reject_reason,
        "raw_text": raw_text,
    })


def _compute_hash(raw_text: str) -> str:
    """计算 METAR 文本 SHA1 hash，用于去重."""
    return hashlib.sha1(raw_text.encode("utf-8")).hexdigest()


def _parse_iso_time(value: str) -> Optional[datetime]:
    """解析 ISO 8601 时间字符串为 UTC datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _clean_raw_metar(raw: str) -> str:
    """去除 Skyviewor raw_metar 末尾的 '='，保持与其他源一致."""
    return raw.rstrip("=").strip()


def _is_observed_at_valid(
    obs_time: datetime,
    settings: Settings,
) -> bool:
    """校验观测时间是否在可接受的新鲜度窗口内.

    与 app.collector._is_observed_at_valid 保持一致，但避免循环导入.
    """
    from app.collector import _is_observed_at_valid as collector_is_valid

    return collector_is_valid(obs_time, settings)


def _should_trust(
    icao: str,
    report_type: str,
    obs_time: datetime,
    settings: Settings,
) -> bool:
    """根据 M2 采信规则判断一条 Skyviewor 记录是否可信.

    规则 (v1.0):
    - METAR:
      - ZBAA/ZGGG/ZSPD: :00 和 :30 都采信
      - 其他机场: 仅 :00 采信
    - SPECI:
      - 仅 ZBAA 采信
    """
    icao = icao.upper()
    report_type = report_type.upper()
    minute = obs_time.minute

    if report_type == "METAR":
        if icao in settings.skyviewor_trusted_half_hour_airports_set:
            return minute in {0, 30}
        return minute == 0

    if report_type == "SPECI":
        return icao in settings.skyviewor_trusted_speci_airports_set

    # 未知类型默认不采信
    return False


async def _get_skyviewor_token(settings: Settings) -> Optional[str]:
    """使用 API Key 换取临时 WebSocket Token."""
    api_key = settings.skyviewor_api_key
    if not api_key:
        logger.warning("Skyviewor API key not configured")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": settings.user_agent,
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.http_timeout)) as client:
            resp = await client.post(settings.skyviewor_token_url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            token = data.get("token")
            if token:
                logger.info("Fetched Skyviewor WebSocket token")
                return token
            logger.warning("Skyviewor token response missing token field")
    except httpx.HTTPError as exc:
        logger.error("Failed to fetch Skyviewor token: %s", exc)
    except json.JSONDecodeError as exc:
        logger.error("Skyviewor token response is not valid JSON: %s", exc)
    return None


async def _store_trusted_record(
    redis_client: Any,
    icao: str,
    raw_text: str,
    observed_at: datetime,
    report_type: str,
    settings: Settings,
    skyviewor_received_at: Optional[str] = None,
) -> None:
    """将可信 Skyviewor 记录写入标准 source key，并触发 winner 重新择优."""
    from app.collector import (
        _merge_and_store_winner_for,
        _store_source_if_changed,
    )

    metar_data: dict[str, Any] = {
        "icao": icao,
        "raw_text": raw_text,
        "observed_at": observed_at.isoformat(),
        "source": "Skyviewor fast-METAR",
        "source_key": "skyviewor",
        "report_type": report_type,
    }
    if skyviewor_received_at is not None:
        metar_data["skyviewor_received_at"] = skyviewor_received_at

    changed = await _store_source_if_changed(
        redis_client, icao, "skyviewor", metar_data, settings
    )
    # 只要收到可信记录就重新择优；即使 hash 未变也做一遍，保证其他源更新后能及时反映
    await _merge_and_store_winner_for(redis_client, icao, settings)

    if changed:
        logger.info(
            "Trusted Skyviewor record accepted for %s at %s (%s)",
            icao,
            observed_at.isoformat(),
            report_type,
        )


async def _process_incoming_item(
    item: dict[str, Any],
    redis_client: Any,
    settings: Settings,
) -> None:
    """处理一条 Skyviewor 数据消息."""
    skyviewor_received_at = _now_utc().isoformat()

    icao = item.get("icao", "").strip().upper()
    raw_metar = item.get("raw_metar", "")
    report_type = item.get("report_type", "").strip().upper()
    obs_time_str = item.get("obs_time", "")

    reject_reason: str | None = None
    trusted = False
    obs_time: datetime | None = None
    raw_text = ""

    if not icao or not raw_metar or not report_type:
        reject_reason = "missing_required_fields"
        await _log_skyviewor_received(
            icao, obs_time, raw_text, report_type, skyviewor_received_at, trusted, reject_reason
        )
        logger.debug("Skyviewor item missing required fields, skipping: %s", item)
        return

    # 只处理监控列表中的机场
    if icao not in settings.monitor_airports_list:
        reject_reason = "not_monitored"
        await _log_skyviewor_received(
            icao, obs_time, raw_text, report_type, skyviewor_received_at, trusted, reject_reason
        )
        logger.debug("Skyviewor item for non-monitored airport %s, skipping", icao)
        return

    # 只处理在 Skyviewor 订阅列表中的机场
    if icao not in settings.skyviewor_airports_list:
        reject_reason = "not_subscribed"
        await _log_skyviewor_received(
            icao, obs_time, raw_text, report_type, skyviewor_received_at, trusted, reject_reason
        )
        logger.debug("Skyviewor item for non-subscribed airport %s, skipping", icao)
        return

    obs_time = _parse_iso_time(obs_time_str)
    if obs_time is None:
        reject_reason = "invalid_obs_time"
        await _log_skyviewor_received(
            icao, obs_time, raw_text, report_type, skyviewor_received_at, trusted, reject_reason
        )
        logger.warning(
            "Skyviewor item for %s has invalid obs_time '%s', skipping",
            icao,
            obs_time_str,
        )
        return

    if not _is_observed_at_valid(obs_time, settings):
        reject_reason = "invalid_observed_at"
        await _log_skyviewor_received(
            icao, obs_time, raw_text, report_type, skyviewor_received_at, trusted, reject_reason
        )
        logger.warning(
            "Skyviewor item for %s rejected due to invalid observed_at: %s",
            icao,
            obs_time.isoformat(),
        )
        return

    raw_text = _clean_raw_metar(raw_metar)
    trusted = _should_trust(icao, report_type, obs_time, settings)

    if trusted:
        await _log_skyviewor_received(
            icao, obs_time, raw_text, report_type, skyviewor_received_at, trusted, reject_reason
        )
        await _store_trusted_record(
            redis_client,
            icao,
            raw_text,
            obs_time,
            report_type,
            settings,
            skyviewor_received_at=skyviewor_received_at,
        )
    else:
        reject_reason = "not_trusted"
        await _log_skyviewor_received(
            icao, obs_time, raw_text, report_type, skyviewor_received_at, trusted, reject_reason
        )
        audit_data = {
            "icao": icao,
            "raw_text": raw_text,
            "observed_at": obs_time.isoformat(),
            "updated_at": _now_utc().isoformat(),
            "source": "Skyviewor fast-METAR",
            "source_key": "skyviewor",
            "report_type": report_type,
            "hash": _compute_hash(raw_text),
            "trusted": False,
        }
        await add_skyviewor_audit(
            redis_client, icao, audit_data, settings.skyviewor_audit_retention_days
        )
        logger.debug(
            "Untrusted Skyviewor record saved to audit for %s at %s (%s)",
            icao,
            obs_time.isoformat(),
            report_type,
        )


async def _handle_message(
    message: str,
    redis_client: Any,
    settings: Settings,
) -> None:
    """处理一条 WebSocket 文本消息."""
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        logger.warning("Skyviewor WebSocket received invalid JSON: %s", message[:200])
        return

    msg_type = data.get("type", "")

    if msg_type == "ping":
        # ping 由连接循环统一回复，这里只记录
        logger.debug("Skyviewor ping received")
        return

    if msg_type == "new_metar_arrived":
        for item in data.get("data", []):
            try:
                await _process_incoming_item(item, redis_client, settings)
            except Exception as exc:
                logger.error("Failed to process Skyviewor item: %s", exc)
        return

    logger.debug("Skyviewor WebSocket unknown message type: %s", msg_type)


async def _send_pong(websocket: websockets.WebSocketClientProtocol) -> None:
    """回复服务端 ping."""
    try:
        await websocket.send(json.dumps({"action": "pong"}))
        logger.debug("Skyviewor pong sent")
    except Exception as exc:
        logger.warning("Failed to send Skyviewor pong: %s", exc)


async def _skyviewor_connection(
    redis_client: Any,
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    """建立 WebSocket 连接并运行消息循环，直到断开或收到停止信号."""
    token = await _get_skyviewor_token(settings)
    if not token:
        raise ConnectionError("Could not obtain Skyviewor WebSocket token")

    uri = f"{settings.skyviewor_ws_url}?token={token}"
    subscribe_msg = json.dumps(
        {
            "action": "subscribe",
            "icaos": settings.skyviewor_airports_list,
        }
    )

    logger.info("Connecting to Skyviewor WebSocket: %s", settings.skyviewor_ws_url)
    async with websockets.connect(uri) as websocket:
        logger.info("Skyviewor WebSocket connected")
        await websocket.send(subscribe_msg)
        logger.info("Skyviewor subscription sent for %d airports", len(settings.skyviewor_airports_list))

        while not stop_event.is_set():
            try:
                # 短超时轮询，兼顾消息响应和 stop_event 检查，避免遗留 Task
                message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed as exc:
                logger.warning("Skyviewor WebSocket closed: %s", exc)
                return
            except Exception as exc:
                logger.error("Skyviewor WebSocket receive error: %s", exc)
                return

            if not isinstance(message, str):
                logger.warning("Skyviewor WebSocket received non-text message: %s", type(message))
                continue

            # ping 需要立即回复，不经过通用处理
            try:
                parsed = json.loads(message)
                if parsed.get("type") == "ping":
                    await _send_pong(websocket)
                    continue
            except json.JSONDecodeError:
                pass

            await _handle_message(message, redis_client, settings)


async def start_skyviewor_loop(settings: Optional[Settings] = None) -> None:
    """启动 Skyviewor 采集循环，带断线重连.

    如果未启用或没有 API Key，则直接返回.
    """
    cfg = settings or get_settings()
    if not cfg.skyviewor_enabled:
        logger.info("Skyviewor disabled, not starting loop")
        return
    if not cfg.skyviewor_api_key:
        logger.warning("Skyviewor enabled but no API key configured, not starting loop")
        return

    global _skyviewor_stop_event
    _skyviewor_stop_event = asyncio.Event()
    stop_event = _skyviewor_stop_event

    redis_client = await get_redis(cfg)
    reconnect_delay = cfg.skyviewor_reconnect_min_seconds

    logger.info("Starting Skyviewor fast-METAR loop")
    while not stop_event.is_set():
        try:
            await _skyviewor_connection(redis_client, cfg, stop_event)
        except asyncio.CancelledError:
            logger.info("Skyviewor loop cancelled")
            raise
        except Exception as exc:
            logger.error("Skyviewor connection error: %s", exc)

        if stop_event.is_set():
            break

        logger.info("Skyviewor reconnecting in %.1fs", reconnect_delay)
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=reconnect_delay,
            )
        except asyncio.TimeoutError:
            pass

        reconnect_delay = min(
            reconnect_delay * 2,
            cfg.skyviewor_reconnect_max_seconds,
        )

    logger.info("Skyviewor loop stopped")
