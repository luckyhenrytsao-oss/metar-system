"""分析 VPS 上过去一段时间采集的 METAR 数据，比较 weather.gov 与 AWC 谁更快拿到新数据。

逻辑：
- 通过 SSH 直连 VPS Redis，拉取所有 metar:{icao}:source:* 与 metar:{icao} 记录。
- 按 updated_at 过滤过去 N 小时内的记录。
- 计算 latency = updated_at - observed_at（即 METAR 发出后到 M2 入库的延迟）。
- 同机场维度对比：找出两个数据源都有记录的机场，判断谁第一个拿到更新的 METAR。
- 输出 Excel，包含来源汇总、同机场胜负统计、原始记录。

用法：
    python scripts/analyze_source_latency.py --hours 0.5
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# 配置
VPS_HOST = "47.251.25.183"
VPS_PORT = "2222"
VPS_USER = "root"
SSH_KEY = r"C:\Users\Henry\.ssh\id_ed25519"
REDIS_KEY_PATTERN = "metar:*"


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


def fetch_redis_keys(pattern: str) -> list[str]:
    """拉取匹配 pattern 的所有 Redis keys。"""
    keys_json = ssh_command(f"docker exec metar-redis redis-cli --json KEYS '{pattern}'")
    return json.loads(keys_json) if keys_json.strip() else []


def fetch_records_for_keys(keys: list[str]) -> list[dict]:
    """分批 MGET 拉取 keys 对应的值。"""
    if not keys:
        return []

    batch_size = 30
    records: list[dict] = []
    for i in range(0, len(keys), batch_size):
        batch = keys[i : i + batch_size]
        args = " ".join(f"'{k}'" for k in batch)
        values_json = ssh_command(f"docker exec metar-redis redis-cli --json MGET {args}")
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


def parse_iso(value: Any) -> datetime | None:
    """解析 ISO 8601 时间字符串为 UTC datetime。"""
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def source_key_to_label(source_key: str) -> str:
    """将 Redis 中的 source_key 转换为可读标签。"""
    return {
        "weathergov": "weather.gov",
        "awc": "aviationweather.gov",
    }.get(source_key, source_key)


def analyze_source_records(records: list[dict], hours: float) -> pd.DataFrame:
    """分析 source-specific 记录，返回带 latency 的 DataFrame。"""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    rows = []
    for r in records:
        key = r.get("redis_key", "")
        if ":source:" not in key:
            continue

        source_key = key.split(":source:")[-1]
        updated_at = parse_iso(r.get("updated_at"))
        observed_at = parse_iso(r.get("observed_at"))

        if updated_at is None or observed_at is None:
            continue
        if updated_at < cutoff:
            continue

        latency = updated_at - observed_at
        rows.append(
            {
                "icao": r.get("icao", ""),
                "source_key": source_key,
                "source": source_key_to_label(source_key),
                "raw_text": r.get("raw_text", ""),
                "observed_at": observed_at,
                "updated_at": updated_at,
                "latency_seconds": latency.total_seconds(),
                "latency_human": str(latency),
            }
        )

    return pd.DataFrame(rows)


def build_summary(df: pd.DataFrame) -> pd.DataFrame:
    """按来源汇总延迟统计。"""
    if df.empty:
        return pd.DataFrame()

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
    return summary


def build_airport_comparison(df: pd.DataFrame) -> pd.DataFrame:
    """同机场对比：找出两个数据源都有记录的机场，判断谁更快拿到更新的数据。"""
    if df.empty:
        return pd.DataFrame()

    # 每个机场每个数据源取 observed_at 最大的那条（最新 METAR）
    latest = (
        df.sort_values("observed_at", ascending=False)
        .groupby(["icao", "source_key"])
        .first()
        .reset_index()
    )

    # 透视
    pivot = latest.pivot_table(
        index="icao",
        columns="source_key",
        values=["observed_at", "updated_at", "latency_seconds", "raw_text"],
        aggfunc="first",
    )

    rows = []
    for icao in pivot.index:
        wg_obs = pivot.loc[icao, ("observed_at", "weathergov")] if ("observed_at", "weathergov") in pivot.columns else None
        awc_obs = pivot.loc[icao, ("observed_at", "awc")] if ("observed_at", "awc") in pivot.columns else None

        if pd.isna(wg_obs) or pd.isna(awc_obs):
            continue

        wg_updated = pivot.loc[icao, ("updated_at", "weathergov")]
        awc_updated = pivot.loc[icao, ("updated_at", "awc")]
        wg_latency = pivot.loc[icao, ("latency_seconds", "weathergov")]
        awc_latency = pivot.loc[icao, ("latency_seconds", "awc")]

        if wg_obs > awc_obs:
            winner = "weather.gov (更新鲜)"
        elif awc_obs > wg_obs:
            winner = "AWC (更新鲜)"
        else:
            # observed_at 相同，比较延迟
            if wg_latency < awc_latency:
                winner = "weather.gov (同时间，延迟更低)"
            elif awc_latency < wg_latency:
                winner = "AWC (同时间，延迟更低)"
            else:
                winner = "平局"

        rows.append(
            {
                "icao": icao,
                "weathergov_observed_at": wg_obs,
                "awc_observed_at": awc_obs,
                "weathergov_updated_at": wg_updated,
                "awc_updated_at": awc_updated,
                "weathergov_latency_s": wg_latency,
                "awc_latency_s": awc_latency,
                "faster_source": winner,
            }
        )

    return pd.DataFrame(rows)


def build_winner_summary(airport_df: pd.DataFrame) -> pd.DataFrame:
    """统计同机场胜负次数。"""
    if airport_df.empty:
        return pd.DataFrame()

    def simplify_winner(value: str) -> str:
        if "weather.gov" in value:
            return "weather.gov"
        if "AWC" in value:
            return "AWC"
        return "平局"

    airport_df["winner_simple"] = airport_df["faster_source"].apply(simplify_winner)
    summary = (
        airport_df["winner_simple"]
        .value_counts()
        .reset_index()
        .rename(columns={"winner_simple": "数据源", "count": "机场数"})
    )
    return summary


def to_naive(dt: datetime) -> datetime:
    """将带时区 datetime 转为 Excel 兼容的 naive UTC datetime。"""
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def write_excel(
    df: pd.DataFrame,
    summary: pd.DataFrame,
    airport_df: pd.DataFrame,
    winner_summary: pd.DataFrame,
    output_path: Path,
) -> None:
    """将分析结果写入 Excel 多 sheet。"""
    df_out = df.copy()
    for col in ["observed_at", "updated_at"]:
        df_out[col] = df_out[col].apply(to_naive)

    airport_out = airport_df.copy()
    for col in ["weathergov_observed_at", "awc_observed_at", "weathergov_updated_at", "awc_updated_at"]:
        if col in airport_out.columns:
            airport_out[col] = airport_out[col].apply(to_naive)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="来源延迟汇总", index=False)
        winner_summary.to_excel(writer, sheet_name="同机场胜负统计", index=False)
        airport_out.to_excel(writer, sheet_name="同机场对比", index=False)
        df_out.to_excel(writer, sheet_name="原始记录", index=False)

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分析 METAR 双源采集延迟")
    parser.add_argument(
        "--hours",
        type=float,
        default=0.5,
        help="分析过去多少小时的数据（默认 0.5）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    hours = args.hours

    print(f"[{datetime.now(timezone.utc).isoformat()}] 开始从 VPS 拉取 METAR 数据...")

    # 只拉取 source-specific keys
    keys = fetch_redis_keys("metar:*:source:*")
    records = fetch_records_for_keys(keys)
    print(f"共拉取 {len(records)} 条数据源记录")

    df = analyze_source_records(records, hours)
    if df.empty:
        raise ValueError(f"过去 {hours} 小时内没有采集到任何 METAR 数据源记录")

    summary = build_summary(df)
    airport_df = build_airport_comparison(df)
    winner_summary = build_winner_summary(airport_df)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = Path(__file__).parent.parent / f"metar_source_latency_{timestamp}.xlsx"
    write_excel(df, summary, airport_df, winner_summary, output_path)

    print(f"\n分析完成，Excel 已保存: {output_path}")
    print(f"过去 {hours} 小时共 {len(df)} 条数据源记录")

    print("\n=== 来源延迟汇总 ===")
    print(
        summary[
            ["source", "count", "avg_latency_human", "median_latency_human", "min_latency_s", "max_latency_s"]
        ].to_string(index=False)
    )

    if not winner_summary.empty:
        print("\n=== 同机场胜负统计（两个源都有数据时，谁更快拿到新数据） ===")
        print(winner_summary.to_string(index=False))
        print(f"\n同场竞技机场数：{len(airport_df)}")


if __name__ == "__main__":
    main()
