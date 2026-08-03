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
      - metar_max_age_seconds -> METAR_MAX_AGE_SECONDS
      - metar_max_future_seconds -> METAR_MAX_FUTURE_SECONDS
      - weathergov_token -> WEATHERGOV_TOKEN
      - http_timeout -> HTTP_TIMEOUT
      - log_level -> LOG_LEVEL
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # IEM LDM 配置
    iem_ldm_enabled: bool = Field(
        default=False,
        description="是否启用 IEM LDM GTS 数据流采集",
    )

    iem_ldm_file_path: str = Field(
        default="/app/ldm_data/metar/metars.txt",
        description="LDM 写入的 METAR 文件路径（M2 容器内路径）",
    )

    iem_ldm_truncate_interval_hours: int = Field(
        default=1,
        ge=1,
        description="METAR 文件截断清理间隔（小时）",
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
        default=1.0,
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

    # METAR 观测时间新鲜度窗口：过旧或未来的报文不进入 Redis
    metar_max_age_seconds: int = Field(
        default=7200,
        ge=60,
        description="允许接收的最陈旧 METAR 观测时间（秒），超过则丢弃",
    )

    metar_max_future_seconds: int = Field(
        default=600,
        ge=0,
        description="允许接收的最超前 METAR 观测时间（秒），超过则丢弃",
    )

    # weather.gov / SynopticData 独立 Token（可选）
    weathergov_token: str = Field(
        default="",
        description="可选的 SynopticData 独立 Token；未配置时抓取 weather.gov 内嵌 Token",
    )

    # Skyviewor fast-METAR WebSocket 数据源
    skyviewor_enabled: bool = Field(
        default=False,
        description="是否启用 Skyviewor fast-METAR WebSocket 数据源",
    )

    skyviewor_api_key: str = Field(
        default="",
        description="Skyviewor 长期 API Key，用于换取临时 WebSocket Token",
    )

    skyviewor_ws_url: str = Field(
        default="wss://special.data-api.skyviewor.host/fast-metar/ws/raw",
        description="Skyviewor fast-METAR WebSocket 地址",
    )

    skyviewor_token_url: str = Field(
        default="https://special.data-api.skyviewor.host/fast-metar/auth/token",
        description="Skyviewor Token 换取地址",
    )

    skyviewor_airports: str = Field(
        default="ZBAA,ZGGG,ZSPD,ZUCK,ZUUU,ZHHH,ZSQD",
        description="Skyviewor 订阅的中国机场 ICAO 代码列表",
    )

    skyviewor_trusted_half_hour_airports: str = Field(
        default="ZBAA,ZGGG,ZSPD",
        description="Skyviewor 中 :00 和 :30 都采信的机场",
    )

    skyviewor_trusted_speci_airports: str = Field(
        default="ZBAA,ZGGG",
        description="Skyviewor 中 SPECI 采信的机场",
    )

    skyviewor_audit_retention_days: int = Field(
        default=7,
        ge=1,
        description="Skyviewor 未采信数据审计记录保留天数",
    )

    skyviewor_reconnect_min_seconds: float = Field(
        default=1.0,
        ge=0.1,
        description="Skyviewor 断线重连最小等待秒数",
    )

    skyviewor_reconnect_max_seconds: float = Field(
        default=60.0,
        ge=1.0,
        description="Skyviewor 断线重连最大等待秒数",
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
        return [
            code.strip() for code in self.monitor_airports.split(",") if code.strip()
        ]

    @computed_field
    @property
    def skyviewor_airports_list(self) -> List[str]:
        """返回 Skyviewor 订阅机场列表（List[str] 形式）."""
        return [
            code.strip().upper()
            for code in self.skyviewor_airports.split(",")
            if code.strip()
        ]

    @computed_field
    @property
    def skyviewor_trusted_half_hour_airports_set(self) -> set[str]:
        """返回 Skyviewor 半点采信机场集合."""
        return {
            code.strip().upper()
            for code in self.skyviewor_trusted_half_hour_airports.split(",")
            if code.strip()
        }

    @computed_field
    @property
    def skyviewor_trusted_speci_airports_set(self) -> set[str]:
        """返回 Skyviewor SPECI 采信机场集合."""
        return {
            code.strip().upper()
            for code in self.skyviewor_trusted_speci_airports.split(",")
            if code.strip()
        }

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()


@lru_cache
def get_settings() -> Settings:
    """返回缓存的 Settings 实例，避免重复解析环境变量."""
    return Settings()
