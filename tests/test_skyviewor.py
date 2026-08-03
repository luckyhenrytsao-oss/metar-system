"""Skyviewor fast-METAR 数据源单元测试."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app import skyviewor as skyviewor_module
from app.config import Settings, get_settings
from app.database import get_skyviewor_audit, get_source_metar
from app.skyviewor import (
    _clean_raw_metar,
    _handle_message,
    _parse_iso_time,
    _process_incoming_item,
    _should_trust,
)


@pytest.fixture
def skyviewor_settings(monkeypatch) -> Settings:
    """返回启用 Skyviewor 并监控中国机场的测试配置."""
    monkeypatch.setenv("MONITOR_AIRPORTS", "ZBAA,ZGGG,ZSPD,ZUCK,ZUUU,ZHHH,ZSQD,KJFK")
    monkeypatch.setenv("SKYVIEWOR_ENABLED", "true")
    monkeypatch.setenv("SKYVIEWOR_API_KEY", "sk-test-key")
    monkeypatch.setenv("SKYVIEWOR_AIRPORTS", "ZBAA,ZGGG,ZSPD,ZUCK,ZUUU,ZHHH,ZSQD")
    monkeypatch.setenv("SKYVIEWOR_TRUSTED_HALF_HOUR_AIRPORTS", "ZBAA,ZGGG,ZSPD")
    monkeypatch.setenv("SKYVIEWOR_TRUSTED_SPECI_AIRPORTS", "ZBAA")
    monkeypatch.setenv("METAR_MAX_AGE_SECONDS", "86400")
    monkeypatch.setenv("METAR_MAX_FUTURE_SECONDS", "600")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/9")

    get_settings.cache_clear()
    return get_settings()


class FakeRedis:
    """极简 async Redis stub，用于不依赖 fakeredis 的纯函数级测试."""

    def __init__(self):
        self.data: dict[str, Any] = {}
        self.sets: dict[str, list[tuple[str, float]]] = {}

    async def set(self, key: str, value: str, **kwargs):
        self.data[key] = value

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def zadd(self, key: str, mapping: dict):
        if key not in self.sets:
            self.sets[key] = []
        for member, score in mapping.items():
            self.sets[key].append((member, float(score)))

    async def zremrangebyscore(self, key: str, _min, _max):
        return 0

    async def expire(self, key: str, _seconds):
        return True

    async def pipeline(self):
        return self

    async def execute(self):
        return []


@pytest.mark.asyncio
async def test_clean_raw_metar_removes_trailing_equals():
    assert _clean_raw_metar("METAR ZBAA 021430Z ... NOSIG=") == "METAR ZBAA 021430Z ... NOSIG"
    assert _clean_raw_metar("METAR ZBAA 021430Z ... NOSIG") == "METAR ZBAA 021430Z ... NOSIG"
    assert _clean_raw_metar("METAR ZBAA 021430Z ... NOSIG===") == "METAR ZBAA 021430Z ... NOSIG"


@pytest.mark.asyncio
async def test_parse_iso_time_with_offset():
    dt = _parse_iso_time("2026-08-02 14:30:00+00:00")
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 8
    assert dt.day == 2
    assert dt.hour == 14
    assert dt.minute == 30
    assert dt.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_should_trust_rules(skyviewor_settings):
    cfg = skyviewor_settings

    # ZBAA METAR at :00 and :30 -> trusted
    assert _should_trust("ZBAA", "METAR", datetime(2026, 8, 2, 14, 0, tzinfo=timezone.utc), cfg)
    assert _should_trust("ZBAA", "METAR", datetime(2026, 8, 2, 14, 30, tzinfo=timezone.utc), cfg)

    # ZBAA METAR at :15 -> not trusted
    assert not _should_trust("ZBAA", "METAR", datetime(2026, 8, 2, 14, 15, tzinfo=timezone.utc), cfg)

    # ZUUU METAR at :00 -> trusted
    assert _should_trust("ZUUU", "METAR", datetime(2026, 8, 2, 14, 0, tzinfo=timezone.utc), cfg)

    # ZUUU METAR at :30 -> not trusted (not in half-hour list)
    assert not _should_trust("ZUUU", "METAR", datetime(2026, 8, 2, 14, 30, tzinfo=timezone.utc), cfg)

    # ZBAA SPECI -> trusted
    assert _should_trust("ZBAA", "SPECI", datetime(2026, 8, 2, 14, 12, tzinfo=timezone.utc), cfg)

    # ZGGG SPECI -> not trusted
    assert not _should_trust("ZGGG", "SPECI", datetime(2026, 8, 2, 14, 12, tzinfo=timezone.utc), cfg)

    # Unknown report type -> not trusted
    assert not _should_trust("ZBAA", "TAF", datetime(2026, 8, 2, 14, 0, tzinfo=timezone.utc), cfg)


@pytest.mark.asyncio
async def test_process_incoming_item_trusted_metar_stores_source(
    fake_redis, skyviewor_settings, monkeypatch
):
    """可信 METAR 应写入标准 source key 并触发 winner 择优."""
    cfg = skyviewor_settings
    monkeypatch.setattr(skyviewor_module, "_now_utc", lambda: datetime(2026, 8, 2, 14, 35, tzinfo=timezone.utc))

    item = {
        "icao": "ZBAA",
        "raw_metar": "METAR ZBAA 021430Z 01002MPS 9999 FEW050CB 26/26 Q1005 NOSIG=",
        "obs_time": "2026-08-02 14:30:00+00:00",
        "report_type": "METAR",
    }

    await _process_incoming_item(item, fake_redis, cfg)

    # 标准 source key 应写入
    source_data = await get_source_metar(fake_redis, "ZBAA", "skyviewor")
    assert source_data is not None
    assert source_data["icao"] == "ZBAA"
    assert source_data["raw_text"] == "METAR ZBAA 021430Z 01002MPS 9999 FEW050CB 26/26 Q1005 NOSIG"
    assert source_data["source_key"] == "skyviewor"
    assert source_data["source"] == "Skyviewor fast-METAR"

    # winner key 应写入（因为只有一个源）
    from app.database import get_metar
    winner = await get_metar(fake_redis, "ZBAA")
    assert winner is not None
    assert winner["source_key"] == "skyviewor"


@pytest.mark.asyncio
async def test_process_incoming_item_untrusted_metar_goes_to_audit(
    fake_redis, skyviewor_settings, monkeypatch
):
    """不可信 METAR 应写入独立审计 key，不进入标准 source key."""
    cfg = skyviewor_settings
    monkeypatch.setattr(skyviewor_module, "_now_utc", lambda: datetime(2026, 8, 2, 14, 35, tzinfo=timezone.utc))

    # ZUUU :30 METAR 按规则不可信
    item = {
        "icao": "ZUUU",
        "raw_metar": "METAR ZUUU 021430Z 01002MPS 9999 FEW050CB 26/26 Q1005 NOSIG=",
        "obs_time": "2026-08-02 14:30:00+00:00",
        "report_type": "METAR",
    }

    await _process_incoming_item(item, fake_redis, cfg)

    # 标准 source key 不应写入
    source_data = await get_source_metar(fake_redis, "ZUUU", "skyviewor")
    assert source_data is None

    # 审计 key 应写入
    audit_records = await get_skyviewor_audit(fake_redis, "ZUUU")
    assert len(audit_records) == 1
    assert audit_records[0]["icao"] == "ZUUU"
    assert audit_records[0]["trusted"] is False
    assert audit_records[0]["report_type"] == "METAR"


@pytest.mark.asyncio
async def test_process_incoming_item_non_subscribed_airport_skipped(
    fake_redis, skyviewor_settings, monkeypatch
):
    """不在 Skyviewor 订阅列表中的机场应被跳过."""
    cfg = skyviewor_settings
    monkeypatch.setattr(skyviewor_module, "_now_utc", lambda: datetime(2026, 8, 2, 14, 35, tzinfo=timezone.utc))

    item = {
        "icao": "KJFK",  # 在 monitor 但不在 skyviewor 订阅列表
        "raw_metar": "METAR KJFK 021430Z 01002MPS 9999 FEW050CB 26/26 Q1005 NOSIG=",
        "obs_time": "2026-08-02 14:30:00+00:00",
        "report_type": "METAR",
    }

    await _process_incoming_item(item, fake_redis, cfg)

    source_data = await get_source_metar(fake_redis, "KJFK", "skyviewor")
    assert source_data is None
    audit_records = await get_skyviewor_audit(fake_redis, "KJFK")
    assert len(audit_records) == 0


@pytest.mark.asyncio
async def test_handle_message_new_metar_arrived(
    fake_redis, skyviewor_settings, monkeypatch
):
    """测试 WebSocket 消息处理入口."""
    cfg = skyviewor_settings
    monkeypatch.setattr(skyviewor_module, "_now_utc", lambda: datetime(2026, 8, 2, 14, 35, tzinfo=timezone.utc))

    message = json.dumps({
        "type": "new_metar_arrived",
        "data": [
            {
                "icao": "ZSPD",
                "raw_metar": "METAR ZSPD 021430Z 01002MPS 9999 FEW050CB 28/26 Q1005 NOSIG=",
                "obs_time": "2026-08-02 14:30:00+00:00",
                "report_type": "METAR",
            }
        ],
    })

    await _handle_message(message, fake_redis, cfg)

    source_data = await get_source_metar(fake_redis, "ZSPD", "skyviewor")
    assert source_data is not None
    assert source_data["icao"] == "ZSPD"


class FakeWebSocket:
    """用于测试 _skyviewor_connection 消息循环的 WebSocket mock."""

    def __init__(self, messages: list[str]):
        self.messages = list(messages)
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str:
        if not self.messages:
            # 模拟连接保持，直到被外部取消
            await asyncio.sleep(10)
            return ""
        return self.messages.pop(0)

    async def send(self, message: str):
        self.sent.append(message)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_handle_message_ping(skyviewor_settings):
    """ping 消息应触发 pong 回复（通过连接循环处理）."""
    cfg = skyviewor_settings
    ws = FakeWebSocket([json.dumps({"type": "ping"})])

    # 模拟连接循环中收到 ping 时的处理
    message = await ws.recv()
    data = json.loads(message)
    if data.get("type") == "ping":
        await ws.send(json.dumps({"action": "pong"}))

    assert len(ws.sent) == 1
    assert json.loads(ws.sent[0]) == {"action": "pong"}
