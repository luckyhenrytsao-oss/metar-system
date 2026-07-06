"""采集器单元测试（Mock HTTPX 请求与网络异常）."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import respx
from httpx import Response

from app.collector import (
    _fetch_airport,
    _fetch_awc_single,
    _fetch_weathergov_batch,
    _parse_metar_time,
    _store_if_changed,
    close_http_client,
    start_collector_loop,
)
from app.config import Settings


@pytest.fixture(autouse=True)
def reset_http_client():
    """每个测试用例结束后关闭 HTTP 客户端，避免状态污染."""
    yield
    asyncio.run(close_http_client())


@pytest.fixture
def sample_awc_response():
    """AWC API 成功响应示例（含 RMK+T 精确温度组）."""
    return [
        {
            "icaoId": "KJFK",
            "rawOb": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
            "reportTime": "2026-07-05T04:55:00Z",
            "metarType": "METAR",
        }
    ]


@pytest.fixture
def sample_weathergov_response():
    """weather.gov / SynopticData 成功响应示例（含 RMK+T 精确温度组）."""
    return {
        "STATION": [
            {
                "STID": "VHHH",
                "MNET_ID": "239",
                "OBSERVATIONS": {
                    "date_time": ["2026-07-05T04:50:00Z"],
                    "air_temp_set_1": [28.0],
                    "metar_set_1": ["METAR VHHH 050450Z 09010KT 10SM FEW020 28/26 Q1012 RMK AO2 T02800260"],
                    "metar_origin_set_1": [1.0],
                },
            }
        ]
    }


@pytest.fixture
def sample_weathergov_non_metar_response():
    """weather.gov 返回 AUTO 与真正 METAR；应过滤 AUTO，只保留带 RMK+T 的 METAR."""
    return {
        "STATION": [
            {
                "STID": "KAUS",
                "MNET_ID": "1",
                "OBSERVATIONS": {
                    "date_time": ["2026-07-05T04:55:00Z", "2026-07-05T04:50:00Z"],
                    "air_temp_set_1": [28.0, 27.0],
                    "metar_set_1": [
                        "METAR KAUS 050455Z AUTO 24008KT 10SM 28/20 A3012",
                        "METAR KAUS 050450Z 24008KT 10SM 27/20 A3012 RMK AO2 T02700200",
                    ],
                    "metar_origin_set_1": [None, 1.0],
                },
            }
        ]
    }


@pytest.mark.asyncio
async def test_fetch_awc_single_success(fake_redis, test_settings, sample_awc_response):
    """测试从 AWC 成功获取单机场 METAR."""
    with respx.mock:
        route = respx.get("https://aviationweather.gov/api/data/metar").mock(
            return_value=Response(200, json=sample_awc_response)
        )
        result = await _fetch_awc_single("KJFK", test_settings)

    assert result is not None
    assert result["icao"] == "KJFK"
    assert "METAR KJFK" in result["raw_text"]
    assert result["source"] == "aviationweather.gov"
    assert route.called


@pytest.mark.asyncio
async def test_fetch_weathergov_batch_success(
    fake_redis, test_settings, sample_weathergov_response
):
    """测试从 weather.gov 批量获取 METAR."""
    test_settings.weathergov_token = "fake-token-for-test"

    with respx.mock:
        respx.get("https://api.synopticdata.com/v2/stations/timeseries").mock(
            return_value=Response(200, json=sample_weathergov_response)
        )
        results = await _fetch_weathergov_batch(["VHHH"], test_settings)

    assert "VHHH" in results
    assert "METAR VHHH" in results["VHHH"]["raw_text"]
    assert results["VHHH"]["source"] == "weather.gov"


@pytest.mark.asyncio
async def test_fetch_weathergov_filters_non_metar_origin(
    fake_redis, test_settings, sample_weathergov_non_metar_response
):
    """测试 weather.gov 采集器过滤 ASOS/AWOS 自动观测，只保留真正 METAR."""
    test_settings.weathergov_token = "fake-token-for-test"

    with respx.mock:
        respx.get("https://api.synopticdata.com/v2/stations/timeseries").mock(
            return_value=Response(200, json=sample_weathergov_non_metar_response)
        )
        results = await _fetch_weathergov_batch(["KAUS"], test_settings)

    assert "KAUS" in results
    # 应该跳过 AUTO（metar_origin_set_1=None）的记录，取更早的真正 METAR
    assert "050450Z" in results["KAUS"]["raw_text"]
    assert "AUTO" not in results["KAUS"]["raw_text"]


@pytest.mark.asyncio
async def test_store_if_changed_writes_new_data(fake_redis, test_settings):
    """测试首次写入 Redis 成功."""
    metar_data = {
        "icao": "KJFK",
        "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
        "observed_at": "2026-07-05T04:55:00+00:00",
        "source": "aviationweather.gov",
    }

    written = await _store_if_changed(fake_redis, "KJFK", metar_data, test_settings)
    assert written is True

    from app.database import get_metar

    stored = await get_metar(fake_redis, "KJFK")
    assert stored is not None
    assert stored["icao"] == "KJFK"
    assert stored["raw_text"] == metar_data["raw_text"]
    assert "hash" in stored
    assert "updated_at" in stored


@pytest.mark.asyncio
async def test_store_if_changed_skips_duplicate(fake_redis, test_settings):
    """测试相同 METAR 文本不会重复写入 Redis."""
    metar_data = {
        "icao": "KJFK",
        "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
        "observed_at": "2026-07-05T04:55:00+00:00",
        "source": "aviationweather.gov",
    }

    first = await _store_if_changed(fake_redis, "KJFK", metar_data, test_settings)
    assert first is True

    second = await _store_if_changed(fake_redis, "KJFK", metar_data, test_settings)
    assert second is False


@pytest.mark.asyncio
async def test_store_if_changed_updates_on_different_text(fake_redis, test_settings):
    """测试 SPECI/新 METAR 会覆盖旧数据."""
    old_data = {
        "icao": "KJFK",
        "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
        "observed_at": "2026-07-05T04:55:00+00:00",
        "source": "aviationweather.gov",
    }
    new_data = {
        "icao": "KJFK",
        "raw_text": "SPECI KJFK 050500Z 25012KT 10SM FEW250 24/17 A3010 RMK AO2 T02400170",
        "observed_at": "2026-07-05T05:00:00+00:00",
        "source": "aviationweather.gov",
    }

    await _store_if_changed(fake_redis, "KJFK", old_data, test_settings)
    updated = await _store_if_changed(fake_redis, "KJFK", new_data, test_settings)
    assert updated is True

    from app.database import get_metar

    stored = await get_metar(fake_redis, "KJFK")
    assert stored["raw_text"] == new_data["raw_text"]


@pytest.mark.asyncio
async def test_fetch_airport_prefers_weathergov_then_awc(
    fake_redis, test_settings, sample_weathergov_response, sample_awc_response
):
    """测试 _fetch_airport 优先使用 weather.gov 批量结果，缺失时回退 AWC."""
    test_settings.weathergov_token = "fake-token-for-test"

    with respx.mock:
        respx.get("https://api.synopticdata.com/v2/stations/timeseries").mock(
            return_value=Response(200, json=sample_weathergov_response)
        )
        respx.get("https://aviationweather.gov/api/data/metar").mock(
            return_value=Response(200, json=sample_awc_response)
        )

        # VHHH 在 weather.gov 响应中
        weathergov_batch = {
            "VHHH": {
                "icao": "VHHH",
                "raw_text": "METAR VHHH 050450Z 09010KT 10SM FEW020 28/26 Q1012 RMK AO2 T02800260",
                "observed_at": "2026-07-05T04:50:00+00:00",
                "source": "weather.gov",
            }
        }
        vhhh = await _fetch_airport("VHHH", weathergov_batch, test_settings)
        # 注意：这里直接传入 weathergov 批量结果，会命中 VHHH
        # KJFK 不在 weather.gov 结果中，会回退 AWC
        kjfk = await _fetch_airport("KJFK", {}, test_settings)

    assert vhhh is not None
    assert vhhh["icao"] == "VHHH"
    assert vhhh["source"] == "weather.gov"

    assert kjfk is not None
    assert kjfk["icao"] == "KJFK"
    assert kjfk["source"] == "aviationweather.gov"


@pytest.mark.asyncio
async def test_collector_loop_survives_network_errors(fake_redis, test_settings, monkeypatch):
    """测试后台采集循环在网络异常时不会崩溃退出."""
    test_settings.monitor_airports = "KJFK"
    test_settings.poll_interval_seconds = 0.05  # 加速测试

    call_count = 0

    async def fake_poll_cycle(settings):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Simulated network crash")
        # 第二次正常返回，模拟恢复

    monkeypatch.setattr("app.collector._poll_cycle", fake_poll_cycle)

    # 启动循环，等待两次轮询后取消
    task = asyncio.create_task(start_collector_loop(test_settings))
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count >= 2, "采集循环应在异常后继续执行"


def test_parse_metar_time_cross_day():
    """测试从 METAR 文本解析跨天边界的时间."""
    base = datetime(2026, 7, 5, 0, 30, tzinfo=timezone.utc)
    # 7 月 5 日 00:30，METAR 显示 2350Z，实际应为 7 月 4 日
    raw = "METAR KJFK 042350Z 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180"
    parsed = _parse_metar_time(raw, base)
    assert parsed is not None
    assert parsed.day == 4
    assert parsed.hour == 23
    assert parsed.minute == 50


@pytest.mark.asyncio
async def test_fetch_awc_handles_rate_limit(fake_redis, test_settings):
    """测试 AWC 返回 429 时不抛异常."""
    with respx.mock:
        respx.get("https://aviationweather.gov/api/data/metar").mock(
            return_value=Response(429, text="Rate Limited")
        )
        result = await _fetch_awc_single("KJFK", test_settings)
    assert result is None
