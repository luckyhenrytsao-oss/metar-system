"""M2 延迟测量日志写入器.

按 T0TX-M2 延迟测量设计文档 (v1.1) 实现:
- 异步/缓冲写入 JSONL 延迟日志
- 单文件大小轮转 (默认 50MB, 保留 3 个 gzip 备份)
- 仅记录 7 个中国机场事件, 控制日志量
- 写日志失败不影响主业务流
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# 第一阶段仅关注中国机场 (与 Skyviewor 订阅列表一致)
CHINA_AIRPORTS = {"ZBAA", "ZGGG", "ZSPD", "ZUCK", "ZUUU", "ZHHH", "ZSQD"}


def _settings() -> Settings:
    """获取当前 Settings 实例."""
    return get_settings()


async def log_latency(record: dict[str, Any]) -> None:
    """写入一条延迟测量记录.

    - 仅当日志启用且 ICAO 在中国机场列表中时才写入
    - 使用 asyncio.to_thread() 包装同步写文件, 避免阻塞事件循环
    - 写失败仅记录 warning, 不抛异常, 不影响主流程
    """
    cfg = _settings()
    if not cfg.latency_log_enabled:
        return

    icao = str(record.get("icao", "")).upper()
    if icao not in CHINA_AIRPORTS:
        return

    await asyncio.to_thread(
        _append_jsonl,
        cfg.latency_log_path,
        record,
        cfg.latency_log_max_bytes,
        cfg.latency_log_backup_count,
    )


def _append_jsonl(
    path: str,
    record: dict[str, Any],
    max_bytes: int,
    backup_count: int,
) -> None:
    """同步追加写入 JSONL 并触发轮转."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        record["logged_at"] = datetime.now(timezone.utc).isoformat()
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _maybe_rotate(path, max_bytes, backup_count)
    except Exception as exc:
        logger.warning("Failed to write latency log: %s", exc)


def _maybe_rotate(
    path: str,
    max_bytes: int,
    backup_count: int,
) -> None:
    """按文件大小轮转日志.

    当文件超过 max_bytes 时:
      - 将现有备份 .i.gz 后移为 .(i+1).gz
      - 将当前文件压缩为 .1.gz
      - 删除当前文件
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size < max_bytes:
        return

    try:
        for i in range(backup_count - 1, 0, -1):
            src = Path(f"{path}.{i}.gz")
            dst = Path(f"{path}.{i + 1}.gz")
            if src.exists():
                shutil.move(src, dst)
        with open(path, "rb") as f_in, gzip.open(f"{path}.1.gz", "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        p.unlink()
    except Exception as exc:
        logger.warning("Failed to rotate latency log: %s", exc)
