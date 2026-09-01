"""转发调度。

职责：按标签/目标筛选 → 时段判定 → 按平台能力过滤消息组件 → 限速发送 → 重试 → 记账。

顶部的 astrbot 导入做了容错，好让本模块在没有安装 astrbot 的环境里也能被
导入做单元测试；真正发送时若导入失败会直接返回错误而不是崩掉插件。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .models import (
    AT_ALL,
    AT_NONE,
    AT_USERS,
    FRIEND,
    GROUP,
    OUTSIDE_DROP,
    OUTSIDE_QUEUE,
    Target,
    caps_of,
)
from .payload import PushMessage, render, split_text
from .schedule import describe, is_within, next_open_at
from .utils import atomic_write_json, load_json, now_ts

try:  # pragma: no cover - 取决于运行环境是否有 astrbot
    import astrbot.api.message_components as Comp
    from astrbot.api import logger
    from astrbot.api.event import MessageChain

    ASTRBOT_AVAILABLE = True
except Exception:  # noqa: BLE001
    import logging

    Comp = None  # type: ignore[assignment]
    MessageChain = None  # type: ignore[assignment]
    logger = logging.getLogger("push_forwarder")
    ASTRBOT_AVAILABLE = False

STATUS_SENT = "sent"
STATUS_QUEUED = "queued"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
# 自测：本来会发到这个目标，但因为 dry_run 停在了发送前一步
STATUS_DRY_RUN = "dry_run"

PREVIEW_LENGTH = 80


class PendingQueue:
    """超出转发时段的待发消息队列，按目标分组，持久化以便重启后补发。"""

    def __init__(self, data_dir: Path, max_size: int = 50) -> None:
        self.path = data_dir / "queue.json"
        self.max_size = max(1, max_size)
        self.items: dict[str, list[dict[str, Any]]] = {}
        self._dirty = False
        self.load()

    def load(self) -> None:
        raw = load_json(self.path, {}) or {}
        items = raw.get("items") or {}
        if isinstance(items, dict):
            self.items = {
                str(k): [m for m in v if isinstance(m, dict)]
                for k, v in items.items()
                if isinstance(v, list)
            }

    def flush(self) -> None:
        if not self._dirty:
            return
        atomic_write_json(
            self.path,
            {"version": 1, "updated_at": now_ts(), "items": self.items},
        )
        self._dirty = False

    def push(self, target_id: str, message: PushMessage) -> None:
        bucket = self.items.setdefault(target_id, [])
        bucket.append(message.to_dict())
        if len(bucket) > self.max_size:
            # 积压超限时丢最旧的，保证补发的是较新的内容
            del bucket[: len(bucket) - self.max_size]
        self._dirty = True

    def take(self, target_id: str) -> list[PushMessage]:
        raw = self.items.pop(target_id, [])
        if raw:
            self._dirty = True
        return [PushMessage.from_dict(m) for m in raw]

    def drop(self, target_id: str) -> None:
        if self.items.pop(target_id, None) is not None:
            self._dirty = True

    def count(self, target_id: str | None = None) -> int:
        if target_id is not None:
            return len(self.items.get(target_id, []))
        return sum(len(v) for v in self.items.values())

    def target_ids(self) -> list[str]:
        return [k for k, v in self.items.items() if v]


class Dispatcher:
    """把一条推送分发到所有命中的转发目标。"""

    def __init__(
        self,
        context: Any,
        store: Any,
        sessions: Any,
        history: Any,
        config: Any,
        data_dir: Path,
    ) -> None:
        self.context = context
        self.store = store
        self.sessions = sessions
        self.history = history
        self.config = config
        self.queue = PendingQueue(data_dir, int(config.get("queue_max_size", 50) or 50))
        self._platform_name_cache: dict[str, str] = {}

    # ------------------------------------------------------------ 配置读取

    def _cfg(self, key: str, default: Any) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    # ------------------------------------------------------------ 平台能力

    def platform_name(self, platform_id: str) -> str:
        """由实例 id 反查适配器类型名（aiocqhttp / qq_official / weixin_oc ...）。"""
        cached = self._platform_name_cache.get(platform_id)
        if cached:
            return cached
        try:
            inst = self.context.get_platform_inst(platform_id)
            name = inst.meta().name if inst else ""
        except Exception:  # noqa: BLE001
            name = ""
        if name:
            self._platform_name_cache[platform_id] = name
        return name

    def invalidate_platform_cache(self) -> None:
        self._platform_name_cache.clear()

    def target_unsupported_reason(self, target: Target) -> str:
        """目标因平台能力而完全不可用时给出原因，可用则返回空串。"""
        caps = caps_of(self.platform_name(target.platform_id))
        if target.message_type == GROUP and not caps.group:
            return "该平台适配器不支持群消息"
        if target.message_type == FRIEND and not caps.private:
            return "该平台适配器不支持私聊消息"
        return ""

    # ------------------------------------------------------------ 目标筛选

    def select_targets(
        self,
        message: PushMessage,
    ) -> tuple[list[Target], list[dict[str, Any]]]:
        """挑出这条推送要发往的目标。

        返回 (可发送目标, 被排除的目标及原因)。显式指定 targets 时按 ID 精确匹配，
        否则按标签路由。
        """
        excluded: list[dict[str, Any]] = []

        if message.target_ids:
            candidates = []
            for tid in message.target_ids:
                target = self.store.get_target(tid)
                if target is None:
                    excluded.append(
                        {
                            "target_id": tid,
                            "status": STATUS_SKIPPED,
                            "detail": "目标不存在",
                        }
                    )
                else:
                    candidates.append(target)
        else:
            candidates = [
                t for t in self.store.list_targets() if t.matches_tags(message.tags)
            ]

        usable: list[Target] = []
        for target in candidates:
            if not target.enabled:
                excluded.append(self._record(target, STATUS_SKIPPED, "目标已停用"))
                continue
            if not self.store.bot_enabled(target.platform_id):
                excluded.append(self._record(target, STATUS_SKIPPED, "所属机器人已停用"))
                continue
            reason = self.target_unsupported_reason(target)
            if reason:
                excluded.append(self._record(target, STATUS_SKIPPED, reason))
                continue
            usable.append(target)
        return usable, excluded

    def _record(self, target: Target, status: str, detail: str) -> dict[str, Any]:
        return {
            "target_id": target.id,
            "platform_id": target.platform_id,
            "display_name": target.display_name or target.umo,
            "status": status,
            "detail": detail,
        }

    # ------------------------------------------------------------ 消息构建

    def build_components(
        self,
        target: Target,
        text: str,
        message: PushMessage,
    ) -> list[Any]:
        """构建消息组件列表，并按目标平台能力过滤掉不支持的组件。

        微信 ClawBot 不支持 At，这里会静默跳过而不是让整条消息发送失败。
        """
        if not ASTRBOT_AVAILABLE:
            return []

        components: list[Any] = []
        caps = caps_of(self.platform_name(target.platform_id))

        mode = message.at_mode if message.at_mode is not None else target.at_mode
        users = message.at_users if message.at_mode is not None else target.at_users

        if mode and mode != AT_NONE and target.message_type == GROUP:
            if not caps.at:
                logger.debug(
                    f"[push_forwarder] {target.platform_id} 不支持 @ 组件，"
                    f"已跳过（目标 {target.id}）"
                )
            else:
                components.extend(self._at_components(mode, users))

        components.append(Comp.Plain(text))
        return components

    @staticmethod
    def _at_components(mode: str, users: list[str]) -> list[Any]:
        """生成 @ 组件。不同平台对"@全体"的表示不一致，做多级兜底。"""
        result: list[Any] = []
        try:
            if mode == AT_ALL:
                at_all = getattr(Comp, "AtAll", None)
                result.append(at_all() if at_all else Comp.At(qq="all"))
            elif mode == AT_USERS:
                for user in users:
                    result.append(Comp.At(qq=user))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[push_forwarder] 构建 @ 组件失败，已忽略：{e}")
            return []
        return result

    @staticmethod
    def build_chain(components: list[Any]) -> Any:
        """把组件列表包成 MessageChain，兼容不同版本的构造方式。"""
        try:
            return MessageChain(chain=list(components))
        except TypeError:
            chain = MessageChain()
            try:
                chain.chain.extend(components)
            except AttributeError:  # 极旧版本只有链式 API
                for c in components:
                    text = getattr(c, "text", None)
                    if text:
                        chain.message(text)
            return chain

    def render_texts(self, message: PushMessage) -> list[str]:
        """渲染并按长度切分正文。"""
        text = render(message, str(self._cfg("format_template", "{title}\n{text}")))
        return split_text(
            text,
            int(self._cfg("format_max_length", 2000)),
            bool(self._cfg("format_split_long_message", True)),
        )

    # ------------------------------------------------------------ 分发主流程

    async def dispatch(self, message: PushMessage) -> dict[str, Any]:
        """分发一条推送，返回统计结果。"""
        targets, results = self.select_targets(message)

        to_send: list[Target] = []
        for target in targets:
            if message.urgent:
                to_send.append(target)
                continue
            schedule = self.store.resolve_schedule(target)
            if is_within(schedule):
                to_send.append(target)
                continue
            action = str((schedule or {}).get("outside_action") or OUTSIDE_QUEUE)
            if action == OUTSIDE_DROP:
                results.append(
                    self._record(
                        target,
                        STATUS_SKIPPED,
                        f"不在转发时段（{describe(schedule)}）",
                    )
                )
            else:
                # 自测不入队：排进去就会在时段开始时真发出去，那就不是自测了
                if not message.dry_run:
                    self.queue.push(target.id, message)
                nxt = next_open_at(schedule)
                when = nxt.strftime("%m-%d %H:%M") if nxt else "未知"
                prefix = "不在转发时段，实发时会排队到" if message.dry_run else "不在转发时段，将于"
                results.append(
                    self._record(
                        target,
                        STATUS_QUEUED,
                        f"{prefix} {when} 补发",
                    )
                )

        if message.dry_run:
            # 走到这里说明解析、路由、时段判定都通过了，唯独不真发。
            # 也不写 history —— 自测不该混进推送记录里。
            for target in to_send:
                results.append(
                    self._record(
                        target, STATUS_DRY_RUN, "链路正常，实发时这条会送到这里"
                    )
                )
            return {
                "summary": self._summarize(results),
                "results": results,
                "dry_run": True,
            }

        if to_send:
            results.extend(await self._send_all(to_send, message))

        self.queue.flush()
        summary = self._summarize(results)
        self._write_history(message, results, summary)
        return {"summary": summary, "results": results}

    async def _send_all(
        self,
        targets: list[Target],
        message: PushMessage,
    ) -> list[dict[str, Any]]:
        """按机器人分组发送：组间并发，组内串行并保持间隔以规避风控。"""
        groups: dict[str, list[Target]] = {}
        for target in targets:
            groups.setdefault(target.platform_id, []).append(target)

        texts = self.render_texts(message)
        sem = asyncio.Semaphore(max(1, int(self._cfg("dispatch_concurrency", 4))))
        interval = max(0, int(self._cfg("dispatch_interval_ms", 600))) / 1000

        async def run_group(items: list[Target]) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            async with sem:
                for idx, target in enumerate(items):
                    out.append(await self._send_one(target, texts, message))
                    if interval and idx < len(items) - 1:
                        await asyncio.sleep(interval)
            return out

        grouped = await asyncio.gather(
            *(run_group(items) for items in groups.values()),
            return_exceptions=True,
        )

        results: list[dict[str, Any]] = []
        for item in grouped:
            if isinstance(item, BaseException):
                logger.error(f"[push_forwarder] 发送分组异常：{item}")
                continue
            results.extend(item)
        return results

    async def _send_one(
        self,
        target: Target,
        texts: list[str],
        message: PushMessage,
    ) -> dict[str, Any]:
        """向单个目标发送（可能是多条），失败按指数退避重试。"""
        if not ASTRBOT_AVAILABLE:
            return self._record(target, STATUS_FAILED, "astrbot 运行时不可用")

        retries = max(0, int(self._cfg("dispatch_retry_times", 2)))
        interval = max(0, int(self._cfg("dispatch_interval_ms", 600))) / 1000
        last_error = ""

        for attempt in range(retries + 1):
            try:
                for idx, text in enumerate(texts):
                    components = self.build_components(target, text, message)
                    ok = await self.context.send_message(
                        target.umo, self.build_chain(components)
                    )
                    if not ok:
                        raise RuntimeError("未找到对应的机器人实例，可能已被删除或未启动")
                    if interval and idx < len(texts) - 1:
                        await asyncio.sleep(interval)
                detail = f"已发送 {len(texts)} 条" if len(texts) > 1 else "已发送"
                return self._record(target, STATUS_SENT, detail)
            except Exception as e:  # noqa: BLE001
                last_error = str(e) or type(e).__name__
                if attempt < retries:
                    await asyncio.sleep(2**attempt)

        logger.warning(
            f"[push_forwarder] 发送到 {target.display_name or target.umo} "
            f"失败：{last_error}"
        )
        return self._record(target, STATUS_FAILED, last_error)

    # ------------------------------------------------------------ 队列补发

    async def flush_queue(self) -> int:
        """把已进入转发时段的排队消息发出去，返回实际发送的目标数。"""
        sent = 0
        for target_id in self.queue.target_ids():
            target = self.store.get_target(target_id)
            if target is None or not target.enabled:
                self.queue.drop(target_id)
                continue
            schedule = self.store.resolve_schedule(target)
            if not is_within(schedule):
                continue

            pending = self.queue.take(target_id)
            if not pending:
                continue

            if bool(self._cfg("dispatch_merge_queued", True)):
                merged = self._merge(pending)
            else:
                merged = pending
            for message in merged:
                texts = self.render_texts(message)
                result = await self._send_one(target, texts, message)
                self.history.add(
                    {
                        "ts": now_ts(),
                        "title": message.title,
                        "preview": self._preview(message),
                        "tags": message.tags,
                        "source": "queue",
                        "results": [result],
                        "summary": self._summarize([result]),
                    }
                )
                if result["status"] == STATUS_SENT:
                    sent += 1
            self.queue.flush()
        self.history.flush()
        return sent

    @staticmethod
    def _merge(messages: list[PushMessage]) -> list[PushMessage]:
        """把积压的多条合并成一条，避免时段开始时刷屏。"""
        if len(messages) <= 1:
            return messages
        blocks = []
        for msg in messages:
            part = "\n".join(p for p in (msg.title, msg.text) if p)
            if part:
                blocks.append(part)
        merged = PushMessage(
            title=f"转发时段内补发 {len(messages)} 条消息",
            text="\n\n———\n\n".join(blocks),
            tags=messages[-1].tags,
            at_mode=messages[-1].at_mode,
            at_users=messages[-1].at_users,
            received_at=messages[-1].received_at,
        )
        return [merged]

    # ------------------------------------------------------------ 统计记账

    @staticmethod
    def _summarize(results: list[dict[str, Any]]) -> dict[str, int]:
        summary = {
            STATUS_SENT: 0,
            STATUS_QUEUED: 0,
            STATUS_SKIPPED: 0,
            STATUS_FAILED: 0,
            STATUS_DRY_RUN: 0,
        }
        for item in results:
            status = item.get("status")
            if status in summary:
                summary[status] += 1
        return summary

    @staticmethod
    def _preview(message: PushMessage) -> str:
        parts = " ".join(p for p in (message.title, message.text) if p)
        text = parts.replace("\n", " ")
        return text if len(text) <= PREVIEW_LENGTH else text[:PREVIEW_LENGTH] + "…"

    def _write_history(
        self,
        message: PushMessage,
        results: list[dict[str, Any]],
        summary: dict[str, int],
    ) -> None:
        self.history.add(
            {
                "ts": now_ts(),
                "title": message.title,
                "preview": self._preview(message),
                "tags": message.tags,
                "source": "push",
                "results": results,
                "summary": summary,
            }
        )
        self.history.flush()
