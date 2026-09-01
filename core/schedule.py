"""转发时段判定。

纯函数模块，不依赖 astrbot，可脱离框架单独用 pytest 测试。

时段结构::

    {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "days": [1, 2, 3, 4, 5],          # ISO 星期，1=周一 7=周日
        "ranges": [["09:00", "18:00"]],   # 支持跨零点，如 ["22:00", "02:00"]
        "outside_action": "queue",
    }

时间比较按"分钟"粒度进行，因此 ["00:00", "23:59"] 表示全天开放。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)

# Windows 上若缺少 tzdata 包，zoneinfo 无法解析 IANA 名称，这里给常用时区兜底
_FALLBACK_OFFSETS: dict[str, int] = {
    "utc": 0,
    "asia/shanghai": 8 * 60,
    "asia/chongqing": 8 * 60,
    "asia/hong_kong": 8 * 60,
    "asia/taipei": 8 * 60,
    "asia/singapore": 8 * 60,
    "asia/tokyo": 9 * 60,
    "asia/seoul": 9 * 60,
    "asia/bangkok": 7 * 60,
    "asia/ho_chi_minh": 7 * 60,
    "asia/kolkata": 5 * 60 + 30,
    "europe/london": 0,
    "europe/paris": 60,
    "europe/berlin": 60,
    "europe/moscow": 3 * 60,
    "america/new_york": -5 * 60,
    "america/chicago": -6 * 60,
    "america/los_angeles": -8 * 60,
    "australia/sydney": 10 * 60,
}

MAX_LOOKAHEAD_DAYS = 8


def resolve_tz(name: str | None) -> tzinfo:
    """把时区名解析为 tzinfo。

    依次尝试：IANA 名称（zoneinfo）→ 固定偏移写法（UTC+8 / +08:00）→
    内置常用时区偏移表 → 本机时区。任何一步失败都不会抛异常。
    """
    if not name:
        return datetime.now().astimezone().tzinfo or timezone.utc

    raw = str(name).strip()

    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(raw)
    except Exception:
        pass

    m = _OFFSET_RE.match(raw)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        minutes = int(m.group(3) or 0)
        return timezone(sign * timedelta(hours=hours, minutes=minutes))

    offset = _FALLBACK_OFFSETS.get(raw.lower())
    if offset is not None:
        return timezone(timedelta(minutes=offset))

    return datetime.now().astimezone().tzinfo or timezone.utc


def parse_hhmm(value: Any) -> int | None:
    """把 "HH:MM" 解析为当天的分钟数。非法输入返回 None。"""
    if isinstance(value, int):
        return value if 0 <= value <= 1439 else None
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _normalized_ranges(schedule: dict[str, Any]) -> list[tuple[int, int]]:
    """取出合法的时间区间，非法条目直接跳过。"""
    result: list[tuple[int, int]] = []
    for item in schedule.get("ranges") or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        start, end = parse_hhmm(item[0]), parse_hhmm(item[1])
        if start is None or end is None:
            continue
        result.append((start, end))
    return result


def _normalized_days(schedule: dict[str, Any]) -> set[int]:
    days: set[int] = set()
    for d in schedule.get("days") or []:
        try:
            n = int(d)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 7:
            days.add(n)
    return days


def is_enabled(schedule: dict[str, Any] | None) -> bool:
    """时段限制是否生效。未配置或未启用都表示不限制。"""
    return bool(schedule and schedule.get("enabled"))


def is_within(schedule: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """判断此刻是否落在允许转发的时段内。

    未启用时段限制时恒为 True；启用但没有任何合法区间或星期时恒为 False
    （这是用户主动配置的结果，按"全天禁止"处理更符合直觉）。
    """
    if not is_enabled(schedule):
        return True
    assert schedule is not None

    tz = resolve_tz(schedule.get("timezone"))
    local = (now or datetime.now(timezone.utc)).astimezone(tz)

    days = _normalized_days(schedule)
    ranges = _normalized_ranges(schedule)
    if not days or not ranges:
        return False

    cur = local.hour * 60 + local.minute
    today = local.isoweekday()
    yesterday = 7 if today == 1 else today - 1

    for start, end in ranges:
        if start <= end:
            if today in days and start <= cur <= end:
                return True
        else:
            # 跨零点区间：当天的尾段，或前一天延续过来的头段
            if today in days and cur >= start:
                return True
            if yesterday in days and cur <= end:
                return True
    return False


def next_open_at(
    schedule: dict[str, Any] | None,
    now: datetime | None = None,
) -> datetime | None:
    """返回下一次可以转发的时刻。

    当前已在时段内则返回传入的时刻本身；未启用限制同样返回当前时刻；
    找不到（例如 days 为空）返回 None。返回值带时区。
    """
    base = now or datetime.now(timezone.utc)
    if not is_enabled(schedule):
        return base
    assert schedule is not None

    if is_within(schedule, base):
        return base

    tz = resolve_tz(schedule.get("timezone"))
    local = base.astimezone(tz)

    days = _normalized_days(schedule)
    ranges = _normalized_ranges(schedule)
    if not days or not ranges:
        return None

    for offset in range(MAX_LOOKAHEAD_DAYS):
        day = local + timedelta(days=offset)
        if day.isoweekday() not in days:
            continue
        midnight = day.replace(hour=0, minute=0, second=0, microsecond=0)
        candidates = [midnight + timedelta(minutes=start) for start, _ in ranges]
        future = sorted(c for c in candidates if c > local)
        if future:
            return future[0]
    return None


def effective_schedule(
    target_schedule: dict[str, Any] | None,
    inherit: bool,
    bot_schedule: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """目标勾选"继承"时用所属机器人的时段，否则用自己的。"""
    return bot_schedule if inherit else target_schedule


def describe(schedule: dict[str, Any] | None) -> str:
    """生成一句人类可读的时段描述，用于指令回执与日志。"""
    if not is_enabled(schedule):
        return "全天不限"
    assert schedule is not None
    names = ["一", "二", "三", "四", "五", "六", "日"]
    days = sorted(_normalized_days(schedule))
    ranges = _normalized_ranges(schedule)
    if not days or not ranges:
        return "全天禁止（未配置有效星期或时间段）"
    if len(days) == 7:
        day_text = "每天"
    else:
        day_text = "周" + "".join(names[d - 1] for d in days)
    range_text = "、".join(
        f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}" for s, e in ranges
    )
    return f"{day_text} {range_text}（{schedule.get('timezone', 'Asia/Shanghai')}）"
