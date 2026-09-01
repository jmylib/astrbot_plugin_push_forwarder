"""core.schedule 的单元测试。

无第三方依赖，可直接 `python tests/test_schedule.py` 运行，也可被 pytest 收集。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.schedule import (  # noqa: E402
    describe,
    is_within,
    next_open_at,
    parse_hhmm,
    resolve_tz,
)

SH = resolve_tz("Asia/Shanghai")


def sh(year: int, month: int, day: int, hour: int, minute: int = 0, sec: int = 0):
    return datetime(year, month, day, hour, minute, sec, tzinfo=SH)


def workday_9to18(action: str = "queue") -> dict:
    return {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "days": [1, 2, 3, 4, 5],
        "ranges": [["09:00", "18:00"]],
        "outside_action": action,
    }


def overnight_22to2() -> dict:
    return {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "days": [1, 2, 3, 4, 5],
        "ranges": [["22:00", "02:00"]],
        "outside_action": "queue",
    }


def test_resolve_tz_falls_back_without_tzdata():
    """Windows 缺少 tzdata 时也必须解析出 +08:00，而不是抛异常。"""
    tz = resolve_tz("Asia/Shanghai")
    assert tz.utcoffset(datetime(2026, 8, 31, 12)) == timedelta(hours=8)
    assert resolve_tz("UTC+8").utcoffset(None) == timedelta(hours=8)
    assert resolve_tz("+05:30").utcoffset(None) == timedelta(hours=5, minutes=30)
    assert resolve_tz("完全不存在的时区") is not None  # 回退本机时区，不抛异常


def test_parse_hhmm():
    assert parse_hhmm("09:00") == 540
    assert parse_hhmm("23:59") == 1439
    assert parse_hhmm("00:00") == 0
    assert parse_hhmm("24:00") is None
    assert parse_hhmm("9:0:0") is None
    assert parse_hhmm(None) is None


def test_disabled_schedule_always_open():
    assert is_within(None) is True
    assert is_within({}) is True
    assert is_within({"enabled": False, "days": [], "ranges": []}) is True


def test_workday_range():
    s = workday_9to18()
    assert is_within(s, sh(2026, 8, 31, 10)) is True  # 周一 10:00
    assert is_within(s, sh(2026, 8, 31, 9, 0)) is True  # 边界：区间起点
    assert is_within(s, sh(2026, 8, 31, 18, 0)) is True  # 边界：区间终点
    assert is_within(s, sh(2026, 8, 31, 8, 59)) is False
    assert is_within(s, sh(2026, 8, 31, 18, 1)) is False
    assert is_within(s, sh(2026, 9, 5, 10)) is False  # 周六


def test_full_day_range_covers_last_minute():
    """00:00-23:59 应视为全天开放，包含 23:59:30 这种带秒的时刻。"""
    s = {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "ranges": [["00:00", "23:59"]],
        "outside_action": "queue",
    }
    assert is_within(s, sh(2026, 8, 31, 23, 59, 30)) is True
    assert is_within(s, sh(2026, 8, 31, 0, 0, 0)) is True


def test_overnight_range():
    s = overnight_22to2()
    assert is_within(s, sh(2026, 8, 31, 23)) is True  # 周一 23:00，当天尾段
    assert is_within(s, sh(2026, 9, 1, 1)) is True  # 周二 01:00，周一延续
    assert is_within(s, sh(2026, 9, 1, 3)) is False  # 周二 03:00，已结束
    assert is_within(s, sh(2026, 9, 1, 21, 59)) is False
    # 周六 01:00 属于周五延续，周五在 days 内，应放行
    assert is_within(s, sh(2026, 9, 5, 1)) is True
    # 周日 01:00 属于周六延续，周六不在 days 内，应拒绝
    assert is_within(s, sh(2026, 9, 6, 1)) is False


def test_timezone_is_respected():
    """同一 UTC 时刻，在不同时区配置下判定结果不同。"""
    utc_moment = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)  # 上海时间 10:00
    assert is_within(workday_9to18(), utc_moment) is True
    tokyo = workday_9to18()
    tokyo["timezone"] = "UTC+0"  # 同一时刻在 UTC 下是 02:00，不在 9-18 内
    assert is_within(tokyo, utc_moment) is False


def test_empty_days_or_ranges_blocks_everything():
    s = workday_9to18()
    s["days"] = []
    assert is_within(s, sh(2026, 8, 31, 10)) is False
    assert next_open_at(s, sh(2026, 8, 31, 10)) is None

    s2 = workday_9to18()
    s2["ranges"] = []
    assert is_within(s2, sh(2026, 8, 31, 10)) is False


def test_next_open_at_same_day():
    s = workday_9to18()
    nxt = next_open_at(s, sh(2026, 8, 31, 8, 0))
    assert nxt is not None
    assert (nxt.hour, nxt.minute) == (9, 0)
    assert nxt.date() == sh(2026, 8, 31, 0).date()


def test_next_open_at_skips_weekend():
    """周五 19:00 之后最近的开放时刻是下周一 09:00。"""
    s = workday_9to18()
    nxt = next_open_at(s, sh(2026, 9, 4, 19, 0))
    assert nxt is not None
    assert nxt.isoweekday() == 1
    assert (nxt.hour, nxt.minute) == (9, 0)
    assert nxt.date() == sh(2026, 9, 7, 0).date()


def test_next_open_at_returns_now_when_open():
    s = workday_9to18()
    moment = sh(2026, 8, 31, 10)
    assert next_open_at(s, moment) == moment
    assert next_open_at(None, moment) == moment


def test_describe():
    assert describe(None) == "全天不限"
    text = describe(workday_9to18())
    assert "周一二三四五" in text
    assert "09:00-18:00" in text
    everyday = workday_9to18()
    everyday["days"] = [1, 2, 3, 4, 5, 6, 7]
    assert describe(everyday).startswith("每天")


def _run_all() -> int:
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}: {e or 'assertion failed'}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
