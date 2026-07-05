"""Pytest 全局配置与共享 fixtures."""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Generator

import fakeredis.aioredis
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import app.database
import app.main
from app.config import Settings, get_settings
from app.database import _redis_pool, close_redis


# 设置默认事件循环策略为 asyncio（pytest-asyncio 0.23+ 推荐）
@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


def pytest_configure(config):
    """pytest 全局配置."""
    config.addinivalue_line("markers", "asyncio: marks tests as async")


@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[fakeredis.aioredis.FakeRedis, None]:
    """提供一个独立的 fakeredis 实例."""
    # 关闭可能存在的真实连接池
    await close_redis()

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    # 替换全局连接池
    global _redis_pool
    _redis_pool = fake

    yield fake

    await fake.flushall()
    await fake.aclose()
    _redis_pool = None


@pytest.fixture
def test_settings(monkeypatch) -> Settings:
    """返回测试专用配置，只监控少量机场以加速测试."""
    monkeypatch.setenv("MONITOR_AIRPORTS", "KJFK,VHHH,EGLL")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "1.0")
    monkeypatch.setenv("METAR_TTL_SECONDS", "7200")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/9")

    # 清除缓存的 Settings
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def test_client(fake_redis, test_settings, monkeypatch) -> TestClient:
    """创建 FastAPI TestClient，使用 FakeRedis 并关闭 lifespan 避免真实网络请求.

    - 通过 monkeypatch 让 app.main/app.database 中的 get_redis 返回 fake_redis
    - lifespan="off" 防止后台采集器在测试期间连接真实数据源
    """
    # 注入 FakeRedis 到 lifespan 中直接调用的 get_redis（允许接收 settings 参数）
    async def _fake_get_redis_lifespan(*args, **kwargs):
        return fake_redis

    monkeypatch.setattr(app.main, "get_redis", _fake_get_redis_lifespan)
    monkeypatch.setattr(app.database, "get_redis", _fake_get_redis_lifespan)

    # 关闭后台采集器，避免测试期间发起真实网络请求
    async def _fake_collector(*args, **kwargs):
        while True:
            await asyncio.sleep(3600)

    monkeypatch.setattr(app.main, "start_collector_loop", _fake_collector)

    # FastAPI 依赖注入需要无参数函数签名
    async def _fake_get_redis_dep():
        return fake_redis

    app.main.app.dependency_overrides[app.main._get_redis_dependency] = _fake_get_redis_dep

    with TestClient(app.main.app) as client:
        yield client

    app.main.app.dependency_overrides.clear()
