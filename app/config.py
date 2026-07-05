"""环境变量与配置管理 (Pydantic Settings)."""

from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field, RedisDsn, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# M1 项目中的 49 个默认机场，以逗号分隔字符串存储，
# 避免 pydantic-settings 对 list 类型进行自动 JSON 解析。
_DEFAULT_MONITOR_AIRPORTS = (
    "ZSPD,ZBAA,EGLC,RKSI,WSSS,LFPB,KSEA,KATL,"
    "KORD,KLGA,RJTT,KDAL,ZUUU,ZUCK,ZGGG,ZGSZ,"
    "ZHHH,ZSQD,EPWA,WMKK,RCSS,LLBG,RKPK,LIMC,"
    "KMIA,NZWN,KBKF,RPLL,CYYZ,SBGR,EDDM,KLAX,"
    "KAUS,EHAM,SAEZ,KSFO,LTAC,LTFM,FACT,VILK,"
    "OPKC,MPMG,KHOU,MMMX,OEJN,EFHK,LEMD,VHHH,UUWW"
)


class Settings(BaseSettings):
    """应用配置，所有字段均可通过环境变量覆盖.

    环境变量名与字段名对应关系（不区分大小写）:
      - redis_url -> REDIS_URL
      - monitor_airports -> MONITOR_AIRPORTS（逗号分隔 ICAO 代码）
      - poll_interval_seconds -> POLL_INTERVAL_SECONDS
      - user_agent -> USER_AGENT
      - metar_ttl_seconds -> METAR_TTL_SECONDS
      - weathergov_token -> WEATHERGOV_TOKEN
      - http_timeout -> HTTP_TIMEOUT
      - log_level -> LOG_LEVEL
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Redis 连接地址
    redis_url: RedisDsn = Field(
        default="redis://localhost:6379/0",
        description="Redis 连接 URL",
    )

    # 监控机场列表，以逗号分隔的大写 ICAO 代码字符串存储
    monitor_airports: str = Field(
        default=_DEFAULT_MONITOR_AIRPORTS,
        description="需要监控的 ICAO 机场代码列表（逗号分隔）",
    )

    # 轮询间隔，规范要求 1~2 秒
    poll_interval_seconds: float = Field(
        default=1.5,
        ge=1.0,
        le=2.0,
        description="采集器轮询间隔（秒），范围 1.0~2.0",
    )

    # HTTP 请求 User-Agent，必须包含可联系邮箱以规避 403
    user_agent: str = Field(
        default="MyMetarApp/1.0 (htsao2000@gmail.com)",
        description="请求头 User-Agent",
    )

    # Redis Key TTL，规范要求 7200 秒（2 小时）
    metar_ttl_seconds: int = Field(
        default=7200,
        ge=60,
        description="METAR 数据在 Redis 中的 TTL（秒）",
    )

    # weather.gov / SynopticData 独立 Token（可选）
    weathergov_token: str = Field(
        default="",
        description="可选的 SynopticData 独立 Token；未配置时抓取 weather.gov 内嵌 Token",
    )

    # HTTP 请求超时
    http_timeout: float = Field(
        default=15.0,
        gt=0,
        description="HTTP 请求超时时间（秒）",
    )

    # 日志级别
    log_level: str = Field(
        default="INFO",
        description="日志级别",
    )

    @field_validator("monitor_airports")
    @classmethod
    def _normalize_monitor_airports(cls, value: str) -> str:
        """规范化机场列表：大写、去空格、去空项、逗号连接."""
        if isinstance(value, str):
            codes = [code.strip().upper() for code in value.split(",") if code.strip()]
            return ",".join(codes)
        if isinstance(value, list):
            codes = [str(code).strip().upper() for code in value if str(code).strip()]
            return ",".join(codes)
        raise ValueError("monitor_airports must be a comma-separated string or list")

    @computed_field
    @property
    def monitor_airports_list(self) -> List[str]:
        """返回监控机场列表（List[str] 形式）."""
        return [code.strip() for code in self.monitor_airports.split(",") if code.strip()]

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()


@lru_cache
def get_settings() -> Settings:
    """返回缓存的 Settings 实例，避免重复解析环境变量."""
    return Settings()
