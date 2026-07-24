"""采集器单元测试（Mock HTTPX 请求与网络异常）."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import respx
from httpx import Response

from app.collector import (
    _IemLdmReader,
    _fetch_airport,
    _fetch_awc_batch,
    _fetch_iem_batch,
    _fetch_weathergov_batch,
    _has_precision_temp,
    _merge_and_store_winners,
    _parse_iem_bulletins,
    _parse_metar_time,
    _select_winner,
    _store_source_if_changed,
    _store_winner_if_changed,
    close_http_client,
    start_collector_loop,
)


@pytest.fixture(autouse=True)
def reset_http_client():
    """每个测试用例结束后关闭 HTTP 客户端，避免状态污染."""
    yield
    asyncio.run(close_http_client())


@pytest.fixture
def sample_awc_response():
    """AWC API 批量响应示例（含 RMK+T 精确温度组）."""
    return [
        {
            "icaoId": "KJFK",
            "rawOb": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
            "reportTime": "2026-07-05T04:55:00Z",
            "metarType": "METAR",
        },
        {
            "icaoId": "EGLL",
            "rawOb": "METAR EGLL 050455Z 24008KT 10SM FEW250 13/10 A3012 RMK AO2 T01330100",
            "reportTime": "2026-07-05T04:55:00Z",
            "metarType": "METAR",
        },
    ]


@pytest.fixture
def sample_awc_response_with_auto():
    """AWC API 返回 AUTO 与 METAR；AUTO 应被视为有效报文保留."""
    return [
        {
            "icaoId": "KJFK",
            "rawOb": "METAR KJFK 050455Z AUTO 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
            "reportTime": "2026-07-05T04:55:00Z",
            "metarType": "AUTO",
        },
        {
            "icaoId": "KJFK",
            "rawOb": "METAR KJFK 050450Z 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
            "reportTime": "2026-07-05T04:50:00Z",
            "metarType": "METAR",
        },
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
                    "metar_set_1": [
                        "METAR VHHH 050450Z 09010KT 10SM FEW020 28/26 Q1012 RMK AO2 T02800260"
                    ],
                    "metar_origin_set_1": [1.0],
                },
            }
        ]
    }


@pytest.fixture
def sample_weathergov_non_metar_response():
    """weather.gov 返回 AUTO 与真正 METAR；应过滤 AUTO，只保留真正 METAR."""
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
async def test_fetch_awc_batch_success(fake_redis, test_settings, sample_awc_response):
    """测试从 AWC 批量获取多个机场 METAR."""
    with respx.mock:
        route = respx.get("https://aviationweather.gov/api/data/metar").mock(
            return_value=Response(200, json=sample_awc_response)
        )
        results = await _fetch_awc_batch(["KJFK", "EGLL"], test_settings)

    assert "KJFK" in results
    assert "EGLL" in results
    assert results["KJFK"]["source"] == "aviationweather.gov"
    assert results["EGLL"]["source"] == "aviationweather.gov"
    assert route.called


@pytest.mark.asyncio
async def test_fetch_awc_batch_keeps_auto(
    fake_redis, test_settings, sample_awc_response_with_auto, monkeypatch
):
    """测试 AWC 批量请求保留 AUTO 报文，与 T0TX 口径一致."""
    monkeypatch.setattr(
        "app.collector._now_utc",
        lambda: datetime(2026, 7, 5, 4, 55, tzinfo=timezone.utc),
    )
    with respx.mock:
        respx.get("https://aviationweather.gov/api/data/metar").mock(
            return_value=Response(200, json=sample_awc_response_with_auto)
        )
        results = await _fetch_awc_batch(["KJFK"], test_settings)

    assert "KJFK" in results
    # AUTO 报文更新且被保留
    assert "AUTO" in results["KJFK"]["raw_text"]
    assert results["KJFK"]["observed_at"] == "2026-07-05T04:55:00+00:00"


@pytest.mark.asyncio
async def test_fetch_awc_batch_filters_speci_for_selected_stations(fake_redis, test_settings):
    """测试 UUWW / LTFM / LLBG 在 AWC 中跳过 SPECI，其他机场正常保留."""
    response = [
        {
            "icaoId": "UUWW",
            "rawOb": "SPECI UUWW 050455Z 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
            "reportTime": "2026-07-05T04:55:00Z",
            "metarType": "SPECI",
        },
        {
            "icaoId": "UUWW",
            "rawOb": "METAR UUWW 050430Z 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
            "reportTime": "2026-07-05T04:30:00Z",
            "metarType": "METAR",
        },
        {
            "icaoId": "KJFK",
            "rawOb": "SPECI KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
            "reportTime": "2026-07-05T04:55:00Z",
            "metarType": "SPECI",
        },
    ]
    with respx.mock:
        respx.get("https://aviationweather.gov/api/data/metar").mock(
            return_value=Response(200, json=response)
        )
        results = await _fetch_awc_batch(["UUWW", "KJFK"], test_settings)

    # UUWW 的 SPECI 被过滤，但同一机场的 METAR 仍被保留
    assert "UUWW" in results
    assert "SPECI" not in results["UUWW"]["raw_text"]
    assert results["UUWW"]["raw_text"].startswith("METAR UUWW")

    # KJFK 不在过滤列表，SPECI 正常保留
    assert "KJFK" in results
    assert "SPECI" in results["KJFK"]["raw_text"]


@pytest.mark.asyncio
async def test_fetch_awc_batch_disabled_for_uuww(fake_redis, test_settings):
    """UUWW 不再全局禁用 AWC；本测试保留以确认 AWC 请求正常发起."""
    with respx.mock:
        route = respx.get("https://aviationweather.gov/api/data/metar").mock(
            return_value=Response(200, json=[])
        )
        results = await _fetch_awc_batch(["UUWW", "KJFK"], test_settings)

    # UUWW 请求仍会发起
    assert route.called
    assert results == {}


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
async def test_store_source_if_changed_writes_new_data(fake_redis, test_settings):
    """测试首次写入数据源专属 Redis Key 成功."""
    metar_data = {
        "icao": "KJFK",
        "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
        "observed_at": "2026-07-05T04:55:00+00:00",
        "source": "aviationweather.gov",
    }

    written = await _store_source_if_changed(
        fake_redis, "KJFK", "awc", metar_data, test_settings
    )
    assert written is True

    from app.database import get_source_metar

    stored = await get_source_metar(fake_redis, "KJFK", "awc")
    assert stored is not None
    assert stored["icao"] == "KJFK"
    assert stored["raw_text"] == metar_data["raw_text"]
    assert "hash" in stored
    assert "updated_at" in stored
    assert stored["source_key"] == "awc"


@pytest.mark.asyncio
async def test_store_source_if_changed_skips_duplicate(fake_redis, test_settings):
    """测试相同 METAR 文本不会重复写入数据源专属 Key."""
    metar_data = {
        "icao": "KJFK",
        "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
        "observed_at": "2026-07-05T04:55:00+00:00",
        "source": "aviationweather.gov",
    }

    first = await _store_source_if_changed(
        fake_redis, "KJFK", "awc", metar_data, test_settings
    )
    assert first is True

    second = await _store_source_if_changed(
        fake_redis, "KJFK", "awc", metar_data, test_settings
    )
    assert second is False


@pytest.mark.asyncio
async def test_store_winner_if_changed_updates_on_different_text(
    fake_redis, test_settings
):
    """测试择优后的 METAR 变化时会覆盖旧数据."""
    old_winner = {
        "icao": "KJFK",
        "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
        "observed_at": "2026-07-05T04:55:00+00:00",
        "source": "aviationweather.gov",
        "source_key": "awc",
    }
    new_winner = {
        "icao": "KJFK",
        "raw_text": "SPECI KJFK 050500Z 25012KT 10SM FEW250 24/17 A3010 RMK AO2 T02400170",
        "observed_at": "2026-07-05T05:00:00+00:00",
        "source": "weather.gov",
        "source_key": "weathergov",
    }

    await _store_winner_if_changed(fake_redis, "KJFK", old_winner, test_settings)
    updated = await _store_winner_if_changed(
        fake_redis, "KJFK", new_winner, test_settings
    )
    assert updated is True

    from app.database import get_metar

    stored = await get_metar(fake_redis, "KJFK")
    assert stored["raw_text"] == new_winner["raw_text"]
    assert stored["source_key"] == "weathergov"


@pytest.mark.asyncio
async def test_fetch_airport_prefers_weathergov_then_awc(
    fake_redis, test_settings, sample_weathergov_response, sample_awc_response
):
    """测试 _fetch_airport 优先使用 weather.gov 批量结果，缺失时回退 AWC."""
    weathergov_batch = {
        "VHHH": {
            "icao": "VHHH",
            "raw_text": "METAR VHHH 050450Z 09010KT 10SM FEW020 28/26 Q1012 RMK AO2 T02800260",
            "observed_at": "2026-07-05T04:50:00+00:00",
            "source": "weather.gov",
        }
    }
    awc_batch = {
        "KJFK": {
            "icao": "KJFK",
            "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
            "observed_at": "2026-07-05T04:55:00+00:00",
            "source": "aviationweather.gov",
        }
    }

    vhhh = await _fetch_airport("VHHH", weathergov_batch, awc_batch)
    kjfk = await _fetch_airport("KJFK", weathergov_batch, awc_batch)
    missing = await _fetch_airport("UUWW", weathergov_batch, awc_batch)

    assert vhhh is not None
    assert vhhh["icao"] == "VHHH"
    assert vhhh["source"] == "weather.gov"

    assert kjfk is not None
    assert kjfk["icao"] == "KJFK"
    assert kjfk["source"] == "aviationweather.gov"

    assert missing is None


@pytest.mark.asyncio
async def test_select_winner_prefers_later_observed_at():
    """测试择优逻辑选择 observed_at 更晚的记录."""
    weathergov_record = {
        "icao": "KSEA",
        "raw_text": "METAR KSEA 101053Z 25004KT 10SM SCT060 14/12 A2997 RMK AO2 T01390117",
        "observed_at": "2026-07-10T10:53:00+00:00",
        "updated_at": "2026-07-10T10:59:15+00:00",
        "source": "weather.gov",
        "source_key": "weathergov",
    }
    awc_record = {
        "icao": "KSEA",
        "raw_text": "METAR KSEA 101100Z 25004KT 10SM SCT060 14/12 A2997 RMK AO2 T01390117",
        "observed_at": "2026-07-10T11:00:00+00:00",
        "updated_at": "2026-07-10T11:05:20+00:00",
        "source": "aviationweather.gov",
        "source_key": "awc",
    }

    winner = _select_winner(weathergov_record, awc_record)
    assert winner["source_key"] == "awc"


@pytest.mark.asyncio
async def test_select_winner_tie_break_by_latency():
    """测试 observed_at 相同时，延迟更低的记录胜出."""
    weathergov_record = {
        "icao": "KSEA",
        "raw_text": "METAR KSEA 101053Z 25004KT 10SM SCT060 14/12 A2997 RMK AO2 T01390117",
        "observed_at": "2026-07-10T10:53:00+00:00",
        "updated_at": "2026-07-10T10:59:15+00:00",
        "source": "weather.gov",
        "source_key": "weathergov",
    }
    awc_record = {
        "icao": "KSEA",
        "raw_text": "METAR KSEA 101053Z 25004KT 10SM SCT060 14/12 A2997 RMK AO2 T01390117",
        "observed_at": "2026-07-10T10:53:00+00:00",
        "updated_at": "2026-07-10T10:55:00+00:00",
        "source": "aviationweather.gov",
        "source_key": "awc",
    }

    winner = _select_winner(weathergov_record, awc_record)
    assert winner["source_key"] == "awc"


@pytest.mark.asyncio
async def test_select_winner_falls_back_when_one_source_missing():
    """测试只有一方有数据时直接采用该方."""
    weathergov_record = {
        "icao": "KSEA",
        "raw_text": "METAR KSEA 101053Z 25004KT 10SM SCT060 14/12 A2997 RMK AO2 T01390117",
        "observed_at": "2026-07-10T10:53:00+00:00",
        "updated_at": "2026-07-10T10:59:15+00:00",
        "source": "weather.gov",
        "source_key": "weathergov",
    }

    assert _select_winner(weathergov_record, None)["source_key"] == "weathergov"
    assert _select_winner(None, weathergov_record)["source_key"] == "weathergov"
    assert _select_winner(None, None) is None


@pytest.mark.asyncio
async def test_merge_and_store_winners_selects_best_source(fake_redis, test_settings):
    """测试 _merge_and_store_winners 会从两个数据源中选择优胜者写入最终 Key."""
    from app.database import set_source_metar

    # weather.gov 数据更新鲜
    weathergov_data = {
        "icao": "KJFK",
        "raw_text": "SPECI KJFK 050500Z 25012KT 10SM FEW250 24/17 A3010 RMK AO2 T02400170",
        "observed_at": "2026-07-05T05:00:00+00:00",
        "source": "weather.gov",
        "source_key": "weathergov",
    }
    # AWC 数据较旧
    awc_data = {
        "icao": "KJFK",
        "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
        "observed_at": "2026-07-05T04:55:00+00:00",
        "source": "aviationweather.gov",
        "source_key": "awc",
    }

    await set_source_metar(
        fake_redis,
        "KJFK",
        "weathergov",
        weathergov_data,
        test_settings.metar_ttl_seconds,
    )
    await set_source_metar(
        fake_redis, "KJFK", "awc", awc_data, test_settings.metar_ttl_seconds
    )

    # 临时把监控列表设为 KJFK，避免查询其他未初始化的机场
    test_settings.monitor_airports = "KJFK"
    await _merge_and_store_winners(fake_redis, test_settings)

    from app.database import get_metar

    winner = await get_metar(fake_redis, "KJFK")
    assert winner is not None
    assert winner["source_key"] == "weathergov"
    assert "SPECI KJFK 050500Z" in winner["raw_text"]


@pytest.mark.asyncio
async def test_collector_loop_survives_network_errors(
    fake_redis, test_settings, monkeypatch
):
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


def test_has_precision_temp_9_digit_group():
    """9 位 T 组（如 T017201444）也应识别为含精确温度组."""
    assert _has_precision_temp(
        "METAR KSEA 230853Z 24008KT 10SM FEW020 17/14 A3000 RMK AO2 T017201444 50003"
    )


def test_has_precision_temp_negative():
    """负温度 T 组也能被识别."""
    assert _has_precision_temp(
        "METAR KSEA 230853Z 24008KT 10SM FEW020 M01/M05 A3000 RMK AO2 T10131015"
    )


def test_has_precision_temp_missing():
    """无 RMK T 组时应返回 False."""
    assert not _has_precision_temp(
        "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012"
    )


@pytest.mark.asyncio
async def test_fetch_awc_handles_rate_limit(fake_redis, test_settings):
    """测试 AWC 返回 429 时不抛异常."""
    with respx.mock:
        respx.get("https://aviationweather.gov/api/data/metar").mock(
            return_value=Response(429, text="Rate Limited")
        )
        results = await _fetch_awc_batch(["KJFK"], test_settings)
    assert results == {}


@pytest.mark.asyncio
async def test_fetch_awc_batch_skips_missing_metar_time(fake_redis, test_settings):
    """测试 AWC 返回的 rawOb 没有 ddHHMMZ 时间组时跳过该条."""
    bad_response = [
        {
            "icaoId": "KJFK",
            "rawOb": "METAR KJFK 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
            "reportTime": "2026-07-05T04:55:00Z",
            "metarType": "METAR",
        }
    ]
    with respx.mock:
        respx.get("https://aviationweather.gov/api/data/metar").mock(
            return_value=Response(200, json=bad_response)
        )
        results = await _fetch_awc_batch(["KJFK"], test_settings)

    assert "KJFK" not in results


@pytest.mark.asyncio
async def test_fetch_weathergov_batch_skips_missing_metar_time(
    fake_redis, test_settings
):
    """测试 weather.gov 返回的 rawOb 没有 ddHHMMZ 时间组时跳过该条."""
    bad_response = {
        "STATION": [
            {
                "STID": "VHHH",
                "OBSERVATIONS": {
                    "date_time": ["2026-07-05T04:50:00Z"],
                    "metar_set_1": [
                        "METAR VHHH 09010KT 10SM FEW020 28/26 Q1012 RMK AO2 T02800260"
                    ],
                    "metar_origin_set_1": [1.0],
                },
            }
        ]
    }
    test_settings.weathergov_token = "fake-token-for-test"

    with respx.mock:
        respx.get("https://api.synopticdata.com/v2/stations/timeseries").mock(
            return_value=Response(200, json=bad_response)
        )
        results = await _fetch_weathergov_batch(["VHHH"], test_settings)

    assert "VHHH" not in results


@pytest.mark.asyncio
async def test_fetch_awc_batch_uses_raw_ob_time_not_report_time(
    fake_redis, test_settings, monkeypatch
):
    """测试 AWC 使用 rawOb 中的 ddHHMMZ 作为 observed_at, 而非 reportTime."""
    from datetime import datetime, timezone

    monkeypatch.setattr(
        "app.collector._now_utc",
        lambda: datetime(2026, 7, 5, 4, 55, tzinfo=timezone.utc),
    )

    response = [
        {
            "icaoId": "KJFK",
            "rawOb": "METAR KJFK 050430Z 24008KT 10SM FEW250 25/18 A3012 RMK AO2 T02500180",
            "reportTime": "2026-07-05T04:55:00Z",
            "metarType": "METAR",
        }
    ]
    with respx.mock:
        respx.get("https://aviationweather.gov/api/data/metar").mock(
            return_value=Response(200, json=response)
        )
        results = await _fetch_awc_batch(["KJFK"], test_settings)

    assert "KJFK" in results
    assert results["KJFK"]["observed_at"] == "2026-07-05T04:30:00+00:00"


@pytest.mark.asyncio
async def test_fetch_weathergov_batch_uses_raw_ob_time_not_date_time(
    fake_redis, test_settings, monkeypatch
):
    """测试 weather.gov 使用 rawOb 中的 ddHHMMZ 作为 observed_at, 而非 date_time."""
    from datetime import datetime, timezone

    monkeypatch.setattr(
        "app.collector._now_utc",
        lambda: datetime(2026, 7, 5, 4, 55, tzinfo=timezone.utc),
    )

    response = {
        "STATION": [
            {
                "STID": "VHHH",
                "OBSERVATIONS": {
                    "date_time": ["2026-07-05T04:55:00Z"],
                    "metar_set_1": [
                        "METAR VHHH 050430Z 09010KT 10SM FEW020 28/26 Q1012 RMK AO2 T02800260"
                    ],
                    "metar_origin_set_1": [1.0],
                },
            }
        ]
    }
    test_settings.weathergov_token = "fake-token-for-test"

    with respx.mock:
        respx.get("https://api.synopticdata.com/v2/stations/timeseries").mock(
            return_value=Response(200, json=response)
        )
        results = await _fetch_weathergov_batch(["VHHH"], test_settings)

    assert "VHHH" in results
    assert results["VHHH"]["observed_at"] == "2026-07-05T04:30:00+00:00"


class TestIemLdmReader:
    """测试 IEM LDM 文件读取器."""

    def test_read_new_returns_appended_content(self, tmp_path):
        """读取追加的新内容并维护 offset."""
        file_path = tmp_path / "metars.txt"
        reader = _IemLdmReader(file_path)

        assert reader.read_new() == ""

        file_path.write_text(
            "SAXX99 KWBC 240200\nMETAR KSEA 240153Z ...=",
            encoding="utf-8",
            newline="\n",
        )
        assert reader.read_new() == "SAXX99 KWBC 240200\nMETAR KSEA 240153Z ...="

        # 模拟 LDM 追加写入（注意：write_text 会截断，不能用来模拟追加）
        with open(file_path, "a", encoding="utf-8", newline="\n") as f:
            f.write("SAXX99 KWBC 240300\nMETAR KSEA 240253Z ...=")
        assert reader.read_new() == "SAXX99 KWBC 240300\nMETAR KSEA 240253Z ...="

    def test_read_new_resets_on_truncation(self, tmp_path):
        """文件被截断（大小小于上次 offset）后从头读取."""
        file_path = tmp_path / "metars.txt"
        file_path.write_text(
            "SAXX99 KWBC 240200\nMETAR KSEA 240153Z ...=",
            encoding="utf-8",
            newline="\n",
        )
        reader = _IemLdmReader(file_path)
        reader.read_new()

        # 模拟文件被清空/截断
        file_path.write_text("truncated content", encoding="utf-8", newline="\n")

        assert reader.read_new() == "truncated content"

    def test_truncate_clears_file(self, tmp_path):
        """truncate 清空文件并重置 offset."""
        file_path = tmp_path / "metars.txt"
        file_path.write_text("some content", encoding="utf-8")
        reader = _IemLdmReader(file_path)
        reader.read_new()

        reader.truncate()
        assert file_path.read_text(encoding="utf-8") == ""
        assert reader._last_offset == 0


def test_parse_iem_bulletins_single_airport():
    """解析单个机场的 IEM bulletin."""
    text = "SAXX99 KWBC 240200\nMETAR KSEA 240153Z 22007KT 10SM FEW050 23/14 A3003 RMK AO2 T02280144="
    results = _parse_iem_bulletins(text, {"KSEA"})

    assert "KSEA" in results
    assert results["KSEA"]["icao"] == "KSEA"
    assert "METAR KSEA 240153Z" in results["KSEA"]["raw_text"]
    assert results["KSEA"]["source_key"] == "iem"
    assert results["KSEA"]["source"] == "IEM LDM"


def test_parse_iem_bulletins_multi_airport():
    """解析包含多个机场的 IEM bulletin."""
    text = (
        "SAXX99 KWBC 240200\n"
        "METAR KSEA 240153Z 22007KT 10SM 23/14 A3003 RMK AO2 T02280144 "
        "METAR KPAE 240153Z 24005KT 10SM 22/13 A3002 RMK AO2 T02200130="
    )
    results = _parse_iem_bulletins(text, {"KSEA", "KPAE"})

    assert set(results.keys()) == {"KSEA", "KPAE"}
    assert "METAR KSEA 240153Z" in results["KSEA"]["raw_text"]
    assert "METAR KPAE 240153Z" in results["KPAE"]["raw_text"]


def test_parse_iem_bulletins_skips_header_stations():
    """解析时跳过 GTS 报头中的集合中心代码，只取 METAR/SPECI 主体."""
    text = "SAXX99 KWBC 240200\nMETAR KSEA 240153Z 22007KT 10SM 23/14 A3003="
    results = _parse_iem_bulletins(text, {"KWBC", "KSEA"})

    # KWBC 是报头，不应被识别为机场
    assert "KWBC" not in results
    assert "KSEA" in results


def test_parse_iem_bulletins_prefers_later_observed_at():
    """同一机场在同一批数据中有多个报告时保留 observed_at 最新的."""
    text = (
        "SAXX99 KWBC 240200\n"
        "METAR KSEA 240153Z 22007KT 10SM 23/14 A3003 "
        "METAR KSEA 240200Z 23008KT 10SM 24/15 A3001="
    )
    results = _parse_iem_bulletins(text, {"KSEA"})

    # 240200Z 比 240153Z 晚，应保留 240200Z 这条
    assert "240200Z" in results["KSEA"]["raw_text"]
    assert "240153Z" not in results["KSEA"]["raw_text"]


@pytest.mark.asyncio
async def test_fetch_iem_batch_returns_empty_when_disabled(test_settings):
    """LDM 未启用时返回空字典."""
    test_settings.iem_ldm_enabled = False
    results = await _fetch_iem_batch(["KSEA"], test_settings)
    assert results == {}


@pytest.mark.asyncio
async def test_fetch_iem_batch_reads_file(tmp_path, test_settings, monkeypatch):
    """LDM 启用时从文件读取并解析."""
    from datetime import datetime, timezone

    monkeypatch.setattr(
        "app.collector._now_utc",
        lambda: datetime(2026, 7, 5, 4, 55, tzinfo=timezone.utc),
    )

    file_path = tmp_path / "metars.txt"
    file_path.write_text(
        "SAXX99 KWBC 240200\nMETAR KSEA 240153Z 22007KT 10SM 23/14 A3003 RMK AO2 T02280144=",
        encoding="utf-8",
    )

    test_settings.iem_ldm_enabled = True
    test_settings.iem_ldm_file_path = str(file_path)

    results = await _fetch_iem_batch(["KSEA"], test_settings)

    assert "KSEA" in results
    assert results["KSEA"]["source_key"] == "iem"


def test_select_winner_prefers_iem_when_latest():
    """三个数据源中 IEM observed_at 最新时胜出."""
    weathergov_record = {
        "icao": "KSEA",
        "observed_at": "2026-07-10T10:53:00+00:00",
        "updated_at": "2026-07-10T10:59:15+00:00",
        "source_key": "weathergov",
    }
    awc_record = {
        "icao": "KSEA",
        "observed_at": "2026-07-10T10:53:00+00:00",
        "updated_at": "2026-07-10T10:55:00+00:00",
        "source_key": "awc",
    }
    iem_record = {
        "icao": "KSEA",
        "observed_at": "2026-07-10T10:54:00+00:00",
        "updated_at": "2026-07-10T10:54:05+00:00",
        "source_key": "iem",
    }

    winner = _select_winner(weathergov_record, awc_record, iem_record)
    assert winner["source_key"] == "iem"


def test_select_winner_falls_back_when_iem_missing():
    """IEM 缺失时仍能从 weathergov/awc 中正常选择."""
    weathergov_record = {
        "icao": "KSEA",
        "observed_at": "2026-07-10T10:53:00+00:00",
        "updated_at": "2026-07-10T10:59:15+00:00",
        "source_key": "weathergov",
    }
    awc_record = {
        "icao": "KSEA",
        "observed_at": "2026-07-10T10:53:00+00:00",
        "updated_at": "2026-07-10T10:55:00+00:00",
        "source_key": "awc",
    }

    winner = _select_winner(weathergov_record, awc_record)
    assert winner["source_key"] == "awc"