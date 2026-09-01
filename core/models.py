"""数据模型与平台能力矩阵。

平台能力来自对 AstrBot v4.27.x 各适配器源码的核实，用于在 UI 上禁用无效选项、
并在发送前过滤掉目标平台不支持的消息组件。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

GROUP = "GroupMessage"
FRIEND = "FriendMessage"

OUTSIDE_DROP = "drop"
OUTSIDE_QUEUE = "queue"
OUTSIDE_SEND = "send"

AT_NONE = "none"
AT_ALL = "all"
AT_USERS = "users"


@dataclass(frozen=True)
class PlatformCaps:
    """某类平台适配器的能力。"""

    group: bool = True
    private: bool = True
    at: bool = True
    listable: bool = False
    note: str = ""


# key 为适配器注册名（platform.meta().name），非用户自定义的实例 id
PLATFORM_CAPS: dict[str, PlatformCaps] = {
    "aiocqhttp": PlatformCaps(
        group=True,
        private=True,
        at=True,
        listable=True,
        note="支持通过 OneBot API 拉取群与好友列表。",
    ),
    "qq_official": PlatformCaps(
        group=True,
        private=True,
        at=True,
        listable=False,
        note="QQ 官方 API 无法查询群列表，群 openid 只能通过收到消息来发现；"
        "主动消息受腾讯侧频次额度限制。",
    ),
    "qqofficial_webhook": PlatformCaps(
        group=True,
        private=True,
        at=True,
        listable=False,
        note="QQ 官方 API（Webhook 模式），限制同 qq_official。",
    ),
    "weixin_oc": PlatformCaps(
        group=False,
        private=True,
        at=False,
        listable=False,
        note="个人微信（ClawBot）适配器仅处理私聊消息，且不支持 @ 组件。",
    ),
    "wecom": PlatformCaps(group=True, private=True, at=True, listable=False),
    "telegram": PlatformCaps(group=True, private=True, at=True, listable=False),
    "discord": PlatformCaps(group=True, private=True, at=True, listable=False),
    "lark": PlatformCaps(group=True, private=True, at=True, listable=False),
    "dingtalk": PlatformCaps(group=True, private=True, at=True, listable=False),
    "slack": PlatformCaps(group=True, private=True, at=True, listable=False),
    "webchat": PlatformCaps(group=False, private=True, at=False, listable=False),
}

# 未知平台采取宽松策略：允许全部能力，避免因适配器更新而误禁用
DEFAULT_CAPS = PlatformCaps(
    group=True,
    private=True,
    at=True,
    listable=False,
    note="未收录的平台，能力按最宽松处理；若发送失败请检查该平台是否支持主动消息。",
)


def caps_of(platform_name: str) -> PlatformCaps:
    """按适配器注册名取能力描述。"""
    return PLATFORM_CAPS.get(platform_name, DEFAULT_CAPS)


def default_schedule() -> dict[str, Any]:
    """机器人默认时段：全天全周开放。"""
    return {
        "enabled": False,
        "timezone": "Asia/Shanghai",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "ranges": [["00:00", "23:59"]],
        "outside_action": OUTSIDE_QUEUE,
    }


@dataclass
class Target:
    """一个转发目标：某个机器人实例下的某个群或私聊会话。"""

    id: str
    platform_id: str
    umo: str
    message_type: str = GROUP
    display_name: str = ""
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    at_mode: str = AT_NONE
    at_users: list[str] = field(default_factory=list)
    schedule_inherit: bool = True
    schedule: dict[str, Any] = field(default_factory=default_schedule)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Target:
        """从持久化数据构造，未知字段忽略，缺失字段取默认值。"""
        known = {f for f in cls.__dataclass_fields__}
        data = {k: v for k, v in raw.items() if k in known}
        data.setdefault("id", "")
        data.setdefault("platform_id", "")
        data.setdefault("umo", "")
        if not data.get("schedule"):
            data["schedule"] = default_schedule()
        return cls(**data)

    def matches_tags(self, tags: list[str]) -> bool:
        """标签路由判定。

        推送未指定标签时命中全部目标；目标未设标签时只接收未指定标签的推送。
        """
        if not tags:
            return True
        if not self.tags:
            return False
        return bool(set(tags) & set(self.tags))


@dataclass
class BotConfig:
    """一个机器人实例的转发配置。"""

    enabled: bool = True
    remark: str = ""
    schedule: dict[str, Any] = field(default_factory=default_schedule)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BotConfig:
        return cls(
            enabled=bool(raw.get("enabled", True)),
            remark=str(raw.get("remark", "")),
            schedule=raw.get("schedule") or default_schedule(),
        )
