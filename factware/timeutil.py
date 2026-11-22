"""UTC 时间解析与比较。交接资料统一使用带 Z 的 UTC 时间或日期。"""
from datetime import datetime, date, timezone, timedelta


def parse_dt(value):
    """解析 `2026-10-25T09:05:00Z` 形式的 UTC 时间，返回带时区的 datetime。"""
    if value is None or str(value).strip() == "":
        raise ValueError("时间为空")
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_day(value):
    """解析结算日 `2026-10-25`，按 UTC 日期处理。"""
    if value is None or str(value).strip() == "":
        raise ValueError("日期为空")
    return date.fromisoformat(str(value).strip())


def iso(dt):
    """统一输出带 Z 的 UTC 字符串。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    text = dt.astimezone(timezone.utc).isoformat()
    return text.replace("+00:00", "Z")


def minutes_between(earlier, later):
    """later - earlier 的分钟数（可为负）。"""
    delta = later - earlier
    return delta / timedelta(minutes=1)
