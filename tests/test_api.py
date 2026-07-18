"""FastAPI 接口测试（Mock Redis 与测试 HTTP 304）."""

from __future__ import annotations


import asyncio
import json

import pytest

import app.main
from app.database import set_metar
from app.events import publish_event, subscribe, unsubscribe
from app.main import _event_stream


class TestMetarEndpoint:
    """测试 GET /api/v1/metar 接口."""

    def test_missing_icao_returns_422(self, test_client):
        """缺少 icao 参数应返回 422."""
        response = test_client.get("/api/v1/metar")
        assert response.status_code == 422

    def test_unmonitored_airport_returns_404(self, test_client):
        """未监控的机场应返回 404."""
        response = test_client.get("/api/v1/metar?icao=ZZZZ")
        assert response.status_code == 404
        assert "not in the monitored airport list" in response.json()["detail"]

    def test_no_data_returns_404(self, test_client):
        """Redis 中没有数据时应返回 404."""
        response = test_client.get("/api/v1/metar?icao=KJFK")
        assert response.status_code == 404
        assert "No METAR data available" in response.json()["detail"]

    def test_returns_200_with_etag(self, test_client, fake_redis):
        """正常请求返回 200 和 ETag."""
        payload = {
            "icao": "KJFK",
            "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
            "observed_at": "2026-07-05T04:55:00+00:00",
            "updated_at": "2026-07-05T04:55:00+00:00",
            "hash": "abc123def456",
            "source": "aviationweather.gov",
        }
        # 注意：fake_redis fixture 在 conftest 中已替换全局连接池
        import asyncio

        asyncio.run(set_metar(fake_redis, "KJFK", payload, 7200))

        response = test_client.get("/api/v1/metar?icao=KJFK")
        assert response.status_code == 200
        data = response.json()
        assert data["icao"] == "KJFK"
        assert "ETag" in response.headers
        assert response.headers["ETag"] == '"abc123def456"'

    def test_304_not_modified(self, test_client, fake_redis):
        """If-None-Match 匹配时返回 304 空 Body."""
        payload = {
            "icao": "KJFK",
            "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
            "observed_at": "2026-07-05T04:55:00+00:00",
            "updated_at": "2026-07-05T04:55:00+00:00",
            "hash": "abc123def456",
            "source": "aviationweather.gov",
        }
        import asyncio

        asyncio.run(set_metar(fake_redis, "KJFK", payload, 7200))

        response = test_client.get(
            "/api/v1/metar?icao=KJFK",
            headers={"If-None-Match": '"abc123def456"'},
        )
        assert response.status_code == 304
        assert response.content == b""

    def test_etag_mismatch_returns_200(self, test_client, fake_redis):
        """If-None-Match 不匹配时返回 200."""
        payload = {
            "icao": "KJFK",
            "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
            "observed_at": "2026-07-05T04:55:00+00:00",
            "updated_at": "2026-07-05T04:55:00+00:00",
            "hash": "abc123def456",
            "source": "aviationweather.gov",
        }
        import asyncio

        asyncio.run(set_metar(fake_redis, "KJFK", payload, 7200))

        response = test_client.get(
            "/api/v1/metar?icao=KJFK",
            headers={"If-None-Match": '"oldhash"'},
        )
        assert response.status_code == 200
        assert response.json()["icao"] == "KJFK"


class TestBatchMetarEndpoint:
    """测试 POST /api/v1/metar/batch 接口."""

    def test_batch_returns_temperatures(self, test_client, fake_redis):
        """批量请求返回可解析温度的机场."""
        payloads = [
            {
                "icao": "KJFK",
                "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
                "observed_at": "2026-07-05T04:55:00+00:00",
                "updated_at": "2026-07-05T04:55:00+00:00",
                "hash": "abc123def456",
                "source": "aviationweather.gov",
            },
            {
                "icao": "EGLL",
                "raw_text": "METAR EGLL 050455Z 24008KT 10SM FEW250 13/10 A3012 RMK AO2 T01330100",
                "observed_at": "2026-07-05T04:55:00+00:00",
                "updated_at": "2026-07-05T04:55:00+00:00",
                "hash": "def789abc012",
                "source": "weather.gov",
            },
        ]
        import asyncio

        for payload in payloads:
            asyncio.run(set_metar(fake_redis, payload["icao"], payload, 7200))

        response = test_client.post(
            "/api/v1/metar/batch",
            json={"icaos": ["KJFK", "EGLL", "ZZZZ"]},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["count"] == 2
        assert set(item["icao"] for item in result["data"]) == {"KJFK", "EGLL"}
        assert "ZZZZ" in result["missing"]

        kjfk = next(item for item in result["data"] if item["icao"] == "KJFK")
        assert kjfk["temperature_c"] == 25.0
        assert kjfk["dewpoint_c"] == 18.0

        egll = next(item for item in result["data"] if item["icao"] == "EGLL")
        assert egll["temperature_c"] == 13.3
        assert egll["dewpoint_c"] == 10.0

    def test_batch_ignores_unmonitored_and_no_data(self, test_client, fake_redis):
        """未监控和无数据的机场进入 missing 列表."""
        import asyncio

        asyncio.run(
            set_metar(
                fake_redis,
                "KJFK",
                {
                    "icao": "KJFK",
                    "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
                    "observed_at": "2026-07-05T04:55:00+00:00",
                    "updated_at": "2026-07-05T04:55:00+00:00",
                    "hash": "abc123def456",
                    "source": "aviationweather.gov",
                },
                7200,
            )
        )

        response = test_client.post(
            "/api/v1/metar/batch",
            json={"icaos": ["KJFK", "NODATA"]},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["count"] == 1
        assert result["missing"] == ["NODATA"]


class TestEventBus:
    """测试内存事件总线."""

    @pytest.mark.asyncio
    async def test_publish_and_subscribe(self):
        """订阅者能收到发布的事件."""
        queue = subscribe()
        try:
            event = {"event_type": "winner_update", "icao": "KJFK", "temp": 25.0}
            await publish_event(event)
            received = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert received == event
        finally:
            unsubscribe(queue)

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_receiving(self):
        """取消订阅后不再收到事件."""
        queue = subscribe()
        unsubscribe(queue)
        await publish_event({"event_type": "winner_update", "icao": "KJFK"})
        assert queue.empty()


class TestMetarStreamEndpoint:
    """测试 GET /api/v1/metar/stream SSE 接口."""

    @pytest.mark.asyncio
    async def test_event_stream_yields_snapshot(self, fake_redis):
        """_event_stream 生成器在连接后先发送 snapshot."""
        await set_metar(
            fake_redis,
            "KJFK",
            {
                "icao": "KJFK",
                "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
                "observed_at": "2026-07-05T04:55:00+00:00",
                "updated_at": "2026-07-05T04:55:00+00:00",
                "hash": "abc123def456",
                "source": "aviationweather.gov",
                "source_key": "awc",
            },
            7200,
        )

        gen = _event_stream({"KJFK"}, fake_redis, heartbeat_interval=0.05)
        chunks = []
        async for chunk in gen:
            chunks.append(chunk)
            if "event: snapshot" in chunk:
                break

        snapshot_text = "".join(chunks)
        assert "event: snapshot" in snapshot_text
        assert '"icao": "KJFK"' in snapshot_text
        assert "25.0" in snapshot_text

    @pytest.mark.asyncio
    async def test_event_stream_yields_published_event(self, fake_redis):
        """_event_stream 能收到后续发布的事件."""
        await set_metar(
            fake_redis,
            "KJFK",
            {
                "icao": "KJFK",
                "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
                "observed_at": "2026-07-05T04:55:00+00:00",
                "updated_at": "2026-07-05T04:55:00+00:00",
                "hash": "abc123def456",
                "source": "aviationweather.gov",
                "source_key": "awc",
            },
            7200,
        )

        gen = _event_stream({"KJFK"}, fake_redis, heartbeat_interval=0.05)

        # 消费 snapshot
        async for chunk in gen:
            if "event: snapshot" in chunk:
                break

        # 发布事件
        event = {
            "event_type": "winner_update",
            "icao": "KJFK",
            "raw_text": "METAR KJFK 050500Z 25012KT 10SM FEW250 24/17 A3010",
            "hash": "newhash123",
            "previous_hash": "abc123def456",
            "source": "weather.gov",
            "source_key": "weathergov",
            "observed_at": "2026-07-05T05:00:00+00:00",
            "updated_at": "2026-07-05T05:00:01+00:00",
        }
        await publish_event(event)

        found = False
        async for chunk in gen:
            if "winner_update" in chunk and "newhash123" in chunk:
                found = True
                break
        assert found, "SSE stream should receive published winner_update event"

    def test_stream_filters_unknown_icao(self, test_client):
        """请求未监控的机场返回 404."""
        response = test_client.get("/api/v1/metar/stream?icaos=ZZZZ")
        assert response.status_code == 404
