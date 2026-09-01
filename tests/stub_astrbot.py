"""把一套最小可用的 astrbot 运行时塞进 sys.modules。

本机没有安装 AstrBot，靠这套桩件把 main.py 和 dispatcher 真实跑起来，
用来验证业务逻辑而不是验证桩件本身。桩件只实现插件实际用到的那部分 API。
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------- 消息组件


class Plain:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def __repr__(self) -> str:
        return f"Plain({self.text!r})"


class At:
    def __init__(self, qq: Any = None, name: str = "") -> None:
        self.qq = qq
        self.name = name

    def __repr__(self) -> str:
        return f"At({self.qq!r})"


class AtAll:
    def __repr__(self) -> str:
        return "AtAll()"


@dataclass
class MessageChain:
    chain: list = field(default_factory=list)

    def message(self, text: str) -> MessageChain:
        self.chain.append(Plain(text))
        return self

    def plain_text(self) -> str:
        return "".join(getattr(c, "text", "") for c in self.chain)


# ------------------------------------------------------------------- 过滤器桩件


class _CommandGroup:
    """@filter.command_group 的替身，支持 @组名.command("子命令")。"""

    def __init__(self, name: str, fn: Any) -> None:
        self.name = name
        self.fn = fn
        self.subs: dict[str, Any] = {}

    def command(self, name: str, **kwargs: Any):
        def deco(fn):
            self.subs[name] = fn
            return fn

        return deco


class PermissionType:
    ADMIN = "admin"
    MEMBER = "member"


class EventMessageType:
    ALL = "all"
    GROUP_MESSAGE = "group"
    PRIVATE_MESSAGE = "private"


class _Filter:
    PermissionType = PermissionType
    EventMessageType = EventMessageType

    @staticmethod
    def command_group(name: str, **kwargs: Any):
        def deco(fn):
            return _CommandGroup(name, fn)

        return deco

    @staticmethod
    def command(name: str, **kwargs: Any):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def permission_type(kind: Any):
        def deco(fn):
            fn._required_permission = kind
            return fn

        return deco

    @staticmethod
    def event_message_type(kind: Any, **kwargs: Any):
        def deco(fn):
            fn._event_message_type = kind
            return fn

        return deco


# ----------------------------------------------------------------- 基础类桩件


class Star:
    def __init__(self, context: Any) -> None:
        self.context = context


class Context:  # 仅作类型占位
    pass


class AstrBotConfig(dict):
    """AstrBot 的配置对象是 dict 的子类，额外带一个 save_config()。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self) -> None:
        self.save_count += 1


class AstrMessageEvent:  # 仅作类型占位
    pass


def register(*args: Any, **kwargs: Any):
    def deco(cls):
        cls._registered_meta = args
        return cls

    return deco


# --------------------------------------------------------------------- Web 桩件


class _QueryDict(dict):
    def get(self, key, default=None, type=None):  # noqa: A002
        value = super().get(key, default)
        if type is not None and value is not None:
            try:
                return type(value)
            except (TypeError, ValueError):
                return default
        return value


class _RequestProxy:
    """astrbot.api.web.request 的替身，测试里手动绑定当前请求。"""

    def __init__(self) -> None:
        self.query = _QueryDict()
        self._json: Any = {}
        self.method = "GET"
        self.username = "tester"

    def bind(self, query: dict | None = None, json_body: Any = None, method: str = "GET") -> None:
        self.query = _QueryDict(query or {})
        self._json = json_body if json_body is not None else {}
        self.method = method

    async def json(self) -> Any:
        return self._json


request = _RequestProxy()


def json_response(data: Any, status_code: int = 200) -> Any:
    """真实实现会包成 HTTP 响应，这里直接返回，方便断言。"""
    return data


def error_response(message: str, status_code: int = 400) -> Any:
    return {"status": "error", "message": message, "status_code": status_code}


# ------------------------------------------------------------------ 安装到 sys.modules


def install(data_path: str) -> None:
    """注册全部桩件模块。data_path 决定插件把数据写到哪。"""

    def make(name: str) -> types.ModuleType:
        module = types.ModuleType(name)
        sys.modules[name] = module
        return module

    astrbot = make("astrbot")
    api = make("astrbot.api")
    event_mod = make("astrbot.api.event")
    star_mod = make("astrbot.api.star")
    web_mod = make("astrbot.api.web")
    comp_mod = make("astrbot.api.message_components")
    core = make("astrbot.core")
    core_utils = make("astrbot.core.utils")
    path_mod = make("astrbot.core.utils.astrbot_path")

    import logging

    api.logger = logging.getLogger("astrbot_stub")
    api.logger.addHandler(logging.NullHandler())
    api.AstrBotConfig = AstrBotConfig

    event_mod.filter = _Filter
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain

    star_mod.Star = Star
    star_mod.Context = Context
    star_mod.register = register

    web_mod.request = request
    web_mod.json_response = json_response
    web_mod.error_response = error_response

    comp_mod.Plain = Plain
    comp_mod.At = At
    comp_mod.AtAll = AtAll

    path_mod.get_astrbot_data_path = lambda: data_path

    astrbot.api = api
    astrbot.core = core
    api.event = event_mod
    api.star = star_mod
    api.web = web_mod
    api.message_components = comp_mod
    core.utils = core_utils
    core_utils.astrbot_path = path_mod


# --------------------------------------------------------------- 平台 / 上下文桩件


class FakePlatformMeta:
    def __init__(self, platform_id: str, name: str, description: str = "") -> None:
        self.id = platform_id
        self.name = name
        self.description = description


class FakePlatform:
    def __init__(self, platform_id: str, name: str, client: Any = None) -> None:
        self._meta = FakePlatformMeta(platform_id, name, f"{name} 适配器")
        self._client = client

    def meta(self) -> FakePlatformMeta:
        return self._meta

    def get_client(self) -> Any:
        return self._client


class FakePlatformManager:
    def __init__(self, platforms: list[FakePlatform]) -> None:
        self.platform_insts = platforms


class FakeContext:
    """记录 send_message 调用，并可注入发送失败以测试重试。"""

    def __init__(self, platforms: list[FakePlatform]) -> None:
        self.platform_manager = FakePlatformManager(platforms)
        self.sent: list[tuple[str, Any]] = []
        self.web_apis: list[tuple] = []
        self.fail_times = 0
        self.return_false_for: set[str] = set()

    def get_platform_inst(self, platform_id: str) -> FakePlatform | None:
        for p in self.platform_manager.platform_insts:
            if p.meta().id == platform_id:
                return p
        return None

    async def send_message(self, umo: str, chain: Any) -> bool:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("模拟的发送失败")
        if umo in self.return_false_for:
            return False
        self.sent.append((umo, chain))
        return True

    def register_web_api(self, route: str, handler: Any, methods: list[str], desc: str) -> None:
        self.web_apis.append((route, handler, methods, desc))


class FakeEvent:
    """一条收到的消息，字段与 AstrMessageEvent 的取值方法对齐。"""

    def __init__(
        self,
        umo: str,
        platform_id: str,
        platform_name: str,
        group_id: str = "",
        sender_name: str = "",
        sender_id: str = "",
    ) -> None:
        self.unified_msg_origin = umo
        self._platform_id = platform_id
        self._platform_name = platform_name
        self._group_id = group_id
        self._sender_name = sender_name
        self._sender_id = sender_id
        self.message_obj = types.SimpleNamespace()
        self.replies: list[str] = []

    def get_platform_id(self) -> str:
        return self._platform_id

    def get_platform_name(self) -> str:
        return self._platform_name

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_sender_id(self) -> str:
        return self._sender_id

    def plain_result(self, text: str) -> str:
        self.replies.append(text)
        return text
