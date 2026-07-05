"""M2 批量温度客户端（参考实现）.

这个文件是 M2 对外提供的接口调用示例，供其他项目（如 M1）参考实现。
不要直接复制到其他项目里运行；应由目标项目按自己的工程规范封装。

调用接口：
    POST http://47.251.25.183/api/v1/metar/batch
    Content-Type: application/json

    {"icaos": ["VHHH", "KSEA", "EGLL", "ZSPD"]}

响应：
    {
      "data": [
        {
          "icao": "VHHH",
          "temperature_c": 30.0,
          "dewpoint_c": 26.0,
          "raw_text": "VHHH 051330Z ...",
          "observed_at": "2026-07-05T13:30:00+00:00",
          "updated_at": "2026-07-05T13:30:00+00:00",
          "source": "weather.gov"
        }
      ],
      "missing": ["EGLL"],
      "count": 1
    }
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class MetarClient:
    """M2 批量温度客户端（参考实现）."""

    def __init__(self, base_url: str = "http://47.251.25.183", timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    def fetch_temperatures(self, icaos: list[str]) -> list[dict[str, Any]]:
        """批量获取指定机场的温度记录."""
        if not icaos:
            return []

        codes = [code.upper().strip() for code in icaos if code.strip()]
        url = f"{self.base_url}/api/v1/metar/batch"

        try:
            resp = self._session.post(
                url,
                json={"icaos": codes},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.error("M2 batch request failed: %s", exc)
            return []
        except ValueError as exc:
            logger.error("M2 invalid JSON response: %s", exc)
            return []

        if data.get("missing"):
            logger.warning("M2 missing airports: %s", data["missing"])

        records = data.get("data", [])
        logger.info("M2 fetched temperatures for %d/%d airports", len(records), len(codes))
        return records

    def fetch_temperatures_for_cities(
        self,
        city_icao_map: dict[str, str],
    ) -> list[dict[str, Any]]:
        """以 {城市名: ICAO} 映射批量获取温度，并在结果中追加 city 字段."""
        icao_to_city = {icao.upper(): city for city, icao in city_icao_map.items()}
        records = self.fetch_temperatures(list(icao_to_city.keys()))
        for record in records:
            record["city"] = icao_to_city.get(record["icao"], record["icao"])
        return records

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "MetarClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


if __name__ == "__main__":
    client = MetarClient()
    city_icao_map = {
        "Shanghai": "ZSPD",
        "Beijing": "ZBAA",
        "Hong Kong": "VHHH",
        "New York": "KLGA",
        "London": "EGLC",
        "Singapore": "WSSS",
        "Tokyo": "RJTT",
        "Seattle": "KSEA",
    }

    records = client.fetch_temperatures_for_cities(city_icao_map)
    print(f"成功获取 {len(records)}/{len(city_icao_map)} 个城市")
    for record in records:
        print(
            f"{record['city']:12s} {record['icao']:5s} "
            f"T={record['temperature_c']:6.1f}°C "
            f"Td={record['dewpoint_c']:6.1f}°C "
            f"obs={record['observed_at']}"
        )
