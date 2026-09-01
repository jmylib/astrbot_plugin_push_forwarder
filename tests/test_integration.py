"""端到端集成测试。

用 tests/stub_astrbot.py 提供的桩件把插件真实加载起来，覆盖：
多机器人分发、标签路由、转发时段（排队/丢弃/紧急）、平台能力降级、重试、
面板接口与安全校验、指令。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

import stub_astrbot as stub  # noqa: E402

stub.install(tempfile.mkdtemp(prefix="pf_boot_"))

import astrbot.core.utils.astrbot_path as astrbot_path  # noqa: E402

from astrbot_plugin_push_forwarder import main as pf  # noqa: E402
from astrbot_plugin_push_forwarder.core.models import FRIEND, GROUP  # noqa: E402
from astrbot_plugin_push_forwarder.core.payload import PushMessage  # noqa: E402
from astrbot_plugin_push_forwarder.core import receiver as recv  # noqa: E402

QQ = "qq_bot_1"
QQ2 = "qq_bot_2"
WX = "wx_bot_1"
ONEBOT = "onebot_1"


def base_config(**overrides):
    cfg = stub.AstrBotConfig(
        {
            "receiver_enabled": False,
            "receiver_token": "",
            "dispatch_interval_ms": 0,
            "dispatch_retry_times": 0,
            "dispatch_concurrency": 4,
            "format_template": "{title}\n{text}",
            "format_max_length": 2000,
            "format_split_long_message": True,
            "session_discovery_enabled": True,
        }
    )
    cfg.update(overrides)
    return cfg


def default_platforms():
    return [
        stub.FakePlatform(QQ, "qq_official"),
        stub.FakePlatform(QQ2, "qq_official"),
        stub.FakePlatform(WX, "weixin_oc"),
    ]


def make_plugin(platforms=None, **cfg_overrides):
    """每个测试用独立的数据目录，互不干扰。"""
    data = tempfile.mkdtemp(prefix="pf_test_")
    astrbot_path.get_astrbot_data_path = lambda: data
    ctx = stub.FakeContext(platforms if platforms is not None else default_platforms())
    cfg = base_config(**cfg_overrides)
    plugin = pf.PushForwarder(ctx, cfg)
    return plugin, ctx, cfg


def add_session(plugin, platform_id, session_id, message_type=GROUP, display_name=""):
    """登记一个"已发现会话"。走公开接口，这样脏标记和落盘行为与真实路径一致。"""
    umo = f"{platform_id}:{message_type}:{session_id}"
    plugin.sessions.upsert_from_api(
        [
            {
                "umo": umo,
                "platform_id": platform_id,
                "message_type": message_type,
                "session_id": session_id,
                "display_name": display_name or session_id,
            }
        ]
    )
    return umo


def add_target(plugin, platform_id, session_id, message_type=GROUP, **kwargs):
    """写入一个转发目标，并让它成为已发现会话（保存接口会校验这一点）。"""
    umo = add_session(
        plugin,
        platform_id,
        session_id,
        message_type,
        kwargs.pop("display_name", ""),
    )
    target, _ = plugin.store.add_target(
        platform_id=platform_id,
        umo=umo,
        message_type=message_type,
        display_name=session_id,
        tags=kwargs.pop("tags", None),
    )
    for key, value in kwargs.items():
        setattr(target, key, value)
    plugin.store.save()
    return target


def closed_schedule(action="queue"):
    """一个当前一定不开放的时段：只在周一 00:00-00:01。"""
    return {
        "enabled": True,
        "timezone": "UTC+0",
        "days": [1],
        "ranges": [["00:00", "00:01"]],
        "outside_action": action,
    }


def open_schedule():
    return {
        "enabled": True,
        "timezone": "UTC+0",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "ranges": [["00:00", "23:59"]],
        "outside_action": "queue",
    }


def texts_of(ctx):
    return [chain.plain_text() for _, chain in ctx.sent]


# --------------------------------------------------------------------- 加载


def test_plugin_loads_and_registers_apis():
    plugin, ctx, _ = make_plugin()
    routes = [r[0] for r in ctx.web_apis]
    assert f"/{pf.PLUGIN_NAME}/bots" in routes
    assert f"/{pf.PLUGIN_NAME}/targets/save" in routes

    # 短名前缀也要注册一份，Dashboard 用哪个名字转发在不同版本里不一致
    assert "push_forwarder" in pf.ROUTE_PREFIXES
    assert "/push_forwarder/bots" in routes

    prefixes = tuple(f"/{p}/" for p in pf.ROUTE_PREFIXES)
    assert all(r.startswith(prefixes) for r in routes), "路由必须带插件名前缀"
    assert len(routes) == len(set(routes)), "同一路由不应重复注册"


def test_token_auto_generated_and_saved():
    _, _, cfg = make_plugin()
    assert len(str(cfg["receiver_token"])) >= 16
    assert cfg.save_count == 1, "自动生成的 Token 必须写回配置"


def test_existing_token_not_overwritten():
    _, _, cfg = make_plugin(receiver_token="my-fixed-token")
    assert cfg["receiver_token"] == "my-fixed-token"
    assert cfg.save_count == 0


# ----------------------------------------------------------------- 会话发现


def test_session_discovery_records_group_and_friend():
    plugin, _, _ = make_plugin()
    event = stub.FakeEvent(
        f"{QQ}:{GROUP}:abc_group_openid_123",
        QQ,
        "qq_official",
        group_id="group_openid_123",
        sender_name="张三",
    )
    asyncio.run(_drain(plugin.record_session(event)))

    entry = plugin.sessions.get(f"{QQ}:{GROUP}:abc_group_openid_123")
    assert entry is not None
    assert entry["platform_id"] == QQ
    assert entry["message_type"] == GROUP
    assert entry["last_sender_name"] == "张三"
    assert entry["msg_count"] == 1
    # 群名拿不到时退化成群号尾段，仍然能认出来
    assert "…" in entry["display_name"] or entry["display_name"]


def test_session_discovery_can_be_disabled():
    plugin, _, _ = make_plugin(session_discovery_enabled=False)
    event = stub.FakeEvent(f"{QQ}:{GROUP}:g1", QQ, "qq_official", group_id="g1")
    asyncio.run(_drain(plugin.record_session(event)))
    assert plugin.sessions.get(f"{QQ}:{GROUP}:g1") is None


def test_session_discovery_survives_broken_event():
    """事件对象缺方法时不能抛异常，否则会拖垮整条消息链路。"""
    plugin, _, _ = make_plugin()

    class Broken:
        unified_msg_origin = f"{QQ}:{GROUP}:g9"

        def get_platform_id(self):
            raise RuntimeError("boom")

    asyncio.run(_drain(plugin.record_session(Broken())))
    assert plugin.sessions.get(f"{QQ}:{GROUP}:g9") is not None


# ------------------------------------------------------------------- 分发


def test_dispatch_to_multiple_bots():
    """一条推送同时发到两个机器人下的三个会话。"""
    plugin, ctx, _ = make_plugin()
    add_target(plugin, QQ, "g1")
    add_target(plugin, QQ, "g2")
    add_target(plugin, QQ2, "g3")

    result = asyncio.run(plugin.dispatcher.dispatch(PushMessage(title="告警", text="磁盘满了")))

    assert result["summary"]["sent"] == 3
    assert len(ctx.sent) == 3
    assert {umo for umo, _ in ctx.sent} == {
        f"{QQ}:{GROUP}:g1",
        f"{QQ}:{GROUP}:g2",
        f"{QQ2}:{GROUP}:g3",
    }
    assert texts_of(ctx)[0] == "告警\n磁盘满了"


def test_tag_routing():
    plugin, ctx, _ = make_plugin()
    add_target(plugin, QQ, "alert_group", tags=["alert"])
    add_target(plugin, QQ, "daily_group", tags=["daily"])
    add_target(plugin, QQ, "no_tag_group")

    asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="x", tags=["alert"])))
    assert [umo for umo, _ in ctx.sent] == [f"{QQ}:{GROUP}:alert_group"]

    ctx.sent.clear()
    # 不带标签的推送发给所有目标
    asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="y")))
    assert len(ctx.sent) == 3


def test_explicit_target_ids_win_over_tags():
    plugin, ctx, _ = make_plugin()
    t1 = add_target(plugin, QQ, "g1", tags=["alert"])
    add_target(plugin, QQ, "g2", tags=["alert"])

    asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="x", target_ids=[t1.id])))
    assert [umo for umo, _ in ctx.sent] == [f"{QQ}:{GROUP}:g1"]


def test_disabled_target_and_bot_are_skipped():
    plugin, ctx, _ = make_plugin()
    add_target(plugin, QQ, "g1", enabled=False)
    add_target(plugin, QQ2, "g2")
    cfg = plugin.store.get_bot(QQ2)
    cfg.enabled = False
    plugin.store.set_bot(QQ2, cfg)

    result = asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="x")))
    assert len(ctx.sent) == 0
    assert result["summary"]["skipped"] == 2
    details = " ".join(r["detail"] for r in result["results"])
    assert "目标已停用" in details and "所属机器人已停用" in details


# ------------------------------------------------------------------- 时段


def test_outside_schedule_queues_and_flush_sends():
    """不在时段内 -> 排队；进入时段后由后台任务补发。"""
    plugin, ctx, _ = make_plugin(dispatch_merge_queued=False)
    target = add_target(plugin, QQ, "g1")
    cfg = plugin.store.get_bot(QQ)
    cfg.schedule = closed_schedule("queue")
    plugin.store.set_bot(QQ, cfg)

    result = asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="夜间告警")))
    assert result["summary"]["queued"] == 1
    assert len(ctx.sent) == 0
    assert plugin.dispatcher.queue.count(target.id) == 1
    assert "补发" in result["results"][0]["detail"]

    # 队列必须落盘，重启后才不会丢
    assert plugin.dispatcher.queue.path.exists()

    cfg.schedule = open_schedule()
    plugin.store.set_bot(QQ, cfg)
    sent = asyncio.run(plugin.dispatcher.flush_queue())

    assert sent == 1
    assert texts_of(ctx) == ["夜间告警"]
    assert plugin.dispatcher.queue.count(target.id) == 0


def test_queued_messages_merged_by_default():
    plugin, ctx, _ = make_plugin(dispatch_merge_queued=True)
    add_target(plugin, QQ, "g1")
    cfg = plugin.store.get_bot(QQ)
    cfg.schedule = closed_schedule("queue")
    plugin.store.set_bot(QQ, cfg)

    for i in range(3):
        asyncio.run(plugin.dispatcher.dispatch(PushMessage(text=f"消息{i}")))

    cfg.schedule = open_schedule()
    plugin.store.set_bot(QQ, cfg)
    asyncio.run(plugin.dispatcher.flush_queue())

    assert len(ctx.sent) == 1, "合并后应只发一条，避免刷屏"
    body = texts_of(ctx)[0]
    assert "补发 3 条消息" in body
    for i in range(3):
        assert f"消息{i}" in body


def test_outside_schedule_drop():
    plugin, ctx, _ = make_plugin()
    target = add_target(plugin, QQ, "g1")
    cfg = plugin.store.get_bot(QQ)
    cfg.schedule = closed_schedule("drop")
    plugin.store.set_bot(QQ, cfg)

    result = asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="x")))
    assert result["summary"]["skipped"] == 1
    assert plugin.dispatcher.queue.count(target.id) == 0
    assert len(ctx.sent) == 0


def test_urgent_bypasses_schedule():
    plugin, ctx, _ = make_plugin()
    add_target(plugin, QQ, "g1")
    cfg = plugin.store.get_bot(QQ)
    cfg.schedule = closed_schedule("drop")
    plugin.store.set_bot(QQ, cfg)

    asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="紧急", urgent=True)))
    assert len(ctx.sent) == 1


def test_target_own_schedule_overrides_bot():
    """目标取消继承后，用自己的时段，不受机器人时段影响。"""
    plugin, ctx, _ = make_plugin()
    add_target(plugin, QQ, "g1", schedule_inherit=False, schedule=open_schedule())
    add_target(plugin, QQ, "g2")  # 继承机器人时段
    cfg = plugin.store.get_bot(QQ)
    cfg.schedule = closed_schedule("drop")
    plugin.store.set_bot(QQ, cfg)

    asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="x")))
    assert [umo for umo, _ in ctx.sent] == [f"{QQ}:{GROUP}:g1"]


def test_per_bot_schedule_is_independent():
    """两个机器人各自的时段互不影响。"""
    plugin, ctx, _ = make_plugin()
    add_target(plugin, QQ, "g1")
    add_target(plugin, QQ2, "g2")

    c1 = plugin.store.get_bot(QQ)
    c1.schedule = closed_schedule("drop")
    plugin.store.set_bot(QQ, c1)
    c2 = plugin.store.get_bot(QQ2)
    c2.schedule = open_schedule()
    plugin.store.set_bot(QQ2, c2)

    asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="x")))
    assert [umo for umo, _ in ctx.sent] == [f"{QQ2}:{GROUP}:g2"]


# --------------------------------------------------------------- 平台能力


def test_weixin_group_target_is_rejected():
    """微信 ClawBot 适配器没有群消息逻辑，群目标必须被跳过而不是发失败。"""
    plugin, ctx, _ = make_plugin()
    add_target(plugin, WX, "some_group", message_type=GROUP)

    result = asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="x")))
    assert len(ctx.sent) == 0
    assert result["summary"]["skipped"] == 1
    assert "不支持群消息" in result["results"][0]["detail"]


def test_weixin_private_target_works_without_at():
    """微信私聊可以发，但 @ 组件必须被静默过滤掉。"""
    plugin, ctx, _ = make_plugin()
    add_target(plugin, WX, "user_1", message_type=FRIEND, at_mode="all")

    asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="hello")))
    assert len(ctx.sent) == 1
    chain = ctx.sent[0][1]
    assert all(not isinstance(c, (stub.At, stub.AtAll)) for c in chain.chain)
    assert chain.plain_text() == "hello"


def test_qq_group_at_all_is_included():
    plugin, ctx, _ = make_plugin()
    add_target(plugin, QQ, "g1", at_mode="all")

    asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="重要")))
    chain = ctx.sent[0][1]
    assert isinstance(chain.chain[0], stub.AtAll)
    assert chain.plain_text() == "重要"


def test_at_users_from_payload_overrides_target():
    plugin, ctx, _ = make_plugin()
    add_target(plugin, QQ, "g1", at_mode="none")

    asyncio.run(
        plugin.dispatcher.dispatch(
            PushMessage(text="x", at_mode="users", at_users=["u1", "u2"])
        )
    )
    chain = ctx.sent[0][1]
    ats = [c for c in chain.chain if isinstance(c, stub.At)]
    assert [a.qq for a in ats] == ["u1", "u2"]


def test_at_ignored_for_private_target():
    """私聊里 @ 没有意义，不应该带上。"""
    plugin, ctx, _ = make_plugin()
    add_target(plugin, QQ, "u1", message_type=FRIEND, at_mode="all")

    asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="x")))
    chain = ctx.sent[0][1]
    assert len(chain.chain) == 1 and isinstance(chain.chain[0], stub.Plain)


# --------------------------------------------------------------- 失败与重试


def test_retry_then_success():
    plugin, ctx, _ = make_plugin(dispatch_retry_times=2)
    add_target(plugin, QQ, "g1")
    ctx.fail_times = 2

    async def scenario():
        original = asyncio.sleep

        async def fast(_seconds, *a, **k):  # 跳过退避等待，保持测试秒级
            return await original(0)

        asyncio.sleep = fast
        try:
            return await plugin.dispatcher.dispatch(PushMessage(text="x"))
        finally:
            asyncio.sleep = original

    result = asyncio.run(scenario())
    assert result["summary"]["sent"] == 1
    assert len(ctx.sent) == 1


def test_failure_recorded_after_retries_exhausted():
    plugin, ctx, _ = make_plugin(dispatch_retry_times=0)
    add_target(plugin, QQ, "g1")
    ctx.fail_times = 1

    result = asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="x")))
    assert result["summary"]["failed"] == 1
    assert "模拟的发送失败" in result["results"][0]["detail"]
    assert plugin.history.records[-1]["summary"]["failed"] == 1


def test_missing_platform_instance_is_failure_not_crash():
    """send_message 返回 False 表示找不到机器人实例，要当失败处理。"""
    plugin, ctx, _ = make_plugin()
    add_target(plugin, QQ, "g1")
    ctx.return_false_for.add(f"{QQ}:{GROUP}:g1")

    result = asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="x")))
    assert result["summary"]["failed"] == 1
    assert "未找到对应的机器人实例" in result["results"][0]["detail"]


def test_long_message_is_split():
    plugin, ctx, _ = make_plugin(format_max_length=20, format_split_long_message=True)
    add_target(plugin, QQ, "g1")

    asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="甲" * 55)))
    assert len(ctx.sent) == 3
    assert "".join(texts_of(ctx)) == "甲" * 55


# ------------------------------------------------------------- 面板接口


def test_api_bots_exposes_capabilities():
    plugin, _, _ = make_plugin()
    data = asyncio.run(plugin.api_bots())["data"]
    by_id = {b["platform_id"]: b for b in data["bots"]}

    assert by_id[QQ]["caps"]["group"] is True
    assert by_id[QQ]["caps"]["listable"] is False
    assert by_id[WX]["caps"]["group"] is False, "微信 ClawBot 不支持群"
    assert by_id[WX]["caps"]["at"] is False
    assert "不支持群消息" in by_id[WX]["caps"]["note"] or by_id[WX]["caps"]["note"]


def test_api_sessions_marks_selected():
    plugin, _, _ = make_plugin()
    add_target(plugin, QQ, "g1")
    add_session(plugin, QQ, "g2", display_name="未选中的群")
    stub.request.bind(query={"platform_id": QQ})
    data = asyncio.run(plugin.api_sessions())["data"]
    by_umo = {s["umo"]: s for s in data["sessions"]}

    assert by_umo[f"{QQ}:{GROUP}:g1"]["selected"] is True
    assert by_umo[f"{QQ}:{GROUP}:g2"]["selected"] is False


def test_api_sessions_filter_by_type_and_keyword():
    plugin, _, _ = make_plugin()
    add_target(plugin, QQ, "g1", display_name="运维群")
    add_target(plugin, QQ, "u1", message_type=FRIEND)

    stub.request.bind(query={"platform_id": QQ, "type": FRIEND})
    data = asyncio.run(plugin.api_sessions())["data"]
    assert [s["session_id"] for s in data["sessions"]] == ["u1"]

    stub.request.bind(query={"platform_id": QQ, "q": "g1"})
    data = asyncio.run(plugin.api_sessions())["data"]
    assert [s["session_id"] for s in data["sessions"]] == ["g1"]


def test_api_targets_save_rejects_unknown_session():
    """面板保存接口只接受已发现的会话，防止被构造请求写入任意会话。"""
    plugin, _, _ = make_plugin()
    add_target(plugin, QQ, "g1")

    stub.request.bind(
        json_body={
            "targets": [
                {"id": "t_1", "platform_id": QQ, "umo": f"{QQ}:{GROUP}:g1", "message_type": GROUP},
                {"id": "t_2", "platform_id": QQ, "umo": f"{QQ}:{GROUP}:未知的群", "message_type": GROUP},
            ]
        },
        method="POST",
    )
    data = asyncio.run(plugin.api_targets_save())["data"]

    assert data["saved"] == 1
    assert data["rejected"] == 1
    assert [t.umo for t in plugin.store.list_targets()] == [f"{QQ}:{GROUP}:g1"]


def test_api_schedule_save_sanitizes_input():
    plugin, _, _ = make_plugin()
    stub.request.bind(
        json_body={
            "platform_id": QQ,
            "enabled": True,
            "remark": "主号",
            "schedule": {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "days": [1, 2, 99, "x"],
                "ranges": [["09:00", "18:00"], "坏数据"],
                "outside_action": "无效值",
            },
        },
        method="POST",
    )
    data = asyncio.run(plugin.api_schedule_save())["data"]

    schedule = data["schedule"]
    assert schedule["days"] == [1, 2], "非法星期要被过滤"
    assert schedule["ranges"] == [["09:00", "18:00"]], "非法区间要被过滤"
    assert schedule["outside_action"] == "queue", "非法策略回落到默认值"
    assert plugin.store.get_bot(QQ).remark == "主号"


def test_api_test_sends_ignoring_schedule():
    plugin, ctx, _ = make_plugin()
    target = add_target(plugin, QQ, "g1")
    cfg = plugin.store.get_bot(QQ)
    cfg.schedule = closed_schedule("drop")
    plugin.store.set_bot(QQ, cfg)

    stub.request.bind(json_body={"target_ids": [target.id], "text": "面板测试"}, method="POST")
    data = asyncio.run(plugin.api_test())["data"]

    assert data["summary"]["sent"] == 1
    assert "面板测试" in texts_of(ctx)[0]


def test_api_webhook_reports_state():
    plugin, _, cfg = make_plugin(receiver_port=9977, receiver_path="push")
    data = asyncio.run(plugin.api_webhook())["data"]
    assert data["port"] == 9977
    assert data["path"] == "/push", "路径要被规范成 /push"
    assert data["token"] == cfg["receiver_token"]
    assert data["running"] is False


def test_api_history_records_dispatch():
    plugin, _, _ = make_plugin()
    add_target(plugin, QQ, "g1")
    asyncio.run(plugin.dispatcher.dispatch(PushMessage(title="标题", text="正文")))

    stub.request.bind(query={"limit": "10"})
    records = asyncio.run(plugin.api_history())["data"]["records"]
    assert len(records) == 1
    assert records[0]["summary"]["sent"] == 1
    assert "标题" in records[0]["preview"]


def test_api_sessions_refresh_rejects_unlistable_platform():
    plugin, _, _ = make_plugin()
    stub.request.bind(json_body={"platform_id": QQ}, method="POST")
    resp = asyncio.run(plugin.api_sessions_refresh())
    assert resp["status"] == "error"
    assert "不支持查询会话列表" in resp["message"]


def test_api_sessions_refresh_pulls_onebot_lists():
    """OneBot 平台可以直接拉到真实群名与好友昵称。"""

    class FakeApi:
        async def call_action(self, action, **kwargs):
            if action == "get_group_list":
                return [{"group_id": 111, "group_name": "技术交流群"}]
            if action == "get_friend_list":
                return [{"user_id": 222, "nickname": "李四", "remark": "老李"}]
            return []

    class FakeClient:
        api = FakeApi()

    plugin, _, _ = make_plugin(
        platforms=[stub.FakePlatform(ONEBOT, "aiocqhttp", client=FakeClient())]
    )
    stub.request.bind(json_body={"platform_id": ONEBOT}, method="POST")
    resp = asyncio.run(plugin.api_sessions_refresh())

    assert resp["status"] == "ok"
    assert resp["data"]["count"] == 2
    group = plugin.sessions.get(f"{ONEBOT}:{GROUP}:111")
    friend = plugin.sessions.get(f"{ONEBOT}:{FRIEND}:222")
    assert group["display_name"] == "技术交流群"
    assert friend["display_name"] == "老李"


# ----------------------------------------------------------------- 指令


def test_fwd_here_adds_current_session():
    plugin, _, _ = make_plugin()
    event = stub.FakeEvent(f"{QQ}:{GROUP}:g_open_1", QQ, "qq_official", group_id="g_open_1")

    asyncio.run(_drain(plugin.fwd_here(event, "alert")))

    target = plugin.store.find_by_umo(f"{QQ}:{GROUP}:g_open_1")
    assert target is not None
    assert target.tags == ["alert"]
    assert "已添加为转发目标" in event.replies[0]


def test_fwd_here_rejects_group_on_weixin():
    plugin, _, _ = make_plugin()
    event = stub.FakeEvent(f"{WX}:{GROUP}:g1", WX, "weixin_oc", group_id="g1")

    asyncio.run(_drain(plugin.fwd_here(event, "")))

    assert plugin.store.find_by_umo(f"{WX}:{GROUP}:g1") is None
    assert "不支持群消息转发" in event.replies[0]


def test_fwd_rm_and_list():
    plugin, _, _ = make_plugin()
    add_target(plugin, QQ, "g1")
    event = stub.FakeEvent(f"{QQ}:{GROUP}:g1", QQ, "qq_official", group_id="g1")

    asyncio.run(_drain(plugin.fwd_list(event)))
    assert "共 1 个转发目标" in event.replies[0]

    asyncio.run(_drain(plugin.fwd_rm(event)))
    assert "已移除" in event.replies[1]
    assert plugin.store.list_targets() == []


def test_fwd_list_empty_gives_guidance():
    plugin, _, _ = make_plugin()
    event = stub.FakeEvent(f"{QQ}:{GROUP}:g1", QQ, "qq_official", group_id="g1")
    asyncio.run(_drain(plugin.fwd_list(event)))
    assert "/fwd here" in event.replies[0]


def test_fwd_url_gives_full_url_in_private():
    plugin, _, cfg = make_plugin()
    token = str(cfg.get("receiver_token") or "")
    assert token, "插件加载时应自动生成 Token"
    event = stub.FakeEvent(f"{QQ}:{FRIEND}:u1", QQ, "qq_official")

    asyncio.run(_drain(plugin.fwd_url(event)))

    reply = event.replies[0]
    assert f"?token={token}" in reply
    assert f"X-Token: {token}" in reply
    assert str(plugin.receiver.port) in reply


def test_fwd_url_hides_token_in_group():
    plugin, _, cfg = make_plugin()
    token = str(cfg.get("receiver_token") or "")
    event = stub.FakeEvent(f"{QQ}:{GROUP}:g1", QQ, "qq_official", group_id="g1")

    asyncio.run(_drain(plugin.fwd_url(event)))

    reply = event.replies[0]
    assert token not in reply
    assert "私聊" in reply


# --------------------------------------------------------- 接收服务生命周期


def test_tasks_not_spawned_without_running_loop():
    """__init__ 里没有事件循环时不能报错，也不能留下没跑起来的协程。"""
    plugin, _, _ = make_plugin()
    assert plugin._tasks == [], "同步构造时拿不到事件循环，这里应该是空的"


def test_initialize_spawns_background_tasks():
    """端口真正开起来的地方是 initialize()，缺了它插件会加载成功但永不监听。"""
    plugin, _, _ = make_plugin()

    async def scenario():
        await plugin.initialize()
        count = len(plugin._tasks)
        await plugin.initialize()  # 可重复调用，不应重复起任务
        again = len(plugin._tasks)
        await asyncio.sleep(0)
        await plugin.terminate()
        return count, again

    count, again = asyncio.run(scenario())
    assert count == 2, "应当起了 startup 与 maintenance 两个任务"
    assert again == 2, "initialize 必须幂等"


def test_receiver_last_error_defaults_to_not_started():
    """未启动时面板要显示"尚未启动"，而不是"原因未知"。"""
    plugin, _, _ = make_plugin()
    assert plugin.receiver.running is False
    assert plugin.receiver.last_error == "尚未启动"

    data = asyncio.run(plugin.api_webhook())["data"]
    assert data["last_error"] == "尚未启动"
    assert data["bound"] == "", "没监听时不该报出 bind 地址"


def test_startup_marks_disabled_reason():
    """配置里关掉接收服务时，理由要写进 last_error 供面板展示。"""
    plugin, _, _ = make_plugin(receiver_enabled=False)
    asyncio.run(plugin._startup())
    assert plugin.receiver.last_error == "已在插件配置中关闭"


def test_fwd_start_reports_failure_reason():
    """/fwd start 把失败原因直接回给管理员，省得去翻日志。"""
    plugin, _, _ = make_plugin(receiver_enabled=True)
    event = stub.FakeEvent(f"{QQ}:{FRIEND}:u1", QQ, "qq_official")

    async def fake_restart():
        plugin.receiver.last_error = "端口 9966 启动失败：address already in use"
        return False

    plugin.receiver.restart = fake_restart
    asyncio.run(_drain(plugin.fwd_start(event)))

    assert "启动失败" in event.replies[0]
    assert "address already in use" in event.replies[0]


def test_fwd_start_refuses_when_disabled():
    plugin, _, _ = make_plugin(receiver_enabled=False)
    event = stub.FakeEvent(f"{QQ}:{FRIEND}:u1", QQ, "qq_official")
    asyncio.run(_drain(plugin.fwd_start(event)))
    assert "插件配置里是关闭的" in event.replies[0]


def test_fwd_start_reports_bound_address():
    plugin, _, _ = make_plugin(receiver_enabled=True)
    event = stub.FakeEvent(f"{QQ}:{FRIEND}:u1", QQ, "qq_official")

    async def fake_restart():
        plugin.receiver.running = True
        plugin.receiver.last_error = ""
        return True

    plugin.receiver.restart = fake_restart
    plugin.receiver.bound_addresses = lambda: "0.0.0.0:9966"
    asyncio.run(_drain(plugin.fwd_start(event)))

    assert "0.0.0.0:9966" in event.replies[0]


def test_fwd_info_shows_retry_hint_when_down():
    plugin, _, _ = make_plugin()
    event = stub.FakeEvent(f"{QQ}:{FRIEND}:u1", QQ, "qq_official")
    asyncio.run(_drain(plugin.fwd_info(event)))
    assert "/fwd start" in event.replies[0]
    assert "尚未启动" in event.replies[0]


# ------------------------------------------------------------- 接收端鉴权


class FakeResponse:
    """够用就好：只记录状态码和 body，便于断言。"""

    def __init__(self, data, status=200):
        self.data = data
        self.status = status


class FakeWeb:
    @staticmethod
    def json_response(data, status=200):
        return FakeResponse(data, status)


class FakeRequest:
    def __init__(
        self, method="POST", path="/push", headers=None, query=None,
        json_body=None, remote="1.2.3.4", match_info=None, raise_on_read=False,
    ):
        self.method = method
        self.path = path
        self.headers = headers or {}
        self.query = query or {}
        self.remote = remote
        self.match_info = match_info or {}
        self.content_type = "application/json"
        self._json = json_body if json_body is not None else {}
        self._raise = raise_on_read

    async def json(self):
        if self._raise:
            raise recv.PayloadError("JSON 解析失败：故意坏掉的输入")
        return self._json


def with_fake_web(plugin):
    """把 receiver 模块里的 aiohttp.web 换成桩件；本机不一定装了 aiohttp。

    这是进程级替换，之后整轮测试都用桩件。处理函数只用到 json_response，
    其余用例不碰 receiver.web，所以不还原也不会互相干扰。
    """
    recv.web = FakeWeb
    return plugin.receiver


def test_receiver_token_check_all_forms():
    plugin, _, cfg = make_plugin(receiver_token="secret-token")
    receiver = plugin.receiver

    class Req:
        def __init__(self, headers=None, query=None):
            self.headers = headers or {}
            self.query = query or {}

    assert receiver._check_token(Req(headers={"X-Token": "secret-token"}), {})
    assert receiver._check_token(Req(headers={"Authorization": "Bearer secret-token"}), {})
    assert receiver._check_token(Req(query={"token": "secret-token"}), {})
    assert receiver._check_token(Req(), {"token": "secret-token"})
    assert not receiver._check_token(Req(headers={"X-Token": "wrong"}), {})
    assert not receiver._check_token(Req(), {})


def test_empty_token_rejects_instead_of_opening_up():
    """服务端没配 Token 时必须拒绝所有请求，而不是当成"不启用鉴权"。"""
    plugin, _, cfg = make_plugin(receiver_token="x")
    cfg["receiver_token"] = ""
    receiver = with_fake_web(plugin)

    class Req:
        headers = {"X-Token": "随便什么"}
        query = {}

    assert receiver._check_token(Req(), {}) is False
    denied = receiver._guard(FakeRequest())
    assert denied is not None and denied.status == 503


def test_health_requires_token():
    plugin, _, cfg = make_plugin(receiver_token="secret-token")
    receiver = with_fake_web(plugin)

    bad = asyncio.run(receiver._handle_health(FakeRequest(method="GET", path="/health")))
    assert bad.status == 401, "免鉴权的探活接口等于对外自报家门"

    ok = asyncio.run(
        receiver._handle_health(
            FakeRequest(method="GET", path="/health", headers={"X-Token": "secret-token"})
        )
    )
    assert ok.status == 200
    assert ok.data["data"]["running"] is False


def test_unknown_path_returns_404_without_details():
    plugin, _, _ = make_plugin(receiver_token="secret-token")
    receiver = with_fake_web(plugin)

    resp = asyncio.run(receiver._handle_unknown(FakeRequest(method="GET", path="/.env")))
    assert resp.status == 404
    assert "push_forwarder" not in str(resp.data), "404 不该暴露这里跑着什么服务"


def test_push_hides_parse_error_from_unauthorized():
    """未授权方连"你的 JSON 写错了"都不该知道。"""
    plugin, _, _ = make_plugin(receiver_token="secret-token")
    receiver = with_fake_web(plugin)

    resp = asyncio.run(receiver._handle_push(FakeRequest(raise_on_read=True)))
    assert resp.status == 401
    assert "JSON" not in resp.data["message"]

    authed = asyncio.run(
        receiver._handle_push(
            FakeRequest(raise_on_read=True, headers={"X-Token": "secret-token"})
        )
    )
    assert authed.status == 400, "带对 Token 的才配看到解析错误"


def test_push_accepts_token_in_body():
    plugin, _, _ = make_plugin(receiver_token="secret-token")
    receiver = with_fake_web(plugin)

    resp = asyncio.run(
        receiver._handle_push(
            FakeRequest(json_body={"token": "secret-token", "text": "hello"})
        )
    )
    assert resp.status in (200, 202), "body 里带 Token 仍然是支持的写法"

    bad = asyncio.run(
        receiver._handle_push(FakeRequest(json_body={"token": "wrong", "text": "hello"}))
    )
    assert bad.status == 401


def test_ip_whitelist_blocks_before_token():
    plugin, _, cfg = make_plugin(receiver_token="secret-token")
    cfg["receiver_ip_whitelist"] = ["10.0.0.0/8"]
    receiver = with_fake_web(plugin)

    resp = asyncio.run(
        receiver._handle_push(
            FakeRequest(remote="8.8.8.8", headers={"X-Token": "secret-token"})
        )
    )
    assert resp.status == 403


def test_reject_logging_is_throttled():
    """被扫描时不能把日志刷爆：计数照记，日志一分钟只写一条。"""
    plugin, _, _ = make_plugin(receiver_token="secret-token")
    receiver = with_fake_web(plugin)

    for _ in range(50):
        asyncio.run(receiver._handle_push(FakeRequest()))

    assert receiver._rejects == 50
    assert receiver._rejects_logged == 1, "只应写过一条日志"


def test_receiver_ip_whitelist():
    plugin, _, cfg = make_plugin()
    receiver = plugin.receiver

    class Req:
        def __init__(self, remote):
            self.remote = remote

    cfg["receiver_ip_whitelist"] = []
    assert receiver._check_ip(Req("8.8.8.8")) is True  # 空白名单不限制

    cfg["receiver_ip_whitelist"] = ["192.168.1.0/24", "10.0.0.5"]
    assert receiver._check_ip(Req("192.168.1.77")) is True
    assert receiver._check_ip(Req("10.0.0.5")) is True
    assert receiver._check_ip(Req("8.8.8.8")) is False


# ----------------------------------------------------------------- 持久化


def test_config_survives_reload():
    """重新构造插件后，目标、时段与队列都要还在。"""
    data = tempfile.mkdtemp(prefix="pf_reload_")
    astrbot_path.get_astrbot_data_path = lambda: data

    ctx = stub.FakeContext(default_platforms())
    plugin = pf.PushForwarder(ctx, base_config())
    add_target(plugin, QQ, "g1", tags=["alert"])
    cfg = plugin.store.get_bot(QQ)
    cfg.schedule = closed_schedule("queue")
    plugin.store.set_bot(QQ, cfg)
    asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="排队的消息")))
    plugin.sessions.flush()

    plugin2 = pf.PushForwarder(stub.FakeContext(default_platforms()), base_config())
    targets = plugin2.store.list_targets()

    assert len(targets) == 1
    assert targets[0].tags == ["alert"]
    assert plugin2.store.get_bot(QQ).schedule["outside_action"] == "queue"
    assert plugin2.dispatcher.queue.count(targets[0].id) == 1, "重启后积压消息不能丢"
    assert plugin2.sessions.has(f"{QQ}:{GROUP}:g1")


def test_corrupted_file_does_not_crash():
    """配置文件损坏时降级为空配置并保留现场，而不是让插件加载失败。"""
    data = tempfile.mkdtemp(prefix="pf_corrupt_")
    astrbot_path.get_astrbot_data_path = lambda: data
    (Path(data) / "push_forwarder").mkdir(parents=True, exist_ok=True)
    (Path(data) / "push_forwarder" / "targets.json").write_text("{ 坏掉的 json", encoding="utf-8")

    plugin = pf.PushForwarder(stub.FakeContext(default_platforms()), base_config())
    assert plugin.store.list_targets() == []
    assert (Path(data) / "push_forwarder" / "targets.json.bad").exists()


# ------------------------------------------------------------------ 运行器


async def _drain(gen):
    """跑完一个异步生成器（指令 handler）或等待一个协程。"""
    if hasattr(gen, "__aiter__"):
        async for _ in gen:
            pass
    else:
        await gen


def test_wecom_key_query_authenticates():
    """企微地址里的 ?key= 等价于 Token，推送方粘过来的 URL 要能原样用。"""
    plugin, _, _ = make_plugin(receiver_token="secret-token")
    receiver = with_fake_web(plugin)

    resp = asyncio.run(
        receiver._handle_push(
            FakeRequest(
                query={"key": "secret-token"},
                json_body={"msgtype": "text", "text": {"content": "hello"}},
            )
        )
    )
    assert resp.data.get("errcode") == 0


def test_wecom_response_uses_errcode():
    """企微通道按 errcode 判成败，缺这个字段会被当成失败。"""
    plugin, _, _ = make_plugin(receiver_token="secret-token")
    receiver = with_fake_web(plugin)

    resp = asyncio.run(
        receiver._handle_push(
            FakeRequest(
                headers={"X-Token": "secret-token"},
                json_body={"msgtype": "text", "text": {"content": "hello"}},
            )
        )
    )
    assert resp.status == 200
    assert resp.data["errcode"] == 0 and resp.data["errmsg"] == "ok"
    assert "data" in resp.data, "转发结果仍要带上，方便排查"


def test_wecom_error_uses_errcode_and_http_status():
    """错误同时用非零 errcode 和 4xx 表达：两种判定方式的推送方都能发现。"""
    plugin, _, _ = make_plugin(receiver_token="secret-token")
    receiver = with_fake_web(plugin)

    resp = asyncio.run(
        receiver._handle_push(
            FakeRequest(
                headers={"X-Token": "secret-token"},
                json_body={"msgtype": "image", "image": {"base64": "x", "md5": "y"}},
            )
        )
    )
    assert resp.status == 400
    assert resp.data["errcode"] == 40008
    assert "errmsg" in resp.data


def test_wecom_auth_failure_uses_errcode():
    plugin, _, _ = make_plugin(receiver_token="secret-token")
    receiver = with_fake_web(plugin)

    resp = asyncio.run(receiver._handle_push(FakeRequest(query={"key": "wrong"})))
    assert resp.status == 401
    assert resp.data["errcode"] == 40001


def test_wecom_detected_from_body_without_key():
    """没带 key 但直接 POST 企微消息体的，响应同样按企微的规矩回。"""
    plugin, _, _ = make_plugin(receiver_token="secret-token")
    receiver = with_fake_web(plugin)

    resp = asyncio.run(
        receiver._handle_push(
            FakeRequest(
                headers={"X-Token": "secret-token"},
                json_body={"msgtype": "text", "text": {"content": "hi"}},
            )
        )
    )
    assert resp.data.get("errcode") == 0


def test_normal_push_response_shape_unchanged():
    """普通推送的响应格式不能被企微兼容带跑偏。"""
    plugin, _, _ = make_plugin(receiver_token="secret-token")
    receiver = with_fake_web(plugin)

    resp = asyncio.run(
        receiver._handle_push(
            FakeRequest(headers={"X-Token": "secret-token"}, json_body={"text": "hi"})
        )
    )
    assert resp.data["status"] == "ok"
    assert "errcode" not in resp.data


def test_dry_run_matches_targets_without_sending():
    """自测要走完筛选，但一条都不能真发出去。"""
    plugin, ctx, _ = make_plugin()
    add_target(plugin, QQ, "g1")
    add_target(plugin, QQ, "g2")

    result = asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="自测", dry_run=True)))

    assert result["dry_run"] is True
    assert result["summary"]["dry_run"] == 2
    assert result["summary"]["sent"] == 0
    assert ctx.sent == []


def test_dry_run_reports_tag_mismatch():
    """标签对不上时命中 0 个 —— 这正是"推送成功但群里没消息"的常见原因。"""
    plugin, ctx, _ = make_plugin()
    add_target(plugin, QQ, "g1", tags=["alert"])

    result = asyncio.run(
        plugin.dispatcher.dispatch(PushMessage(text="x", tags=["other"], dry_run=True))
    )
    assert result["summary"]["dry_run"] == 0
    assert ctx.sent == []


def test_dry_run_does_not_enqueue_outside_schedule():
    """排进队列就会在时段开始时真发出去，那就不叫自测了。"""
    plugin, ctx, _ = make_plugin()
    target = add_target(plugin, QQ, "g1")
    cfg = plugin.store.get_bot(QQ)
    cfg.schedule = closed_schedule("queue")
    plugin.store.set_bot(QQ, cfg)

    result = asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="x", dry_run=True)))

    assert result["summary"]["queued"] == 1
    assert plugin.dispatcher.queue.count(target.id) == 0
    assert ctx.sent == []


def test_dry_run_not_written_to_history():
    plugin, _, _ = make_plugin()
    add_target(plugin, QQ, "g1")
    asyncio.run(plugin.dispatcher.dispatch(PushMessage(text="自测", dry_run=True)))

    stub.request.bind(query={"limit": "10"})
    records = asyncio.run(plugin.api_history())["data"]["records"]
    assert records == [], "自测不该混进推送记录"


def test_push_endpoint_dry_run_returns_200():
    """dry_run 没有 sent，但它是成功的，不能回 202 让推送方以为没生效。"""
    plugin, ctx, _ = make_plugin(receiver_token="secret-token")
    add_target(plugin, QQ, "g1")
    receiver = with_fake_web(plugin)

    resp = asyncio.run(
        receiver._handle_push(
            FakeRequest(
                headers={"X-Token": "secret-token"},
                json_body={"text": "自测", "dry_run": True},
            )
        )
    )
    assert resp.status == 200
    assert resp.data["data"]["dry_run"] is True
    assert ctx.sent == []


def test_api_selftest_reports_service_down():
    """接收服务没起来时直接说清楚，不要去打一个必定超时的回环请求。"""
    plugin, _, _ = make_plugin(receiver_enabled=True)
    plugin.receiver.running = False
    plugin.receiver.last_error = "端口 9966 启动失败"

    data = asyncio.run(plugin.api_selftest())["data"]
    assert data["ok"] is False
    assert data["stage"] == "listen"
    assert "端口 9966 启动失败" in data["message"]


def test_api_selftest_reports_disabled():
    plugin, _, cfg = make_plugin()
    cfg["receiver_enabled"] = False

    data = asyncio.run(plugin.api_selftest())["data"]
    assert data["ok"] is False and data["stage"] == "disabled"


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
            import traceback

            print(f"  ERROR {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
