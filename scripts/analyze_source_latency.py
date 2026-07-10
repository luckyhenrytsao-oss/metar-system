"""分析 VPS 上过去 1 小时采集的 METAR 数据，比较 weather.gov 与 AWC 的采集延迟。

逻辑：
- 通过 SSH 直连 VPS Redis，拉取所有 metar:* 当前最新记录。
- 按 updated_at 过滤过去 1 小时内的记录。
- 计算 latency = updated_at - observed_at（即 METAR 发出后到 M2 入库的延迟）。
- 按数据源（weather.gov / aviationweather.gov）做统计对比，并输出 Excel。

注意：M2 当前采集策略是 weather.gov 优先、AWC 兜底，因此 Redis 中每个机场
通常只保留一个来源的最新记录。本脚本按"实际入库来源"分组比较延迟分布，
若要严格同机场双源对比，需要临时修改采集器同时写入两个来源。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# 配置
VPS_HOST = "47.251.25.183"
VPS_PORT = "2222"
VPS_USER = "root"
SSH_KEY = r"C:\Users\Henry\.ssh\id_ed25519"
REDIS_KEY_PATTERN = "metar:*"
PAST_HOURS = 1


def ssh_command(cmd: str) -> str:
    """通过 SSH 在 VPS 上执行命令并返回 stdout。"""
    full_cmd = [
        "ssh",
        "-i", SSH_KEY,
        "-p", VPS_PORT,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"{VPS_USER}@{VPS_HOST}",
        cmd,
    ]
    result = subprocess.run(full_cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        print("SSH 命令失败:", result.stderr, file=sys.stderr)
        raise RuntimeError(f"SSH failed: {result.stderr}")
    return result.stdout


def fetch_all_metar_records() -> list[dict]:
    """从 VPS Redis 拉取所有 metar:* 记录。"""
    keys_json = ssh_command(
        f"docker exec metar-redis redis-cli --json KEYS '{REDIS_KEY_PATTERN}'"
    )
    keys = json.loads(keys_json) if keys_json.strip() else []
    if not keys:
        return []

    # 分批 MGET，避免命令行过长
    batch_size = 30
    records: list[dict] = []
    for i in range(0, len(keys), batch_size):
        batch = keys[i : i + batch_size]
        # 构造 redis-cli MGET 参数
        args = " ".join(f"'{k}'" for k in batch)
        values_json = ssh_command(
            f"docker exec metar-redis redis-cli --json MGET {args}"
        )
        values = json.loads(values_json) if values_json.strip() else []
        for key, value in zip(batch, values):
            if not value:
                continue
            try:
                record = json.loads(value)
                record["redis_key"] = key
                records.append(record)
            except json.JSONDecodeError:
                print(f"无法解析 {key} 的值，已跳过", file=sys.stderr)

    return records


def parse_iso(value: str) -> datetime:
    """解析 ISO 8601 时间字符串为 UTC datetime。"""
    # pandas 兼容：去掉可能存在的 6 位微秒后的多余精度
    value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value)


def analyze(records: list[dict], hours: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """分析数据并返回多个 DataFrame。"""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    rows = []
    for r in records:
        try:
            updated_at = parse_iso(r["updated_at"])
            observed_at = parse_iso(r["observed_at"])
        except (KeyError, ValueError, TypeError) as exc:
            print(f"记录 {r.get('redis_key')} 时间解析失败: {exc}", file=sys.stderr)
            continue

        if updated_at < cutoff:
            continue

        latency = updated_at - observed_at
        rows.append(
            {
                "icao": r.get("icao", ""),
                "source": r.get("source", "unknown"),
                "raw_text": r.get("raw_text", ""),
                "observed_at": observed_at,
                "updated_at": updated_at,
                "latency_seconds": latency.total_seconds(),
                "latency_human": str(latency),
            }
        )

    if not rows:
        raise ValueError("过去 1 小时内没有采集到任何 METAR 记录")

    df = pd.DataFrame(rows)
    df = df.sort_values(["source", "latency_seconds"], ascending=[True, True])

    # 按来源汇总
    summary = (
        df.groupby("source")
        .agg(
            count=("icao", "count"),
            avg_latency_s=("latency_seconds", "mean"),
            median_latency_s=("latency_seconds", "median"),
            min_latency_s=("latency_seconds", "min"),
            max_latency_s=("latency_seconds", "max"),
            p95_latency_s=("latency_seconds", lambda x: x.quantile(0.95)),
            airports=("icao", lambda x: ", ".join(sorted(set(x)))),
        )
        .reset_index()
    )
    summary["avg_latency_human"] = summary["avg_latency_s"].apply(
        lambda s: str(timedelta(seconds=round(s, 3)))[:-3]
    )
    summary["median_latency_human"] = summary["median_latency_s"].apply(
        lambda s: str(timedelta(seconds=round(s, 3)))[:-3]
    )

    # 机场维度：仅保留有多个来源记录的机场做同场对比（在当前策略下极少出现）
    airport_groups = df.groupby(["icao", "source"]).agg(
        count=("latency_seconds", "count"),
        min_latency_s=("latency_seconds", "min"),
        avg_latency_s=("latency_seconds", "mean"),
        max_latency_s=("latency_seconds", "max"),
        observed_at_latest=("observed_at", "max"),
    ).reset_index()

    # 透视：每个机场各来源的延迟
    airport_pivot = airport_groups.pivot_table(
        index="icao", columns="source", values="avg_latency_s", aggfunc="min"
    ).reset_index()

    # 单源机场列表
    source_count_per_airport = df.groupby("icao")["source"].nunique().reset_index(name="source_count")
    single_source = source_count_per_airport[source_count_per_airport["source_count"] == 1].merge(
        df[["icao", "source", "latency_seconds", "observed_at", "updated_at"]], on="icao", how="left"
    ).drop_duplicates(subset=["icao"])

    return df, summary, airport_pivot, single_source


def to_naive(dt: datetime) -> datetime:
    """将带时区 datetime 转为 Excel 兼容的 naive UTC datetime。"""
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def write_excel(
    df: pd.DataFrame,
    summary: pd.DataFrame,
    airport_pivot: pd.DataFrame,
    single_source: pd.DataFrame,
    output_path: Path,
) -> None:
    """将分析结果写入 Excel 多 sheet。"""
    # Excel 不支持带时区的 datetime
    df_out = df.copy()
    for col in ["observed_at", "updated_at"]:
        df_out[col] = df_out[col].apply(to_naive)

    single_source_out = single_source.copy()
    for col in ["observed_at", "updated_at"]:
        single_source_out[col] = single_source_out[col].apply(to_naive)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="来源延迟汇总", index=False)
        df_out.to_excel(writer, sheet_name="原始记录", index=False)
        airport_pivot.to_excel(writer, sheet_name="同机场来源对比", index=False)
        single_source_out.to_excel(writer, sheet_name="单源机场", index=False)

        # 调整列宽
        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            for col in ws.columns:
                max_length = 0
                col_letter = col[0].column_letter
                for cell in col:
                    try:
                        max_length = max(max_length, len(str(cell.value)))
                    except Exception:
                        pass
                ws.column_dimensions[col_letter].width = min(max_length + 2, 60)


def main() -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] 开始从 VPS 拉取 METAR 数据...")
    records = fetch_all_metar_records()
    print(f"共拉取 {len(records)} 条 Redis 记录")

    df, summary, airport_pivot, single_source = analyze(records, PAST_HOURS)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = Path(__file__).parent.parent / f"metar_source_latency_{timestamp}.xlsx"
    write_excel(df, summary, airport_pivot, single_source, output_path)

    print(f"\n分析完成，Excel 已保存: {output_path}")
    print(f"过去 {PAST_HOURS} 小时共 {len(df)} 条记录")
    print("\n=== 来源延迟汇总 ===")
    print(
        summary[
            ["source", "count", "avg_latency_human", "median_latency_human", "min_latency_s", "max_latency_s"]
        ].to_string(index=False)
    )


if __name__ == "__main__":
    main()
