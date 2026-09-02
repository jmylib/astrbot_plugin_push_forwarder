"""core.payload 的单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import AT_ALL, AT_NONE, AT_USERS  # noqa: E402
from core.payload import (  # noqa: E402
    PayloadError,
    PushMessage,
    merge_query_routing,
    parse_payload,
    query_to_dict,
    render,
    split_text,
)


def test_parse_plain_text():
    msg = parse_payload("服务器炸了")
    assert msg.text == "服务器炸了"
    assert msg.title == ""
    assert msg.tags == []


def test_parse_empty_raises():
    for bad in ["", "   ", {}, {"title": ""}, 123]:
        try:
            parse_payload(bad)
        except PayloadError:
            continue
        raise AssertionError(f"{bad!r} 应当被拒绝")


def test_parse_alias_fields():
    """兼容 content / message / body 等常见字段名。"""
    assert parse_payload({"content": "a"}).text == "a"
    assert parse_payload({"message": "b"}).text == "b"
    assert parse_payload({"body": "c"}).text == "c"
    assert parse_payload({"subject": "标题", "msg": "正文"}).title == "标题"
    # 只有标题也算有效内容
    assert parse_payload({"title": "只有标题"}).title == "只有标题"


def test_parse_tags_forms():
    assert parse_payload({"text": "x", "tags": ["a", "b"]}).tags == ["a", "b"]
    assert parse_payload({"text": "x", "tag": "a,b"}).tags == ["a", "b"]
    assert parse_payload({"text": "x", "tag": "a，b"}).tags == ["a", "b"]  # 全角逗号
    assert parse_payload({"text": "x"}, default_tags=["d"]).tags == ["d"]
    # 显式给了标签就不再套用默认标签
    assert parse_payload({"text": "x", "tag": "a"}, default_tags=["d"]).tags == ["a"]


def test_parse_at_forms():
    assert parse_payload({"text": "x"}).at_mode is None  # 未指定 -> 沿用目标配置
    assert parse_payload({"text": "x", "at": True}).at_mode == AT_ALL
    assert parse_payload({"text": "x", "at": False}).at_mode == AT_NONE
    assert parse_payload({"text": "x", "at": "all"}).at_mode == AT_ALL
    msg = parse_payload({"text": "x", "at": ["u1", "u2"]})
    assert msg.at_mode == AT_USERS and msg.at_users == ["u1", "u2"]
    msg2 = parse_payload({"text": "x", "at": {"mode": "users", "users": ["u9"]}})
    assert msg2.at_mode == AT_USERS and msg2.at_users == ["u9"]


def test_parse_targets_and_urgent():
    msg = parse_payload({"text": "x", "targets": "t_1,t_2", "urgent": True})
    assert msg.target_ids == ["t_1", "t_2"]
    assert msg.urgent is True
    assert parse_payload({"text": "x", "force": True}).urgent is True


def test_render_default_template():
    msg = PushMessage(title="告警", text="CPU 95%")
    assert render(msg) == "告警\nCPU 95%"


def test_render_drops_blank_lines_when_title_missing():
    """标题为空时不应留下开头的空行。"""
    assert render(PushMessage(text="正文")) == "正文"
    assert render(PushMessage(title="标题")) == "标题"


def test_render_custom_template_variables():
    msg = PushMessage(title="T", text="B", tags=["ops", "db"])
    out = render(msg, "[{tag}] {title}\n{text}")
    assert out == "[ops、db] T\nB"


def test_render_content_with_braces_does_not_break():
    """正文里出现 {} 时不应导致渲染报错。"""
    msg = PushMessage(text='{"json": "{value}"}')
    assert render(msg) == '{"json": "{value}"}'


def test_render_never_returns_empty():
    """模板被改成只剩空变量时要退回原始内容，而不是发出空消息。"""
    msg = PushMessage(title="标题", text="正文")
    assert render(msg, "{tag}") == "标题\n正文"


def test_split_text_no_limit():
    assert split_text("abc", 0) == ["abc"]
    assert split_text("abc", 100) == ["abc"]


def test_split_text_truncate_mode():
    out = split_text("a" * 50, 10, split=False)
    assert len(out) == 1 and out[0].endswith("…") and len(out[0]) == 10


def test_split_text_prefers_newline():
    """切分点落在换行处，且每片尽量填满窗口以减少消息条数。"""
    text = "第一段内容\n第二段内容\n第三段内容"
    out = split_text(text, 12)
    assert len(out) == 2
    assert out[0] == "第一段内容\n第二段内容"
    assert out[1] == "第三段内容"
    assert "".join(out).replace("\n", "") == text.replace("\n", "")
    assert all(c == c.strip() for c in out)  # 切片首尾不残留空白


def test_split_text_hard_cut_when_no_boundary():
    text = "x" * 25
    out = split_text(text, 10)
    assert [len(c) for c in out] == [10, 10, 5]
    assert "".join(out) == text


def test_push_message_roundtrip():
    msg = PushMessage(title="t", text="b", tags=["a"], at_mode=AT_ALL, urgent=True)
    restored = PushMessage.from_dict(msg.to_dict())
    assert restored.title == msg.title
    assert restored.tags == msg.tags
    assert restored.at_mode == msg.at_mode
    assert restored.urgent is True


def test_wecom_text_at_all():
    """企微 text 消息 + @all，应当映射成本插件的 at_mode=all。"""
    msg = parse_payload(
        {
            "msgtype": "text",
            "text": {"content": "CPU 95%", "mentioned_list": ["@all"]},
        }
    )
    assert msg.text == "CPU 95%"
    assert msg.at_mode == AT_ALL


def test_wecom_userid_degrades_to_text():
    """企微的 userid 在 QQ/微信那边对不上号，退化成正文前缀而不是真 @。"""
    msg = parse_payload(
        {
            "msgtype": "text",
            "text": {"content": "该你了", "mentioned_list": ["wangqing", "lisi"]},
        }
    )
    assert msg.text == "@wangqing @lisi\n该你了"
    assert msg.at_mode is None, "对不上的 ID 不该冒充成真的 @"


def test_wecom_mobile_is_not_forwarded():
    """手机号不能被转发到别的群里去。"""
    msg = parse_payload(
        {
            "msgtype": "text",
            "text": {"content": "上线了", "mentioned_mobile_list": ["13800001111"]},
        }
    )
    assert "13800001111" not in msg.text
    assert msg.at_mode is None

    # 但手机号列表里的 @all 仍然算数
    all_msg = parse_payload(
        {
            "msgtype": "text",
            "text": {"content": "上线了", "mentioned_mobile_list": ["@all"]},
        }
    )
    assert all_msg.at_mode == AT_ALL


def test_wecom_markdown_mention_syntax():
    """markdown 的 <@userid> 写法只有企微认，别的平台会看到一串尖括号。"""
    msg = parse_payload(
        {"msgtype": "markdown", "markdown": {"content": "<@zhangsan> 看下日志"}}
    )
    assert msg.text == "@zhangsan 看下日志"


def test_wecom_news_single_article():
    msg = parse_payload(
        {
            "msgtype": "news",
            "news": {
                "articles": [
                    {
                        "title": "发布完成",
                        "description": "v2.1 已上线",
                        "url": "https://example.com/r/1",
                    }
                ]
            },
        }
    )
    assert msg.title == "发布完成"
    assert msg.text == "v2.1 已上线\nhttps://example.com/r/1"


def test_wecom_news_multiple_articles():
    msg = parse_payload(
        {
            "msgtype": "news",
            "news": {
                "articles": [
                    {"title": "A", "url": "https://e.com/a"},
                    {"title": "B", "url": "https://e.com/b"},
                ]
            },
        }
    )
    assert msg.title == ""
    assert "A" in msg.text and "B" in msg.text


def test_wecom_template_card():
    msg = parse_payload(
        {
            "msgtype": "template_card",
            "template_card": {
                "card_type": "text_notice",
                "main_title": {"title": "磁盘告警", "desc": "sda1 使用率 92%"},
                "horizontal_content_list": [{"keyname": "主机", "value": "web-01"}],
                "card_action": {"type": 1, "url": "https://example.com/alert"},
            },
        }
    )
    assert msg.title == "磁盘告警"
    assert "sda1 使用率 92%" in msg.text
    assert "主机：web-01" in msg.text
    assert "https://example.com/alert" in msg.text


def test_wecom_unsupported_msgtype():
    """图片/文件/语音没有可读正文，明确拒绝而不是静默吞掉。"""
    for bad in ("image", "file", "voice"):
        try:
            parse_payload({"msgtype": bad, bad: {"base64": "xx", "md5": "yy"}})
        except PayloadError as e:
            assert bad in str(e)
            continue
        raise AssertionError(f"msgtype={bad} 应当被拒绝")


def test_wecom_empty_content_raises():
    try:
        parse_payload({"msgtype": "text", "text": {"content": "   "}})
    except PayloadError:
        return
    raise AssertionError("空正文应当被拒绝")


def test_wecom_keeps_plugin_extensions():
    """本插件的扩展字段可以直接混在企微消息体里，方便按标签分流。"""
    msg = parse_payload(
        {
            "msgtype": "text",
            "text": {"content": "紧急"},
            "tags": ["alert", "ops"],
            "urgent": True,
            "at": {"mode": AT_USERS, "users": ["u1"]},
        }
    )
    assert msg.tags == ["alert", "ops"]
    assert msg.urgent is True
    assert msg.at_mode == AT_USERS and msg.at_users == ["u1"]


def test_wecom_path_tag_still_applies():
    """POST /push/<tag> 带来的默认标签对企微格式同样生效。"""
    msg = parse_payload(
        {"msgtype": "text", "text": {"content": "hi"}}, default_tags=["ops"]
    )
    assert msg.tags == ["ops"]


def test_non_wecom_body_unaffected():
    """没有 msgtype 的照旧走原来的解析路径。"""
    msg = parse_payload({"title": "T", "text": "B"})
    assert (msg.title, msg.text) == ("T", "B")


def test_wecom_markdown_strips_font_tags():
    """企微告警模板几乎都用 <font color>，别的平台只发纯文本，要去掉。"""
    msg = parse_payload(
        {
            "msgtype": "markdown",
            "markdown": {
                "content": '<font color="warning">磁盘告警</font>\n主机 web-01'
            },
        }
    )
    assert "<font" not in msg.text and "</font>" not in msg.text
    assert msg.text == "磁盘告警\n主机 web-01"


def test_wecom_tolerates_flat_shorthand():
    """有些实现偷懒写成 {"msgtype":"text","text":"..."}，也认。"""
    msg = parse_payload({"msgtype": "text", "text": "偷懒写法"})
    assert msg.text == "偷懒写法"


def test_dry_run_flag_parsed():
    for key in ("dry_run", "dryrun", "dry-run"):
        assert parse_payload({"text": "x", key: True}).dry_run, key
    assert not parse_payload({"text": "x"}).dry_run


def test_query_string_false_values_are_not_truthy():
    """GET 查询串传过来全是字符串，bool("0") 是 True，不能直接 bool()。"""
    assert not parse_payload({"text": "x", "dry_run": "0"}).dry_run
    assert not parse_payload({"text": "x", "urgent": "false"}).urgent
    assert parse_payload({"text": "x", "dry_run": "1"}).dry_run
    assert parse_payload({"text": "x", "urgent": "true"}).urgent


def test_dry_run_never_survives_persistence():
    """自测消息不落盘，重启后就不可能被当成真消息发出去。"""
    msg = parse_payload({"text": "x", "dry_run": True})
    assert "dry_run" not in msg.to_dict()
    assert not PushMessage.from_dict(msg.to_dict()).dry_run


def test_wecom_payload_supports_dry_run():
    msg = parse_payload(
        {"msgtype": "text", "text": {"content": "自测"}, "dry_run": True}
    )
    assert msg.dry_run and msg.text == "自测"


def test_bots_field_aliases():
    """指定机器人的几种写法都要认。"""
    assert parse_payload({"text": "x", "bot": "qq_1"}).bot_ids == ["qq_1"]
    assert parse_payload({"text": "x", "bots": ["a", "b"]}).bot_ids == ["a", "b"]
    assert parse_payload({"text": "x", "bot_id": "a"}).bot_ids == ["a"]
    assert parse_payload({"text": "x", "platform_id": "a"}).bot_ids == ["a"]
    # 逗号分隔（查询串里只能这么写）
    assert parse_payload({"text": "x", "bots": "a, b"}).bot_ids == ["a", "b"]
    assert parse_payload({"text": "x"}).bot_ids == []


def test_generic_platform_key_is_not_a_bot_selector():
    """推送方的 platform 字段常表示别的意思，认了它会把推送路由到零个目标。"""
    msg = parse_payload({"text": "x", "platform": "linux", "platforms": ["k8s"]})
    assert msg.bot_ids == []


def test_bot_ids_survive_persistence():
    """排队消息落盘后要保留机器人条件（dry_run 才是故意不落盘的那个）。"""
    msg = PushMessage(text="x", bot_ids=["qq_1"], dry_run=True)
    back = PushMessage.from_dict(msg.to_dict())
    assert back.bot_ids == ["qq_1"]
    assert back.dry_run is False


def test_wecom_payload_supports_bots():
    msg = parse_payload(
        {"msgtype": "text", "text": {"content": "CPU 95%"}, "bot": "qq_1"}
    )
    assert msg.bot_ids == ["qq_1"] and msg.text == "CPU 95%"


def test_merge_query_routing_only_touches_routing_keys():
    """URL 上的路由字段补进 body，但不许顶掉正文，也不该把 token 塞进去。"""
    body = {"msgtype": "text", "text": {"content": "hello"}}
    merged = merge_query_routing(
        body, {"bot": "qq_1", "tags": "alert", "key": "secret", "text": "别顶掉我"}
    )
    assert merged["bot"] == "qq_1" and merged["tags"] == "alert"
    assert merged["text"] == {"content": "hello"}, "正文只认请求体"
    assert "key" not in merged, "Token 不是路由字段，不该混进消息体"
    assert body == {"msgtype": "text", "text": {"content": "hello"}}, "不能改原对象"


def test_merge_query_routing_body_wins():
    merged = merge_query_routing({"bot": "in_body"}, {"bot": "in_url"})
    assert merged["bot"] == "in_body"


def test_query_to_dict_keeps_repeated_params():
    """?bot=a&bot=b 不能只剩一个 —— dict(query) 会丢掉前面的。"""

    class MultiQuery:
        """够用就好：模拟 aiohttp 的 MultiDict，迭代时重复键各吐一次。"""

        def __init__(self, pairs):
            self.pairs = pairs

        def __iter__(self):
            return iter(k for k, _ in self.pairs)

        def get(self, key, default=None):
            return next((v for k, v in self.pairs if k == key), default)

        def getall(self, key, default=None):
            found = [v for k, v in self.pairs if k == key]
            return found or (default if default is not None else [])

    out = query_to_dict(MultiQuery([("bot", "a"), ("bot", "b"), ("text", "x")]))
    assert out["bot"] == ["a", "b"]
    assert out["text"] == "x"


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
