"""推送 payload 的解析与文本渲染。

不依赖 astrbot，可独立测试。消息链的构建放在 dispatcher 里，
因为那一步需要按目标平台的能力过滤组件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import AT_ALL, AT_NONE, AT_USERS
from .utils import now_ts

# 兼容常见推送工具的字段命名，越靠前优先级越高
TITLE_KEYS = ("title", "subject", "summary", "标题")
TEXT_KEYS = ("text", "content", "message", "body", "msg", "desc", "description", "内容")
TAG_KEYS = ("tags", "tag", "group", "channel", "标签")
TARGET_KEYS = ("targets", "target", "to")
# 指定机器人。故意不收 platform / platforms 这类泛化写法 —— 推送方的消息体里
# 很可能已经有个表示别的意思的 platform 字段（操作系统、来源系统…），
# 认了它就会把一条本该全发的推送莫名其妙地路由到零个目标上。
BOT_KEYS = ("bots", "bot", "bot_id", "bot_ids", "platform_id", "机器人")


class PayloadError(ValueError):
    """payload 不合法，调用方应返回 400。"""


@dataclass
class PushMessage:
    """一条待转发的推送。"""

    title: str = ""
    text: str = ""
    tags: list[str] = field(default_factory=list)
    target_ids: list[str] = field(default_factory=list)
    bot_ids: list[str] = field(default_factory=list)
    at_mode: str | None = None
    at_users: list[str] = field(default_factory=list)
    urgent: bool = False
    # 自测用：解析、路由、时段判定照常走完，但不真发也不入队。
    # 故意不写进 to_dict/from_dict —— 自测消息不落盘，就没有重启后被当真发出去的可能。
    dry_run: bool = False
    received_at: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "text": self.text,
            "tags": self.tags,
            "target_ids": self.target_ids,
            "bot_ids": self.bot_ids,
            "at_mode": self.at_mode,
            "at_users": self.at_users,
            "urgent": self.urgent,
            "received_at": self.received_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PushMessage:
        """从队列文件恢复。"""
        return cls(
            title=str(raw.get("title") or ""),
            text=str(raw.get("text") or ""),
            tags=[str(t) for t in (raw.get("tags") or [])],
            target_ids=[str(t) for t in (raw.get("target_ids") or [])],
            bot_ids=[str(b) for b in (raw.get("bot_ids") or [])],
            at_mode=raw.get("at_mode"),
            at_users=[str(u) for u in (raw.get("at_users") or [])],
            urgent=bool(raw.get("urgent")),
            received_at=float(raw.get("received_at") or now_ts()),
        )


DRY_RUN_KEYS = ("dry_run", "dryrun", "dry-run")


def _truthy(value: Any) -> bool:
    """判断开关字段。

    GET 查询串里的值全是字符串，``bool("0")`` 是 True，直接 bool() 会把
    ``?urgent=0`` 当成真，所以字符串要单独按字面判。
    """
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    return bool(value)


def _flag(raw: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(_truthy(raw[k]) for k in keys if k in raw)


def _list_field(raw: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    """按别名取第一个出现的列表字段。"""
    return _as_list(next((raw[k] for k in keys if k in raw), None))


def _first_str(raw: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _as_list(value: Any) -> list[str]:
    """把字符串 / 列表 / 逗号分隔串统一成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.replace("，", ",").split(",") if p.strip()]
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                result.append(text)
        return result
    return [str(value).strip()] if str(value).strip() else []


# 路由类字段：允许写在 URL 查询串里。正文相关的键不在其中 —— 从 URL 顶掉
# 请求体里的正文只会制造"发出去的内容和推送方以为的不一样"这类难查的问题。
ROUTING_KEYS = BOT_KEYS + TAG_KEYS + TARGET_KEYS + ("urgent", "force") + DRY_RUN_KEYS


def query_to_dict(query: Any) -> dict[str, Any]:
    """把查询串转成 dict，同名参数出现多次时合并成列表（``?bot=a&bot=b``）。"""
    getall = getattr(query, "getall", None)
    out: dict[str, Any] = {}
    for key in query:
        if key in out:
            # MultiDict 迭代会把重复的键各吐一次，第一次就已经全取到了
            continue
        if getall is None:
            out[key] = query.get(key)
            continue
        values = list(getall(key, []))
        out[key] = values[0] if len(values) == 1 else values
    return out


def merge_query_routing(body: dict[str, Any], query: Any) -> dict[str, Any]:
    """把查询串里的路由字段补进请求体，请求体里已有的键不动。

    企业微信机器人那类推送通道，界面上只让填一个 URL，消息体是固定格式改不动。
    把 ``bot`` / ``tags`` 这些写在 URL 上，那一侧就不用改代码也能指定机器人。
    """
    extra = {
        k: v
        for k, v in query_to_dict(query).items()
        if k in ROUTING_KEYS and k not in body
    }
    if not extra:
        return body
    merged = dict(body)
    merged.update(extra)
    return merged


def _parse_at(raw: Any) -> tuple[str | None, list[str]]:
    """解析 at 字段。

    支持 ``true`` / ``"all"`` / ``["u1","u2"]`` / ``{"mode":"users","users":[...]}``
    四种写法，返回 (mode, users)；未指定时 mode 为 None，表示沿用目标自身配置。
    """
    if raw is None:
        return None, []
    if isinstance(raw, bool):
        return (AT_ALL, []) if raw else (AT_NONE, [])
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in ("all", "everyone", "@all", "全体"):
            return AT_ALL, []
        if value in ("none", "no", "off", ""):
            return AT_NONE, []
        return AT_USERS, [raw.strip()]
    if isinstance(raw, (list, tuple)):
        users = _as_list(raw)
        return (AT_USERS, users) if users else (AT_NONE, [])
    if isinstance(raw, dict):
        mode = str(raw.get("mode") or "").strip().lower()
        users = _as_list(raw.get("users") or raw.get("user"))
        if mode in (AT_ALL, AT_NONE, AT_USERS):
            return mode, users
        if users:
            return AT_USERS, users
        return None, []
    return None, []


# ---------------------------------------------------------------- 企业微信兼容

# 企业微信群机器人的消息体形如 {"msgtype":"text","text":{"content":"..."}}。
# 很多监控/告警系统自带「企业微信机器人」推送通道，界面上只让填一个 URL，
# 兼容这套协议之后，把那个 URL 换成本插件地址就能直接用，推送方一行都不用改。
WECOM_TEXT_TYPES = ("text", "markdown", "markdown_v2")
# 这几类没有可读正文，本插件只转发文本，明确告诉推送方不支持，别静默吞掉
WECOM_UNSUPPORTED = ("image", "file", "voice")

# markdown 里的 @ 写法是 <@userid>，企微客户端会渲染成人名，别的平台不会
_MD_MENTION = re.compile(r"<@([^<>\s]+)>")
# 企微 markdown 支持 <font color="warning">…</font> 着色，几乎每个告警模板都在用。
# 别的平台只发纯文本，不去掉就会在群里看到一串字面标签。
_MD_FONT = re.compile(r"</?font[^<>]*>", re.IGNORECASE)
_AT_ALL_WORDS = ("@all", "all", "@全体成员")


def is_wecom_payload(raw: Any) -> bool:
    """判断是不是企业微信群机器人的消息体。有 msgtype 字段就算。"""
    return isinstance(raw, dict) and bool(str(raw.get("msgtype") or "").strip())


def _wecom_mentions(block: dict[str, Any]) -> tuple[str | None, list[str]]:
    """把企微的 @ 字段映射成本插件的 at，返回 (at_mode, 退化成文本的人名)。

    企微用 userid 和手机号标识人，QQ、微信那边是另一套 ID，对不上号，
    所以只有 @all 能真正映射成「@全体」。剩下的 userid 退化成正文前缀，
    信息不丢；手机号则直接丢弃 —— 不该把号码转发到另一个群里去。
    """
    userids = _as_list(block.get("mentioned_list"))
    mobiles = _as_list(block.get("mentioned_mobile_list"))
    at_all = any(v.lower() in _AT_ALL_WORDS for v in userids + mobiles)
    names = [v for v in userids if v.lower() not in _AT_ALL_WORDS]
    return (AT_ALL if at_all else None), names


def _dict_items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(
        value, list
    ) else []


def _joined(*values: Any) -> str:
    return "\n".join(v for v in (str(x or "").strip() for x in values) if v)


def _wecom_articles(block: dict[str, Any]) -> tuple[str, str]:
    """图文消息渲染成「标题 + 摘要 + 链接」的纯文本。"""
    articles = _dict_items(block.get("articles"))
    if not articles:
        return "", ""
    if len(articles) == 1:
        # 只有一条时把标题提升为 title，走模板渲染更好看
        one = articles[0]
        return str(one.get("title") or "").strip(), _joined(
            one.get("description"), one.get("url")
        )
    blocks = [
        _joined(a.get("title"), a.get("description"), a.get("url")) for a in articles
    ]
    return "", "\n\n".join(b for b in blocks if b)


def _wecom_card(block: dict[str, Any]) -> tuple[str, str]:
    """模板卡片尽力抽取可读文本。字段随 card_type 变化，取到什么算什么。"""
    main = block.get("main_title")
    main = main if isinstance(main, dict) else {}
    title = str(main.get("title") or "").strip()

    lines: list[str] = []
    source = block.get("source")
    if isinstance(source, dict):
        lines.append(str(source.get("desc") or "").strip())
    lines.append(str(main.get("desc") or "").strip())
    lines.append(str(block.get("sub_title_text") or "").strip())
    for row in _dict_items(block.get("horizontal_content_list")):
        key = str(row.get("keyname") or "").strip()
        value = str(row.get("value") or "").strip()
        if key and value:
            lines.append(f"{key}：{value}")
        elif key or value:
            lines.append(key or value)
    action = block.get("card_action")
    if isinstance(action, dict):
        lines.append(str(action.get("url") or "").strip())
    return title, "\n".join(line for line in lines if line)


def parse_wecom_payload(
    raw: dict[str, Any], default_tags: list[str] | None = None
) -> PushMessage:
    """解析企业微信群机器人格式。

    只兼容入口协议：正文取出来之后照旧走本插件的模板、标签、时段与目标选择，
    内部模型完全不变。本插件自己的扩展字段（tags / targets / urgent / at）
    可以直接混在企微消息体里，方便按标签分流。
    """
    msgtype = str(raw.get("msgtype") or "").strip().lower()
    if msgtype in WECOM_UNSUPPORTED:
        raise PayloadError(
            f"不支持 msgtype={msgtype}：本插件只转发文本，请改用 text 或 markdown"
        )

    block = raw.get(msgtype)
    if isinstance(block, str):
        # 有些实现偷懒，直接写成 {"msgtype":"text","text":"..."}，一并认了
        block = {"content": block}
    block = block if isinstance(block, dict) else {}

    title = ""
    at_mode: str | None = None
    names: list[str] = []

    if msgtype in WECOM_TEXT_TYPES:
        text = str(block.get("content") or "").strip()
        if msgtype != "text":
            text = _MD_FONT.sub("", _MD_MENTION.sub(r"@\1", text)).strip()
        at_mode, names = _wecom_mentions(block)
    elif msgtype == "news":
        title, text = _wecom_articles(block)
    elif msgtype == "template_card":
        title, text = _wecom_card(block)
    else:
        # 没见过的 msgtype 也先试着捞一把正文，捞不到再报错
        text = str(block.get("content") or "").strip()

    if names and text:
        text = " ".join(f"@{n}" for n in names) + "\n" + text

    if not title and not text:
        raise PayloadError(
            f"企业微信消息体里没有可转发的文本（msgtype={msgtype or '缺失'}）"
        )

    tags = _list_field(raw, TAG_KEYS)
    if not tags and default_tags:
        tags = list(default_tags)
    explicit_mode, explicit_users = _parse_at(raw.get("at"))

    return PushMessage(
        title=title,
        text=text,
        tags=tags,
        target_ids=_list_field(raw, TARGET_KEYS),
        bot_ids=_list_field(raw, BOT_KEYS),
        at_mode=explicit_mode or at_mode,
        at_users=explicit_users,
        urgent=_flag(raw, ("urgent", "force")),
        dry_run=_flag(raw, DRY_RUN_KEYS),
    )


def parse_payload(raw: Any, default_tags: list[str] | None = None) -> PushMessage:
    """把请求体解析成 PushMessage。

    raw 为字符串时整体作为正文（便于 ``Content-Type: text/plain`` 与简易脚本）。
    标题与正文全空时抛 PayloadError。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise PayloadError("推送内容为空")
        return PushMessage(text=text, tags=list(default_tags or []))

    if not isinstance(raw, dict):
        raise PayloadError("推送内容必须是 JSON 对象或纯文本")

    # 企微群机器人的消息体结构不同（msgtype + 同名嵌套对象），单独走一条路
    if is_wecom_payload(raw):
        return parse_wecom_payload(raw, default_tags=default_tags)

    title = _first_str(raw, TITLE_KEYS)
    text = _first_str(raw, TEXT_KEYS)
    if not title and not text:
        raise PayloadError(
            "缺少消息内容，请提供 text（或 content / message / body）字段"
        )

    tags = _list_field(raw, TAG_KEYS)
    if not tags and default_tags:
        tags = list(default_tags)

    at_mode, at_users = _parse_at(raw.get("at"))

    return PushMessage(
        title=title,
        text=text,
        tags=tags,
        target_ids=_list_field(raw, TARGET_KEYS),
        bot_ids=_list_field(raw, BOT_KEYS),
        at_mode=at_mode,
        at_users=at_users,
        urgent=_flag(raw, ("urgent", "force")),
        dry_run=_flag(raw, DRY_RUN_KEYS),
    )


def _collapse_blank_lines(text: str) -> str:
    """把模板里因空变量留下的连续空行压成一个，并去掉首尾空白。"""
    lines = [line.rstrip() for line in text.splitlines()]
    result: list[str] = []
    for line in lines:
        if not line and (not result or not result[-1]):
            continue
        result.append(line)
    while result and not result[-1]:
        result.pop()
    return "\n".join(result).strip()


def render(message: PushMessage, template: str = "{title}\n{text}") -> str:
    """按模板渲染正文。

    用逐个变量替换而非 str.format，这样消息内容里出现 ``{}`` 不会导致渲染失败，
    也避免了 format 的属性访问带来的风险。
    """
    mapping = {
        "{title}": message.title,
        "{text}": message.text,
        "{tag}": "、".join(message.tags),
        "{tags}": "、".join(message.tags),
        "{time}": datetime.fromtimestamp(message.received_at).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    }
    rendered = template or "{title}\n{text}"
    for key, value in mapping.items():
        rendered = rendered.replace(key, value)
    result = _collapse_blank_lines(rendered)
    # 模板被改成只有变量且变量全空时，退回到原始内容，避免发出空消息
    return result or _collapse_blank_lines(f"{message.title}\n{message.text}")


def split_text(text: str, max_length: int, split: bool = True) -> list[str]:
    """按长度切分正文。

    max_length <= 0 表示不限制；split 为 False 时只截断并加省略号。
    切分优先落在换行处，其次是句末标点，都没有才硬切。
    """
    if max_length <= 0 or len(text) <= max_length:
        return [text]

    if not split:
        return [text[: max_length - 1] + "…"]

    chunks: list[str] = []
    rest = text
    while len(rest) > max_length:
        window = rest[:max_length]
        cut = window.rfind("\n")
        if cut < max_length // 2:
            for mark in ("。", "！", "？", ". ", "! ", "? ", "；", ";", " "):
                pos = window.rfind(mark)
                if pos >= max_length // 2:
                    cut = pos + len(mark)
                    break
        if cut < max_length // 2:
            cut = max_length
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return [c for c in chunks if c]
