"""HTTP 推送接收服务。

独立监听一个端口，而不是挂在 AstrBot 的 WebUI 端口上：Dashboard 对 /api
全路径启用了 JWT 鉴权中间件，外部推送方无法提供 Dashboard 的 token。
这里自管一套简单的 Token 鉴权，推送方只需要一个 URL 和一个 Token。
所有路径都要过鉴权（含 /health），未知路径一律 404，不对外暴露任何免鉴权入口。

同时兼容企业微信群机器人的协议：地址里的 ``?key=`` 等价于 Token，消息体认
``msgtype``，响应带 ``errcode``。这样推送方那边现成的「企业微信机器人」通道
只要把 Webhook 地址换成本插件的地址就能用，不必改推送方的代码。
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Awaitable, Callable

from .payload import (
    PayloadError,
    PushMessage,
    is_wecom_payload,
    merge_query_routing,
    parse_payload,
    query_to_dict,
)
from .utils import ip_allowed

try:  # pragma: no cover
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except Exception:  # noqa: BLE001
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:  # pragma: no cover
    from astrbot.api import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger("push_forwarder")

PushHandler = Callable[[PushMessage], Awaitable[dict[str, Any]]]


def normalize_path(path: str) -> str:
    """把用户填的路径规范成 /xxx 形式。"""
    text = (path or "/push").strip()
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or "/push"


class Receiver:
    """接收外部推送并交给分发器。"""

    def __init__(self, config: Any, handler: PushHandler) -> None:
        self.config = config
        self.handler = handler
        self._runner: Any = None
        self._site: Any = None
        self.running = False
        # 面板和 /fwd info 会显示这个字段。给个初值，免得"还没启动"被显示成"原因未知"
        self.last_error = "尚未启动"
        self._rejects = 0
        self._rejects_logged = 0
        self._reject_logged_at = 0.0

    # ---------------------------------------------------------------- 生命周期

    @property
    def path(self) -> str:
        return normalize_path(str(self.config.get("receiver_path") or "/push"))

    @property
    def port(self) -> int:
        try:
            return int(self.config.get("receiver_port") or 9966)
        except (TypeError, ValueError):
            return 9966

    @property
    def host(self) -> str:
        return str(self.config.get("receiver_host") or "0.0.0.0")

    async def start(self) -> bool:
        """启动监听。端口被占用等错误只记日志，不影响插件其余功能。"""
        if not AIOHTTP_AVAILABLE:
            self.last_error = "aiohttp 不可用，无法启动推送接收服务"
            logger.error(f"[push_forwarder] {self.last_error}")
            return False
        if self.running:
            return True

        # 推送内容是纯文本，256 KB 绰绰有余。上限压低一点，
        # 未通过鉴权的请求体也要先读进内存，别给人留个塞大包的口子。
        app = web.Application(client_max_size=256 * 1024)
        path = self.path
        app.router.add_route("POST", path, self._handle_push)
        app.router.add_route("GET", path, self._handle_push)
        app.router.add_route("POST", f"{path}/{{tag}}", self._handle_push)
        app.router.add_route("GET", f"{path}/{{tag}}", self._handle_push)
        app.router.add_route("GET", "/health", self._handle_health)
        # 兜底放最后：其余路径一律 404，不透露这里跑着什么服务
        app.router.add_route("*", "/{tail:.*}", self._handle_unknown)

        try:
            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.host, self.port)
            await self._site.start()
        except OSError as e:
            self.last_error = f"端口 {self.port} 启动失败：{e}"
            logger.error(f"[push_forwarder] {self.last_error}")
            await self.stop()
            return False
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            logger.error(f"[push_forwarder] 推送接收服务启动失败：{e}")
            await self.stop()
            return False

        self.running = True
        self.last_error = ""
        bound = self.bound_addresses()
        # 打印真实 bind 到的地址：光看配置不能确定端口开没开，日志里有这行才算数
        logger.info(
            f"[push_forwarder] 推送接收服务已启动：http://{self.host}:{self.port}{path}"
            + (f"（实际监听 {bound}）" if bound else "")
        )
        return True

    def bound_addresses(self) -> str:
        """从 aiohttp 取真实 bind 上的地址，用来确认端口确实开了。"""
        try:
            server = getattr(self._site, "_server", None)
            sockets = getattr(server, "sockets", None) or []
            parts = []
            for sock in sockets:
                addr = sock.getsockname()
                if isinstance(addr, tuple) and len(addr) >= 2:
                    parts.append(f"{addr[0]}:{addr[1]}")
            return "、".join(parts)
        except Exception:  # noqa: BLE001
            return ""

    async def stop(self) -> None:
        """优雅关停。必须可重复调用，热重载依赖它释放端口。"""
        self.running = False
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:  # noqa: BLE001
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self._runner = None

    async def restart(self) -> bool:
        await self.stop()
        return await self.start()

    # ------------------------------------------------------------------ 鉴权

    @property
    def token(self) -> str:
        return str(self.config.get("receiver_token") or "")

    @staticmethod
    def _match(candidates: list[str], expected: str) -> bool:
        return any(
            c and secrets.compare_digest(str(c), expected) for c in candidates
        )

    @staticmethod
    def _meta_tokens(request: Any) -> list[str]:
        """请求头与查询串里可能带 Token 的位置。读 body 之前就能判定。"""
        candidates = [
            request.headers.get("X-Token", ""),
            request.headers.get("X-Push-Token", ""),
            request.query.get("token", ""),
            # 企微群机器人的地址形如 ...?key=xxx，把 key 当作 Token 的别名，
            # 推送方粘贴过来的 URL 才能原样可用
            request.query.get("key", ""),
        ]
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            candidates.append(auth[7:].strip())
        return candidates

    def _check_token(self, request: Any, body: Any) -> bool:
        expected = self.token
        if not expected:
            # 没配 Token 就一律拒绝。这里以前返回 True，等于"没设密码就不设防"，
            # 一旦有人清空配置，整个端口对公网敞开。
            return False
        candidates = self._meta_tokens(request)
        if isinstance(body, dict):
            candidates.append(str(body.get("token") or ""))
        return self._match(candidates, expected)

    @staticmethod
    def _is_wecom_request(request: Any) -> bool:
        """URL 里带 key= 基本就是企微通道过来的，读 body 之前就能判断。"""
        try:
            return "key" in request.query
        except Exception:  # noqa: BLE001
            return False

    def _deny(
        self, message: str, status: int, wecom: bool = False, errcode: int = 40001
    ) -> Any:
        """出错响应。

        企微通道普遍只看 errcode 判成败，所以那一侧要按它的字段名回。
        HTTP 状态码仍按语义给（企微自己一律回 200），这样只看状态码的
        推送方也能发现失败 —— 两种判定方式都能兜住。
        """
        if wecom:
            return web.json_response(
                {"errcode": errcode, "errmsg": message}, status=status
            )
        return web.json_response({"status": "error", "message": message}, status=status)

    def _log_reject(self, request: Any, reason: str) -> None:
        """拒绝日志做节流，免得被扫描器把日志刷爆。"""
        self._rejects += 1
        now = time.monotonic()
        if now - self._reject_logged_at < 60:
            return
        skipped = self._rejects - self._rejects_logged
        self._reject_logged_at = now
        self._rejects_logged = self._rejects
        extra = f"（自上次记录以来共 {skipped} 次）" if skipped > 1 else ""
        logger.warning(
            f"[push_forwarder] 拒绝来自 {getattr(request, 'remote', '?')} 的请求："
            f"{reason}{extra}"
        )

    def _guard(self, request: Any) -> Any:
        """所有路由共用的前置校验：IP 白名单 + 服务端是否配了 Token。

        返回 None 表示继续，否则返回要直接回给对方的响应。
        """
        wecom = self._is_wecom_request(request)
        if not self._check_ip(request):
            self._log_reject(request, "不在 IP 白名单")
            return self._deny("IP 不在白名单内", 403, wecom)
        if not self.token:
            self._log_reject(request, "服务端未配置 Token")
            return self._deny("服务端未配置 Token，已拒绝所有请求", 503, wecom)
        return None

    def _check_ip(self, request: Any) -> bool:
        whitelist = self.config.get("receiver_ip_whitelist") or []
        if not whitelist:
            return True
        remote = request.remote or ""
        return ip_allowed(remote, list(whitelist))

    # ------------------------------------------------------------------ 路由

    async def _read_body(self, request: Any) -> Any:
        """按 Content-Type 读取请求体，尽量宽容。"""
        if request.method == "GET":
            # 不用 dict()：同名参数出现多次时它只留最后一个，?tag=a&tag=b 会丢一半
            return query_to_dict(request.query)

        content_type = (request.content_type or "").lower()
        if "json" in content_type:
            try:
                return await request.json()
            except Exception as e:  # noqa: BLE001
                raise PayloadError(f"JSON 解析失败：{e}") from e
        if "form" in content_type:
            return dict(await request.post())

        text = (await request.text()).strip()
        if not text:
            return {}
        # 有些工具不设 Content-Type 却发的是 JSON，这里再试一次
        if text[0] in "{[":
            try:
                import json

                return json.loads(text)
            except Exception:  # noqa: BLE001
                pass
        return text

    async def _handle_push(self, request: Any) -> Any:
        denied = self._guard(request)
        if denied is not None:
            return denied

        # 先用请求头/查询串鉴权；过了这一关再谈 body 里写了什么
        authed = self._match(self._meta_tokens(request), self.token)
        wecom = self._is_wecom_request(request)

        try:
            body = await self._read_body(request)
        except PayloadError as e:
            if not authed:
                # 未授权的请求不该知道自己的 JSON 哪里写错了
                self._log_reject(request, "Token 无效")
                return self._deny("Token 无效", 401, wecom)
            return self._deny(str(e), 400, wecom, 40008)

        # body 读出来才知道是不是企微格式（没带 key 但直接 POST 企微消息体的情况）
        wecom = wecom or is_wecom_payload(body)

        if isinstance(body, dict) and request.method != "GET":
            # 企微那类通道的消息体是固定格式改不动，把路由字段写在 URL 上
            # （?bot=xxx&tags=alert）也能生效。GET 的 body 本来就是查询串，不用再合。
            body = merge_query_routing(body, request.query)

        if not authed:
            in_body = str(body.get("token") or "") if isinstance(body, dict) else ""
            if not self._match([in_body], self.token):
                self._log_reject(request, "Token 无效")
                return self._deny("Token 无效", 401, wecom)

        default_tags = []
        tag = request.match_info.get("tag")
        if tag:
            default_tags = [tag]

        try:
            message = parse_payload(body, default_tags=default_tags)
        except PayloadError as e:
            return self._deny(str(e), 400, wecom, 40008)

        try:
            result = await self.handler(message)
        except Exception as e:  # noqa: BLE001
            logger.exception("[push_forwarder] 分发推送时出错")
            # -1 是企微的「系统繁忙」，推送方看到它通常会重试
            return self._deny(f"分发失败：{e}", 500, wecom, -1)

        if wecom:
            # 缺 errcode 会被企微通道当成失败。转发结果放 data 里，
            # 是额外字段，不影响只认 errcode 的推送方。
            return web.json_response(
                {"errcode": 0, "errmsg": "ok", "data": result}, status=200
            )

        summary = result.get("summary", {})
        # dry_run 不会有 sent，但它是成功的，别回 202 让推送方以为没生效
        ok = summary.get("sent") or summary.get("queued") or result.get("dry_run")
        status = 200 if ok else 202
        return web.json_response({"status": "ok", "data": result}, status=status)

    async def _handle_health(self, request: Any) -> Any:
        """健康检查。同样要带 Token —— 免鉴权的探活接口等于对外自报家门。"""
        denied = self._guard(request)
        if denied is not None:
            return denied
        if not self._check_token(request, {}):
            self._log_reject(request, "Token 无效")
            return self._deny("Token 无效", 401)
        return web.json_response({"status": "ok", "data": {"running": self.running}})

    async def _handle_unknown(self, request: Any) -> Any:
        """未知路径一律 404，且不说明这里跑着什么服务。"""
        self._log_reject(request, f"访问了未知路径 {request.path}")
        return self._deny("Not Found", 404)
