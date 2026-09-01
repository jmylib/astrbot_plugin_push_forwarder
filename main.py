"""astrbot_plugin_push_forwarder —— 推送转发插件。

接收外部 HTTP 推送，按可视化配置转发到多个机器人实例的群聊 / 私聊，
支持按机器人设置转发时段、标签路由、@ 提醒与超时段排队补发。

设计要点见 README.md。这里只做整合：生命周期、事件监听、指令、Web API。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.dispatcher import Dispatcher
from .core.models import FRIEND, GROUP, caps_of, default_schedule
from .core.payload import PushMessage
from .core.receiver import Receiver
from .core.schedule import describe, next_open_at
from .core.session_registry import SessionRegistry, short_id
from .core.store import HistoryStore, TargetStore
from .core.utils import gen_token, now_ts

PLUGIN_NAME = "astrbot_plugin_push_forwarder"
# Dashboard 转发到 /api/v1/plugins/extensions/<plugin_name>/... 时用的 <plugin_name>
# 在不同版本里可能是完整名，也可能是去掉 astrbot_plugin_ 前缀的短名。两个都注册，
# 免得因为前缀对不上导致面板所有接口 404、页面卡在"加载中"。
ROUTE_PREFIXES = tuple(
    dict.fromkeys([PLUGIN_NAME, PLUGIN_NAME.replace("astrbot_plugin_", "", 1)])
)
MAINTENANCE_INTERVAL = 60
PRUNE_EVERY_N_ROUNDS = 60

# Web API / 插件 Page 依赖较新版本的 AstrBot；缺失时插件降级为"指令 + 配置"模式
try:
    from astrbot.api.web import json_response, request

    WEB_API_AVAILABLE = True
except Exception:  # noqa: BLE001
    json_response = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]
    WEB_API_AVAILABLE = False

try:
    from astrbot.api.star import register
except Exception:  # noqa: BLE001

    def register(*args: Any, **kwargs: Any):  # type: ignore[misc]
        """旧版本没有 register 装饰器时的空实现，元数据以 metadata.yaml 为准。"""

        def deco(cls):
            return cls

        return deco


def _data_dir() -> Path:
    """插件数据目录。按 AstrBot 规范放在 data/ 下，不写插件目录。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(get_astrbot_data_path())
    except Exception:  # noqa: BLE001
        base = Path("data")
    path = base / "push_forwarder"
    path.mkdir(parents=True, exist_ok=True)
    return path


@register(
    PLUGIN_NAME,
    "jimmy",
    "接收 HTTP 推送并转发到多个机器人的群/私聊，支持按机器人设置转发时段",
    "1.0.8",
)
class PushForwarder(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = _data_dir()

        self.store = TargetStore(self.data_dir)
        self.sessions = SessionRegistry(
            self.data_dir,
            max_count=int(config.get("session_max_count", 500) or 500),
            expire_days=int(config.get("session_expire_days", 30) or 30),
        )
        self.history = HistoryStore(
            self.data_dir,
            max_records=int(config.get("history_max_records", 200) or 200),
        )
        self.dispatcher = Dispatcher(
            context, self.store, self.sessions, self.history, config, self.data_dir
        )
        self.receiver = Receiver(config, self._on_push)

        self._ensure_token()
        self._register_web_apis()

        self._stopping = False
        self._tasks: list[asyncio.Task] = []
        # __init__ 里不一定有运行中的事件循环（取决于 AstrBot 的加载路径），
        # 拿不到就什么也不做，等 initialize() 再来一次。少了这个兜底，
        # 插件看着加载成功，端口却永远不会开。
        self._ensure_tasks()

    # ------------------------------------------------------------------ 生命周期

    def _spawn(self, coro: Any, name: str) -> bool:
        try:
            self._tasks.append(asyncio.create_task(coro, name=f"push_forwarder:{name}"))
            return True
        except RuntimeError as e:
            # 没有运行中的事件循环时要主动关闭协程，否则会泄漏并刷 "never awaited" 警告
            coro.close()
            logger.debug(f"[push_forwarder] 暂时起不了后台任务 {name}（{e}），留给 initialize")
            return False

    def _ensure_tasks(self) -> None:
        """启动后台任务。可重复调用，已经起过就直接返回。"""
        if self._stopping or self._tasks:
            return
        if not self._spawn(self._startup(), "startup"):
            return
        self._spawn(self._maintenance_loop(), "maintenance")

    async def initialize(self) -> None:
        """AstrBot 的异步初始化钩子。这里一定有事件循环，是端口真正开起来的地方。"""
        self._ensure_tasks()

    def _ensure_token(self) -> None:
        """首次加载时自动生成推送 Token 并写回配置，免去用户手动设置。"""
        if not str(self.config.get("receiver_token") or ""):
            token = gen_token()
            self.config["receiver_token"] = token
            try:
                self.config.save_config()
                logger.info("[push_forwarder] 已自动生成推送 Token，可在插件配置或面板中查看")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[push_forwarder] 保存自动生成的 Token 失败：{e}")

    async def _startup(self) -> None:
        if not bool(self.config.get("receiver_enabled", True)):
            self.receiver.last_error = "已在插件配置中关闭"
            logger.info("[push_forwarder] 推送接收服务已在配置中关闭，不会监听任何端口")
            return
        if await self.receiver.start():
            return
        logger.error(
            f"[push_forwarder] 端口 {self.receiver.port} 没能监听起来："
            f"{self.receiver.last_error}。常见原因：端口被别的程序占用、"
            "该端口在系统/容器里不可用。换个端口后重载插件即可重试。"
        )

    async def _maintenance_loop(self) -> None:
        """定时补发排队消息、落盘缓存、清理过期会话。"""
        rounds = 0
        while not self._stopping:
            try:
                await asyncio.sleep(MAINTENANCE_INTERVAL)
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            try:
                sent = await self.dispatcher.flush_queue()
                if sent:
                    logger.info(f"[push_forwarder] 进入转发时段，补发了 {sent} 个目标的积压消息")

                rounds += 1
                if rounds % PRUNE_EVERY_N_ROUNDS == 0:
                    removed = self.sessions.prune(self.store.known_umos())
                    if removed:
                        logger.debug(f"[push_forwarder] 清理了 {removed} 个过期会话")

                self.sessions.flush()
                self.history.flush()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.error(f"[push_forwarder] 后台维护任务出错：{e}")

    async def terminate(self) -> None:
        """插件停用/热重载时释放端口与落盘。不实现这个会导致重载后端口被占用。"""
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

        await self.receiver.stop()
        try:
            self.sessions.flush()
            self.history.flush()
            self.dispatcher.queue.flush()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[push_forwarder] 退出时落盘失败：{e}")
        logger.info("[push_forwarder] 已停止")

    # ------------------------------------------------------------------ 会话发现

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def record_session(self, event: AstrMessageEvent):
        """记录出现过的会话，供面板勾选。

        QQ 官方机器人的群 openid 只能这样拿到，因此这是转发目标的主要来源。
        不调用 stop_event，不影响其他插件。
        """
        if not bool(self.config.get("session_discovery_enabled", True)):
            return
        self.sessions.record(event)

    # ------------------------------------------------------------------ 推送入口

    async def _on_push(self, message: PushMessage) -> dict[str, Any]:
        return await self.dispatcher.dispatch(message)

    # -------------------------------------------------------------------- 指令

    @filter.command_group("fwd")
    def fwd(self):
        """推送转发管理指令。"""

    @fwd.command("here")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def fwd_here(self, event: AstrMessageEvent, tag: str = ""):
        """把当前会话添加为转发目标，可附带一个标签。"""
        umo = event.unified_msg_origin
        platform_id = event.get_platform_id()
        message_type = self._message_type_of(event, umo)

        caps = caps_of(event.get_platform_name() or "")
        if message_type == GROUP and not caps.group:
            yield event.plain_result("该平台适配器不支持群消息转发，无法添加。")
            return

        entry = self.sessions.get(umo) or {}
        display = entry.get("display_name") or self._fallback_name(event, message_type)

        target, created = self.store.add_target(
            platform_id=platform_id,
            umo=umo,
            message_type=message_type,
            display_name=display,
            tags=[tag] if tag else None,
        )
        schedule = self.store.resolve_schedule(target)
        lines = [
            "已添加为转发目标。" if created else "该会话已是转发目标，配置已更新。",
            f"名称：{target.display_name}",
            f"编号：{target.id}",
            f"标签：{'、'.join(target.tags) if target.tags else '（无，接收未指定标签的推送）'}",
            f"时段：{describe(schedule)}",
        ]
        yield event.plain_result("\n".join(lines))

    @fwd.command("rm")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def fwd_rm(self, event: AstrMessageEvent):
        """把当前会话从转发目标中移除。"""
        if self.store.remove_by_umo(event.unified_msg_origin):
            yield event.plain_result("已移除，本会话不再接收转发推送。")
        else:
            yield event.plain_result("本会话不在转发目标列表中。")

    @fwd.command("list")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def fwd_list(self, event: AstrMessageEvent):
        """列出所有转发目标。"""
        targets = self.store.list_targets()
        if not targets:
            yield event.plain_result(
                "还没有配置任何转发目标。\n"
                "在需要接收推送的群或私聊里发送 /fwd here 即可添加，"
                "也可以在 WebUI 的「推送转发」面板中勾选。"
            )
            return

        lines = [f"共 {len(targets)} 个转发目标："]
        for target in targets:
            mark = "○" if not target.enabled else "●"
            kind = "群" if target.message_type == GROUP else "私"
            tags = f" [{'、'.join(target.tags)}]" if target.tags else ""
            queued = self.dispatcher.queue.count(target.id)
            backlog = f" 积压{queued}条" if queued else ""
            lines.append(
                f"{mark} [{kind}] {target.display_name or short_id(target.umo, 12)}"
                f"{tags} ({target.id}){backlog}"
            )
        yield event.plain_result("\n".join(lines))

    @fwd.command("test")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def fwd_test(self, event: AstrMessageEvent):
        """向当前会话所对应的转发目标发送一条测试消息。"""
        target = self.store.find_by_umo(event.unified_msg_origin)
        if target is None:
            yield event.plain_result("本会话不是转发目标，请先用 /fwd here 添加。")
            return

        message = PushMessage(
            title="推送转发测试",
            text=f"这是一条测试消息，发送时间 {self._now_text()}。",
            urgent=True,
        )
        result = await self.dispatcher.dispatch(message)
        detail = next(
            (r for r in result["results"] if r.get("target_id") == target.id), None
        )
        if detail and detail.get("status") == "sent":
            yield event.plain_result("测试消息已发送。")
        else:
            reason = (detail or {}).get("detail", "未命中该目标")
            yield event.plain_result(f"测试未成功：{reason}")

    @fwd.command("info")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def fwd_info(self, event: AstrMessageEvent):
        """查看推送地址与服务状态。"""
        token = str(self.config.get("receiver_token") or "")
        bound = self.receiver.bound_addresses()
        if self.receiver.running:
            state = f"运行中（实际监听 {bound}）" if bound else "运行中"
        else:
            state = f"未运行（{self.receiver.last_error or '原因未知'}）"
        lines = [
            f"接收服务：{state}",
            f"监听地址：{self.receiver.host}:{self.receiver.port}",
            f"推送路径：{self.receiver.path}",
            f"Token：{token[:4] + '****' + token[-4:] if len(token) > 8 else '（未设置）'}",
            f"转发目标：{len(self.store.list_targets())} 个",
            f"待补发：{self.dispatcher.queue.count()} 条",
        ]
        if not self.receiver.running:
            lines += ["", "端口没开时可以发 /fwd start 就地重试，失败原因会直接回给你。"]
        elif bound:
            lines += [
                "",
                "端口在 AstrBot 进程里确实开着。外部仍连不上的话，多半是 AstrBot 跑在 "
                "Docker 里而这个端口没映射出来，或者被防火墙拦了。",
            ]
        lines += ["", "发送 /fwd url 获取可直接复制的完整推送地址。"]
        yield event.plain_result("\n".join(lines))

    @fwd.command("start")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def fwd_start(self, event: AstrMessageEvent):
        """就地重启接收服务，把失败原因直接回给管理员，省得翻日志。"""
        if not bool(self.config.get("receiver_enabled", True)):
            yield event.plain_result(
                "推送接收服务在插件配置里是关闭的，请先在 WebUI 插件配置中打开"
                "「启用 HTTP 推送接收服务」，再重载插件。"
            )
            return
        ok = await self.receiver.restart()
        if not ok:
            yield event.plain_result(
                f"启动失败：{self.receiver.last_error}\n"
                "端口被占用就换一个（插件配置 → 监听端口），改完重载插件。"
            )
            return
        bound = self.receiver.bound_addresses()
        where = bound or f"{self.receiver.host}:{self.receiver.port}"
        yield event.plain_result(
            f"接收服务已启动，实际监听 {where}。\n"
            "从外部还是连不上的话，检查 Docker 端口映射和防火墙。"
        )

    @fwd.command("url")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def fwd_url(self, event: AstrMessageEvent):
        """生成带 Token 的完整推送地址，可直接粘贴到推送工具里。"""
        token = str(self.config.get("receiver_token") or "")
        if not token:
            yield event.plain_result("尚未生成 Token，请重载插件或在插件配置中设置。")
            return

        base = f"http://{self._guess_host()}:{self.receiver.port}{self.receiver.path}"
        is_private = self._message_type_of(event, event.unified_msg_origin) == FRIEND
        if not is_private:
            # Token 等同于口令，不往群里贴
            yield event.plain_result(
                f"推送地址：{base}\n"
                "完整地址含 Token，不便在群里显示。请私聊机器人发送 /fwd url，"
                "或在 WebUI 的「推送转发」面板中点「复制完整地址」。"
            )
            return

        yield event.plain_result(
            f"完整推送地址（含 Token，请妥善保管）：\n{base}?token={token}\n\n"
            f"也可以用请求头的方式：\n{base}\n请求头 X-Token: {token}\n\n"
            "推送方是「企业微信机器人」通道的话，把它的 Webhook 地址换成：\n"
            f"{base}?key={token}\n消息体不用改。\n\n"
            "如果推送方不在本机，把地址里的主机名换成它能访问到的 IP 或域名。"
        )

    # ------------------------------------------------------------------ 指令辅助

    def _guess_host(self) -> str:
        """猜一个推送方能用的主机名。

        监听 0.0.0.0 时服务端并不知道对方该走哪个地址，这里取本机在默认路由上的
        IP —— 内网推送多半可用；跨网段或有反代时仍需用户自行替换。
        """
        host = self.receiver.host
        if host not in ("0.0.0.0", "::", ""):
            return host
        import socket

        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("223.5.5.5", 53))  # 不发包，只为拿到出口网卡地址
            return sock.getsockname()[0]
        except Exception:  # noqa: BLE001
            return "你的服务器IP"
        finally:
            if sock is not None:
                sock.close()

    @staticmethod
    def _message_type_of(event: AstrMessageEvent, umo: str) -> str:
        parts = umo.split(":", 2)
        if len(parts) >= 2 and parts[1]:
            return parts[1]
        return GROUP if event.get_group_id() else FRIEND

    @staticmethod
    def _fallback_name(event: AstrMessageEvent, message_type: str) -> str:
        if message_type == GROUP:
            group_id = event.get_group_id() or ""
            return f"群 {short_id(group_id)}" if group_id else "群聊"
        return event.get_sender_name() or "私聊"

    @staticmethod
    def _now_text() -> str:
        from datetime import datetime

        return datetime.now().strftime("%H:%M:%S")

    # ------------------------------------------------------------------ Web API

    def _register_web_apis(self) -> None:
        """注册面板用的后端接口。

        路由必须带插件名前缀；面板侧通过 bridge 调用时不需要写前缀。
        GET / POST 使用不同路由而非同路由多方法，避免依赖注册表的去重细节。
        """
        if not WEB_API_AVAILABLE:
            logger.warning(
                "[push_forwarder] 当前 AstrBot 版本不支持插件 Web API，"
                "可视化面板不可用，请使用 /fwd 指令管理转发目标"
            )
            return

        routes = [
            ("bots", self.api_bots, ["GET"], "机器人实例与能力"),
            ("sessions", self.api_sessions, ["GET"], "已发现的会话"),
            ("sessions/refresh", self.api_sessions_refresh, ["POST"], "从平台拉取会话列表"),
            ("sessions/rename", self.api_session_rename, ["POST"], "修改会话备注"),
            ("targets", self.api_targets, ["GET"], "转发目标列表"),
            ("targets/save", self.api_targets_save, ["POST"], "保存转发目标"),
            ("schedule/save", self.api_schedule_save, ["POST"], "保存机器人转发时段"),
            ("test", self.api_test, ["POST"], "发送测试消息"),
            ("history", self.api_history, ["GET"], "推送历史"),
            ("webhook", self.api_webhook, ["GET"], "推送地址与状态"),
            ("ping", self.api_ping, ["GET"], "连通性自检"),
        ]
        registered: list[str] = []
        for prefix in ROUTE_PREFIXES:
            for path, handler, methods, desc in routes:
                route = f"/{prefix}/{path}"
                try:
                    self.context.register_web_api(route, handler, methods, desc)
                    registered.append(route)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[push_forwarder] 注册接口 {route} 失败：{e}")

        if registered:
            logger.info(
                f"[push_forwarder] 已注册 {len(registered)} 个面板接口，前缀："
                f"{'、'.join(ROUTE_PREFIXES)}"
            )
        else:
            logger.error(
                "[push_forwarder] 面板接口一个都没注册成功，可视化面板将无法加载，"
                "请改用 /fwd 指令管理转发目标"
            )

    async def api_ping(self):
        """面板起不来时用来判断是接口不通还是页面本身的问题。"""
        return json_response(
            {
                "status": "ok",
                "data": {"plugin": PLUGIN_NAME, "server_time": now_ts()},
            }
        )

    async def api_bots(self):
        """列出所有平台实例，附带能力矩阵与当前时段配置。"""
        bots = []
        try:
            instances = self.context.platform_manager.platform_insts
        except Exception as e:  # noqa: BLE001
            logger.error(f"[push_forwarder] 读取平台实例失败：{e}")
            instances = []

        for inst in instances:
            try:
                meta = inst.meta()
            except Exception:  # noqa: BLE001
                continue
            platform_id = getattr(meta, "id", "") or ""
            platform_name = getattr(meta, "name", "") or ""
            caps = caps_of(platform_name)
            cfg = self.store.get_bot(platform_id)
            bots.append(
                {
                    "platform_id": platform_id,
                    "platform_name": platform_name,
                    "description": getattr(meta, "description", "") or "",
                    "caps": {
                        "group": caps.group,
                        "private": caps.private,
                        "at": caps.at,
                        "listable": caps.listable,
                        "note": caps.note,
                    },
                    "enabled": cfg.enabled,
                    "remark": cfg.remark,
                    "schedule": cfg.schedule or default_schedule(),
                    "schedule_text": describe(cfg.schedule),
                    "target_count": len(self.store.list_targets(platform_id)),
                    "session_count": len(self.sessions.list(platform_id=platform_id)),
                }
            )
        return json_response({"status": "ok", "data": {"bots": bots}})

    async def api_sessions(self):
        """返回某个机器人下已发现的会话，供面板勾选。"""
        platform_id = request.query.get("platform_id", "") or ""
        message_type = request.query.get("type", "") or ""
        keyword = request.query.get("q", "") or ""

        selected = self.store.known_umos()
        items = []
        for entry in self.sessions.list(
            platform_id=platform_id or None,
            message_type=message_type or None,
            keyword=keyword,
        ):
            item = dict(entry)
            item["selected"] = entry.get("umo") in selected
            item["short_id"] = short_id(entry.get("session_id") or "", 10)
            items.append(item)
        return json_response(
            {
                "status": "ok",
                "data": {
                    "sessions": items,
                    "discovery_enabled": bool(
                        self.config.get("session_discovery_enabled", True)
                    ),
                },
            }
        )

    async def api_sessions_refresh(self):
        """对支持列表查询的平台（目前是 OneBot 系）拉取真实的群/好友列表。"""
        body = await self._json_body()
        platform_id = str(body.get("platform_id") or "")
        if not platform_id:
            return json_response({"status": "error", "message": "缺少 platform_id"})

        inst = self.context.get_platform_inst(platform_id)
        if inst is None:
            return json_response({"status": "error", "message": "机器人实例不存在"})

        platform_name = ""
        try:
            platform_name = inst.meta().name or ""
        except Exception:  # noqa: BLE001
            pass

        if not caps_of(platform_name).listable:
            return json_response(
                {
                    "status": "error",
                    "message": f"{platform_name or '该平台'} 不支持查询会话列表，"
                    "请让机器人在目标会话中收一条消息，或在该会话发送 /fwd here",
                }
            )

        try:
            count = await self._refresh_onebot(inst, platform_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[push_forwarder] 拉取会话列表失败：{e}")
            return json_response({"status": "error", "message": f"拉取失败：{e}"})

        self.sessions.flush()
        return json_response({"status": "ok", "data": {"count": count}})

    async def _refresh_onebot(self, inst: Any, platform_id: str) -> int:
        """调用 OneBot v11 的 get_group_list / get_friend_list。"""
        client = inst.get_client()
        items: list[dict[str, Any]] = []

        try:
            groups = await client.api.call_action("get_group_list") or []
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[push_forwarder] get_group_list 失败：{e}")
            groups = []
        for group in groups:
            gid = str(group.get("group_id") or "")
            if not gid:
                continue
            items.append(
                {
                    "umo": f"{platform_id}:{GROUP}:{gid}",
                    "platform_id": platform_id,
                    "platform_name": "aiocqhttp",
                    "message_type": GROUP,
                    "session_id": gid,
                    "group_id": gid,
                    "display_name": str(group.get("group_name") or f"群 {gid}"),
                }
            )

        try:
            friends = await client.api.call_action("get_friend_list") or []
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[push_forwarder] get_friend_list 失败：{e}")
            friends = []
        for friend in friends:
            uid = str(friend.get("user_id") or "")
            if not uid:
                continue
            name = str(friend.get("remark") or friend.get("nickname") or uid)
            items.append(
                {
                    "umo": f"{platform_id}:{FRIEND}:{uid}",
                    "platform_id": platform_id,
                    "platform_name": "aiocqhttp",
                    "message_type": FRIEND,
                    "session_id": uid,
                    "group_id": "",
                    "display_name": name,
                }
            )

        return self.sessions.upsert_from_api(items)

    async def api_session_rename(self):
        body = await self._json_body()
        umo = str(body.get("umo") or "")
        name = str(body.get("display_name") or "").strip()
        if not umo or not name:
            return json_response({"status": "error", "message": "参数不完整"})
        if not self.sessions.set_display_name(umo, name):
            return json_response({"status": "error", "message": "会话不存在"})
        target = self.store.find_by_umo(umo)
        if target is not None:
            target.display_name = name
            self.store.save()
        self.sessions.flush()
        return json_response(
            {"status": "ok", "data": {"umo": umo, "display_name": name}}
        )

    async def api_targets(self):
        targets = []
        for target in self.store.list_targets():
            item = target.to_dict()
            schedule = self.store.resolve_schedule(target)
            item["schedule_text"] = describe(schedule)
            item["queued"] = self.dispatcher.queue.count(target.id)
            item["unsupported"] = self.dispatcher.target_unsupported_reason(target)
            nxt = next_open_at(schedule)
            item["next_open_at"] = nxt.isoformat() if nxt else None
            targets.append(item)
        return json_response({"status": "ok", "data": {"targets": targets}})

    async def api_targets_save(self):
        """整表保存转发目标。

        只接受已被发现或已存在的 umo，避免面板被构造出的请求写入任意会话。
        """
        body = await self._json_body()
        items = body.get("targets")
        if not isinstance(items, list):
            return json_response({"status": "error", "message": "targets 必须是数组"})

        allowed = self.store.known_umos() | set(self.sessions.sessions.keys())
        rejected = []
        accepted = []
        for item in items:
            if not isinstance(item, dict):
                continue
            umo = str(item.get("umo") or "")
            if umo not in allowed:
                rejected.append(umo)
                continue
            accepted.append(item)

        self.store.replace_targets(accepted)
        self.dispatcher.invalidate_platform_cache()
        if rejected:
            logger.warning(f"[push_forwarder] 已忽略 {len(rejected)} 个未知会话的保存请求")
        return json_response(
            {
                "status": "ok",
                "data": {"saved": len(accepted), "rejected": len(rejected)},
            }
        )

    async def api_schedule_save(self):
        """保存某个机器人的启用状态、备注与转发时段。"""
        body = await self._json_body()
        platform_id = str(body.get("platform_id") or "")
        if not platform_id:
            return json_response({"status": "error", "message": "缺少 platform_id"})

        cfg = self.store.get_bot(platform_id)
        if "enabled" in body:
            cfg.enabled = bool(body.get("enabled"))
        if "remark" in body:
            cfg.remark = str(body.get("remark") or "")
        schedule = body.get("schedule")
        if isinstance(schedule, dict):
            cfg.schedule = self._sanitize_schedule(schedule)
        self.store.set_bot(platform_id, cfg)

        return json_response(
            {
                "status": "ok",
                "data": {
                    "platform_id": platform_id,
                    "schedule": cfg.schedule,
                    "schedule_text": describe(cfg.schedule),
                },
            }
        )

    @staticmethod
    def _sanitize_schedule(raw: dict[str, Any]) -> dict[str, Any]:
        """只保留已知字段，时间格式交给 schedule 模块判定，非法项会被它跳过。"""
        base = default_schedule()
        base["enabled"] = bool(raw.get("enabled"))
        base["timezone"] = str(raw.get("timezone") or base["timezone"])
        days = [int(d) for d in (raw.get("days") or []) if str(d).isdigit()]
        base["days"] = [d for d in days if 1 <= d <= 7] or base["days"]
        ranges = []
        for item in raw.get("ranges") or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                ranges.append([str(item[0]), str(item[1])])
        base["ranges"] = ranges or base["ranges"]
        action = str(raw.get("outside_action") or base["outside_action"])
        valid_actions = ("drop", "queue", "send")
        base["outside_action"] = action if action in valid_actions else "queue"
        return base

    async def api_test(self):
        """向指定目标发送测试消息，忽略时段限制。"""
        body = await self._json_body()
        target_ids = [str(t) for t in (body.get("target_ids") or []) if t]
        if not target_ids:
            return json_response({"status": "error", "message": "请先选择要测试的目标"})

        message = PushMessage(
            title="推送转发测试",
            text=str(body.get("text") or "这是一条来自 WebUI 面板的测试消息。"),
            target_ids=target_ids,
            urgent=True,
        )
        result = await self.dispatcher.dispatch(message)
        return json_response({"status": "ok", "data": result})

    async def api_history(self):
        try:
            limit = int(request.query.get("limit", "30") or 30)
        except (TypeError, ValueError):
            limit = 30
        return json_response(
            {"status": "ok", "data": {"records": self.history.list(min(200, limit))}}
        )

    async def api_webhook(self):
        """推送地址、Token 与服务状态，供面板顶部展示。"""
        token = str(self.config.get("receiver_token") or "")
        return json_response(
            {
                "status": "ok",
                "data": {
                    "enabled": bool(self.config.get("receiver_enabled", True)),
                    "running": self.receiver.running,
                    "last_error": self.receiver.last_error,
                    "bound": self.receiver.bound_addresses(),
                    "host": self.receiver.host,
                    "port": self.receiver.port,
                    "path": self.receiver.path,
                    "token": token,
                    "queued": self.dispatcher.queue.count(),
                    "target_count": len(self.store.list_targets()),
                    "server_time": now_ts(),
                },
            }
        )

    @staticmethod
    async def _json_body() -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return {}
        return body if isinstance(body, dict) else {}
