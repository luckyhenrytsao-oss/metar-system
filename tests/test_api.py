"""FastAPI 接口测试（Mock Redis 与测试 HTTP 304）."""

from __future__ import annotations


from app.database import add_source_history, set_metar


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


class TestHealthEndpoint:
    """测试健康检查接口."""

    def test_health_check(self, test_client):
        response = test_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestSourcesHistoryEndpoint:
    """测试 GET /api/v1/metar/sources/history 接口."""

    def test_history_requires_icao(self, test_client):
        """缺少 icao 参数应返回 422."""
        response = test_client.get("/api/v1/metar/sources/history")
        assert response.status_code == 422

    def test_history_unmonitored_airport_returns_404(self, test_client):
        """未监控的机场应返回 404."""
        response = test_client.get("/api/v1/metar/sources/history?icao=ZZZZ&hours=4")
        assert response.status_code == 404

    def test_history_requires_hours_or_start(self, test_client):
        """必须提供 hours 或 start."""
        response = test_client.get("/api/v1/metar/sources/history?icao=KJFK")
        assert response.status_code == 422
        assert "必须提供 hours 或 start 参数" in response.json()["detail"]

    def test_history_hours_and_start_mutually_exclusive(self, test_client):
        """hours 与 start/end 不能同时提供."""
        response = test_client.get(
            "/api/v1/metar/sources/history?icao=KJFK&hours=4&start=2026-07-05T00:00:00Z"
        )
        assert response.status_code == 422

    def test_history_returns_records_grouped_by_observed_at(self, test_client, fake_redis):
        """按 observed_at 分组返回两条 METAR 历史记录."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)

        # 第一条 METAR：1 小时前观测
        wg_1 = {
            "icao": "KJFK",
            "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
            "observed_at": (now - timedelta(hours=1)).isoformat(),
            "updated_at": (now - timedelta(hours=0, minutes=59, seconds=55)).isoformat(),
            "hash": "wg-hash-1",
            "source": "weather.gov",
            "source_key": "weathergov",
        }
        awc_1 = {
            "icao": "KJFK",
            "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
            "observed_at": (now - timedelta(hours=1)).isoformat(),
            "updated_at": (now - timedelta(hours=0, minutes=59, seconds=50)).isoformat(),
            "hash": "awc-hash-1",
            "source": "aviationweather.gov",
            "source_key": "awc",
        }

        # 第二条 METAR：30 分钟前观测
        wg_2 = {
            "icao": "KJFK",
            "raw_text": "SPECI KJFK 050500Z 25012KT 10SM FEW250 24/17 A3010 RMK AO2 T02400170",
            "observed_at": (now - timedelta(minutes=30)).isoformat(),
            "updated_at": (now - timedelta(minutes=28, seconds=30)).isoformat(),
            "hash": "wg-hash-2",
            "source": "weather.gov",
            "source_key": "weathergov",
        }
        awc_2 = {
            "icao": "KJFK",
            "raw_text": "SPECI KJFK 050500Z 25012KT 10SM FEW250 24/17 A3010 RMK AO2 T02400170",
            "observed_at": (now - timedelta(minutes=30)).isoformat(),
            "updated_at": (now - timedelta(minutes=28, seconds=40)).isoformat(),
            "hash": "awc-hash-2",
            "source": "aviationweather.gov",
            "source_key": "awc",
        }

        for record in [wg_1, awc_1, wg_2, awc_2]:
            asyncio.run(add_source_history(fake_redis, "KJFK", record["source_key"], record))

        response = test_client.get("/api/v1/metar/sources/history?icao=KJFK&hours=24")
        assert response.status_code == 200
        result = response.json()

        assert result["icao"] == "KJFK"
        assert result["count"] == 2

        # 按 observed_at 降序
        assert result["records"][0]["observed_at"] == wg_2["observed_at"]
        assert result["records"][1]["observed_at"] == wg_1["observed_at"]

        # 第一条 AWC 更快（updated_at 更早）
        rec_0 = result["records"][0]
        assert rec_0["winner"]["source_key"] == "awc"
        assert rec_0["awc"]["updated_at"] == awc_2["updated_at"]
        assert rec_0["weathergov"]["updated_at"] == wg_2["updated_at"]

        # 第二条 weather.gov 更快（updated_at 更早）
        rec_1 = result["records"][1]
        assert rec_1["winner"]["source_key"] == "weathergov"
        assert rec_1["weathergov"]["updated_at"] == wg_1["updated_at"]
        assert rec_1["awc"]["updated_at"] == awc_1["updated_at"]

    def test_history_returns_null_for_missing_source(self, test_client, fake_redis):
        """某数据源缺失时返回 null 而不是整条记录缺失."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        awc_record = {
            "icao": "KJFK",
            "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
            "observed_at": (now - timedelta(hours=1)).isoformat(),
            "updated_at": (now - timedelta(minutes=59, seconds=55)).isoformat(),
            "hash": "awc-hash-1",
            "source": "aviationweather.gov",
            "source_key": "awc",
        }
        asyncio.run(add_source_history(fake_redis, "KJFK", "awc", awc_record))

        response = test_client.get("/api/v1/metar/sources/history?icao=KJFK&hours=24")
        assert response.status_code == 200
        result = response.json()
        assert result["count"] == 1
        assert result["records"][0]["weathergov"] is None
        assert result["records"][0]["awc"] is not None
        assert result["records"][0]["winner"]["source_key"] == "awc"

    def test_history_time_window_filter(self, test_client, fake_redis):
        """start/end 时间窗口过滤应生效."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        awc_record = {
            "icao": "KJFK",
            "raw_text": "METAR KJFK 050455Z 24008KT 10SM FEW250 25/18 A3012",
            "observed_at": (now - timedelta(hours=1)).isoformat(),
            "updated_at": (now - timedelta(minutes=59, seconds=55)).isoformat(),
            "hash": "awc-hash-1",
            "source": "aviationweather.gov",
            "source_key": "awc",
        }
        asyncio.run(add_source_history(fake_redis, "KJFK", "awc", awc_record))

        start = (now - timedelta(hours=2)).isoformat()
        end = now.isoformat()
        import urllib.parse

        response = test_client.get(
            "/api/v1/metar/sources/history?icao=KJFK"
            f"&start={urllib.parse.quote(start)}"
            f"&end={urllib.parse.quote(end)}"
        )
        assert response.status_code == 200
        result = response.json()
        assert result["count"] == 1

        start = (now - timedelta(minutes=10)).isoformat()
        end = now.isoformat()
        response = test_client.get(
            "/api/v1/metar/sources/history?icao=KJFK"
            f"&start={urllib.parse.quote(start)}"
            f"&end={urllib.parse.quote(end)}"
        )
        assert response.status_code == 200
        result = response.json()
        assert result["count"] == 0
