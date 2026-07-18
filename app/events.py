"""M2 内部事件总线.

用于将采集器产生的新 METAR 数据事件广播给 SSE 等长连接消费者.
当前为单进程内存实现; M2 部署为单容器单进程, 足够使用.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 订阅者队列集合; 每个 SSE 连接对应一个 asyncio.Queue
_subscribers: set[asyncio.Queue] = set()


def subscribe(maxsize: int = 100) -> asyncio.Queue:
    """创建并注册一个事件队列, 返回给 SSE 等消费者使用."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    _subscribers.add(queue)
    logger.debug("New event subscriber added, total=%d", len(_subscribers))
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    """注销事件队列."""
    _subscribers.discard(queue)
    logger.debug("Event subscriber removed, total=%d", len(_subscribers))


async def publish_event(event: dict[str, Any]) -> None:
    """发布事件到所有订阅者.

    非阻塞: 如果某个订阅者队列已满, 直接丢弃该订阅者的旧事件,
    避免采集器被慢消费者拖慢.
    """
    if not _subscribers:
        return

    dead: set[asyncio.Queue] = set()
    for queue in _subscribers:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Event subscriber queue full, dropping event")
            dead.add(queue)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to publish event to subscriber: %s", exc)
            dead.add(queue)

    for queue in dead:
        unsubscribe(queue)


def subscriber_count() -> int:
    """返回当前订阅者数量."""
    return len(_subscribers)
