"""会话发现。

QQ 官方机器人与个人微信（ClawBot）都没有查询群/好友列表的接口，
群 openid 只在机器人收到该群消息时才下发。因此这里监听所有收到的消息，
把出现过的会话记录下来，供 WebUI 面板勾选。

`record()` 会在每条消息上被调用，必须保持轻量：只改内存并标脏，
落盘由后台任务定时执行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import FRIEND, GROUP
from .utils import atomic_write_json, load_json, now_ts

SCHEMA_VERSION = 1
SOURCE_DISCOVERED = "discovered"
SOURCE_API = "api"


def short_id(value: str, keep: int = 8) -> str:
    """截取 ID 尾部用于显示，QQ 官方的 openid 很长，全展示没有意义。"""
    text = str(value or "")
    return text if len(text) <= keep else "…" + text[-keep:]


class SessionRegistry:
    """已发现会话的缓存。"""

    def __init__(
        self,
        data_dir: Path,
        max_count: int = 500,
        expire_days: int = 30,
    ) -> None:
        self.path = data_dir / "sessions.json"
        self.max_count = max(10, max_count)
        self.expire_seconds = max(1, expire_days) * 86400
        self.sessions: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self.load()

    # ------------------------------------------------------------------ 读写

    def load(self) -> None:
        raw = load_json(self.path, {}) or {}
        sessions = raw.get("sessions") or {}
        if isinstance(sessions, dict):
            self.sessions = {
                str(k): v for k, v in sessions.items() if isinstance(v, dict)
            }

    def flush(self) -> None:
        if not self._dirty:
            return
        atomic_write_json(
            self.path,
            {
                "version": SCHEMA_VERSION,
                "updated_at": now_ts(),
                "sessions": self.sessions,
            },
        )
        self._dirty = False

    # -------------------------------------------------------------- 记录会话

    def record(self, event: Any) -> None:
        """从一条收到的消息中提取会话信息。

        全部字段都用防御式取值：不同平台适配器提供的属性并不一致，
        任何一个字段缺失都不应该影响消息主流程。
        """
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            if not umo:
                return

            platform_id = self._call(event, "get_platform_id")
            platform_name = self._call(event, "get_platform_name")
            group_id = self._call(event, "get_group_id")
            sender_name = self._call(event, "get_sender_name")
            sender_id = self._call(event, "get_sender_id")

            # umo 形如 platform_id:MessageType:session_id
            parts = umo.split(":", 2)
            message_type = parts[1] if len(parts) >= 2 else ""
            session_id = parts[2] if len(parts) >= 3 else ""
            if not message_type:
                message_type = GROUP if group_id else FRIEND
            if not platform_id and parts:
                platform_id = parts[0]

            entry = self.sessions.get(umo)
            if entry is None:
                entry = {
                    "umo": umo,
                    "platform_id": platform_id,
                    "platform_name": platform_name,
                    "message_type": message_type,
                    "session_id": session_id,
                    "group_id": group_id,
                    "display_name": "",
                    "source": SOURCE_DISCOVERED,
                    "msg_count": 0,
                }
                self.sessions[umo] = entry

            entry["last_active"] = now_ts()
            entry["msg_count"] = int(entry.get("msg_count", 0)) + 1
            if sender_name:
                entry["last_sender_name"] = sender_name
            if sender_id:
                entry["last_sender_id"] = sender_id
            if platform_name and not entry.get("platform_name"):
                entry["platform_name"] = platform_name

            # 显示名只在还没有时补，避免覆盖用户在面板里改过的备注
            if not entry.get("display_name"):
                entry["display_name"] = self._guess_name(
                    event, message_type, group_id, sender_name
                )

            self._dirty = True
        except Exception:  # noqa: BLE001 - 会话发现绝不能影响消息主流程
            return

    @staticmethod
    def _call(event: Any, method: str) -> str:
        """调用 event 上的取值方法，任何异常或缺失都返回空串。"""
        fn = getattr(event, method, None)
        if not callable(fn):
            return ""
        try:
            value = fn()
        except Exception:  # noqa: BLE001
            return ""
        return str(value) if value not in (None, "") else ""

    @staticmethod
    def _guess_name(
        event: Any,
        message_type: str,
        group_id: str,
        sender_name: str,
    ) -> str:
        """尽量给出一个人能认出来的名字。

        群名多数平台在消息事件里并不下发，这里退化为群号尾段；
        面板中会同时展示最近发言人帮助辨认，也支持手动改备注。
        """
        if message_type == GROUP:
            raw = getattr(event, "message_obj", None)
            if raw is not None:
                for attr in ("group_name", "group_title"):
                    name = getattr(raw, attr, None)
                    if name:
                        return str(name)
            return f"群 {short_id(group_id)}" if group_id else "群聊"
        return sender_name or "私聊"

    def upsert_from_api(self, items: list[dict[str, Any]]) -> int:
        """写入平台原生 API 拉取到的会话（目前只有 OneBot 系支持）。

        API 拿到的名字比猜测出来的准确，因此会覆盖 display_name。
        """
        count = 0
        for item in items:
            umo = item.get("umo")
            if not umo:
                continue
            entry = self.sessions.get(umo) or {"msg_count": 0}
            entry.update(item)
            entry["source"] = SOURCE_API
            entry.setdefault("last_active", now_ts())
            self.sessions[umo] = entry
            count += 1
        if count:
            self._dirty = True
        return count

    def set_display_name(self, umo: str, name: str) -> bool:
        entry = self.sessions.get(umo)
        if entry is None:
            return False
        entry["display_name"] = name
        self._dirty = True
        return True

    def has(self, umo: str) -> bool:
        return umo in self.sessions

    def get(self, umo: str) -> dict[str, Any] | None:
        return self.sessions.get(umo)

    # ------------------------------------------------------------------ 查询

    def list(
        self,
        platform_id: str | None = None,
        message_type: str | None = None,
        keyword: str = "",
    ) -> list[dict[str, Any]]:
        """按机器人 / 会话类型 / 关键词筛选，结果按最后活跃时间倒序。"""
        result = []
        needle = keyword.strip().lower()
        searchable = ("display_name", "session_id", "group_id", "last_sender_name")
        for entry in self.sessions.values():
            if platform_id and entry.get("platform_id") != platform_id:
                continue
            if message_type and entry.get("message_type") != message_type:
                continue
            if needle:
                haystack = " ".join(str(entry.get(k, "")) for k in searchable).lower()
                if needle not in haystack:
                    continue
            result.append(entry)
        result.sort(key=lambda e: float(e.get("last_active") or 0), reverse=True)
        return result

    # ------------------------------------------------------------------ 清理

    def prune(self, keep_umos: set[str] | None = None) -> int:
        """清理过期与超量的会话。已配置为转发目标的会话永远保留。"""
        keep = keep_umos or set()
        cutoff = now_ts() - self.expire_seconds
        removed = 0

        for umo in list(self.sessions.keys()):
            if umo in keep:
                continue
            if float(self.sessions[umo].get("last_active") or 0) < cutoff:
                del self.sessions[umo]
                removed += 1

        if len(self.sessions) > self.max_count:
            candidates = [
                (float(v.get("last_active") or 0), k)
                for k, v in self.sessions.items()
                if k not in keep
            ]
            candidates.sort()
            overflow = len(self.sessions) - self.max_count
            for _, umo in candidates[:overflow]:
                del self.sessions[umo]
                removed += 1

        if removed:
            self._dirty = True
        return removed
