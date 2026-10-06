"""时间工具：统一按 UTC 处理。

PostgreSQL 的 timestamptz 取回带时区，SQLite 取回为无时区时间；
统一在系统边界归一化，避免 aware/naive 比较报错。
"""
from datetime import datetime, timezone


def ensure_utc(dt: datetime) -> datetime:
    """无时区时间按 UTC 处理；有时区的统一转成 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc(dt: datetime) -> str:
    """UTC ISO 8601 规范表示（快照 / 表态片段坐标 / 封存内容的时间格式）。

    整秒时刻保持既有格式 ``YYYY-MM-DDTHH:MM:SSZ``（与对外时间格式一致，
    既有整秒标识不变）；含亚秒（微秒非零）时追加 6 位微秒
    ``.ffffff``——与 API 响应中亚秒时刻的序列化形态一致，使亚秒级
    边界变化在快照标识、表态片段坐标与封存证据时间中可按实际时刻区分。
    """
    dt = ensure_utc(dt)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        base += f".{dt.microsecond:06d}"
    return base + "Z"
