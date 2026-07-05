"""FastAPI 接口测试（Mock Redis 与测试 HTTP 304）."""

from __future__ import annotations

import pytest

from app.database import set_metar


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


class TestHealthEndpoint:
    """测试健康检查接口."""

    def test_health_check(self, test_client):
        response = test_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
