"""延迟测量日志写入器单元测试."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.latency_logger import CHINA_AIRPORTS, log_latency


@pytest.fixture
def latency_log_path(tmp_path, monkeypatch) -> Path:
    """返回一个临时日志路径，并覆盖配置."""
    path = tmp_path / "m2_latency.jsonl"
    monkeypatch.setenv("M2_LATENCY_LOG_ENABLED", "true")
    monkeypatch.setenv("M2_LATENCY_LOG_PATH", str(path))
    get_settings.cache_clear()
    return path


@pytest.mark.asyncio
async def test_log_latency_writes_china_airport(latency_log_path: Path):
    await log_latency({
        "icao": "ZBAA",
        "event_type": "source_update",
        "source_key": "skyviewor",
    })
    assert latency_log_path.exists()
    lines = latency_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["icao"] == "ZBAA"
    assert record["event_type"] == "source_update"
    assert "logged_at" in record


@pytest.mark.asyncio
async def test_log_latency_ignores_non_china_airport(latency_log_path: Path):
    await log_latency({
        "icao": "KJFK",
        "event_type": "source_update",
        "source_key": "awc",
    })
    assert not latency_log_path.exists() or latency_log_path.read_text() == ""


@pytest.mark.asyncio
async def test_log_latency_disabled(monkeypatch, tmp_path: Path):
    path = tmp_path / "m2_latency.jsonl"
    monkeypatch.setenv("M2_LATENCY_LOG_ENABLED", "false")
    monkeypatch.setenv("M2_LATENCY_LOG_PATH", str(path))
    get_settings.cache_clear()
    await log_latency({
        "icao": "ZBAA",
        "event_type": "source_update",
        "source_key": "skyviewor",
    })
    assert not path.exists()


def test_china_airports_set():
    assert "ZBAA" in CHINA_AIRPORTS
    assert "ZGGG" in CHINA_AIRPORTS
    assert "KJFK" not in CHINA_AIRPORTS
