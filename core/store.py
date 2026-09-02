"""转发目标 / 机器人配置 / 推送历史的持久化。

数据落在 AstrBot 的 data 目录下（data/push_forwarder/），不写插件目录，
以免插件更新或重装时丢失配置。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import GROUP, BotConfig, Target, default_schedule
from .utils import atomic_write_json, gen_id, load_json, now_ts

SCHEMA_VERSION = 1


class TargetStore:
    """转发目标与各机器人配置的读写。

    单事件循环内使用，方法均为同步；写操作走原子替换，中断不会损坏文件。
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "targets.json"
        self.bots: dict[str, BotConfig] = {}
        self.targets: list[Target] = []
        self.load()

    # ------------------------------------------------------------------ 读写

    def load(self) -> None:
        raw = load_json(self.path, {}) or {}
        self.bots = {
            str(k): BotConfig.from_dict(v or {})
            for k, v in (raw.get("bots") or {}).items()
        }
        self.targets = []
        seen: set[str] = set()
        for item in raw.get("targets") or []:
            if not isinstance(item, dict):
                continue
            target = Target.from_dict(item)
            if not target.umo or not target.platform_id:
                continue
            if not target.id or target.id in seen:
                target.id = gen_id()
            seen.add(target.id)
            self.targets.append(target)

    def save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "version": SCHEMA_VERSION,
                "updated_at": now_ts(),
                "bots": {k: v.to_dict() for k, v in self.bots.items()},
                "targets": [t.to_dict() for t in self.targets],
            },
        )

    # ------------------------------------------------------------ 目标增删查

    def list_targets(self, platform_id: str | None = None) -> list[Target]:
        if platform_id is None:
            return list(self.targets)
        return [t for t in self.targets if t.platform_id == platform_id]

    def get_target(self, target_id: str) -> Target | None:
        return next((t for t in self.targets if t.id == target_id), None)

    def find_by_umo(self, umo: str) -> Target | None:
        return next((t for t in self.targets if t.umo == umo), None)

    def add_target(
        self,
        *,
        platform_id: str,
        umo: str,
        message_type: str = GROUP,
        display_name: str = "",
        tags: list[str] | None = None,
    ) -> tuple[Target, bool]:
        """新增转发目标。umo 已存在时返回已有目标，第二个返回值表示是否新建。"""
        existing = self.find_by_umo(umo)
        if existing:
            if tags:
                merged = list(dict.fromkeys([*existing.tags, *tags]))
                existing.tags = merged
            if display_name and not existing.display_name:
                existing.display_name = display_name
            existing.enabled = True
            self.save()
            return existing, False

        target = Target(
            id=gen_id(),
            platform_id=platform_id,
            umo=umo,
            message_type=message_type,
            display_name=display_name,
            tags=list(tags or []),
        )
        self.targets.append(target)
        self.save()
        return target, True

    def remove_target(self, target_id: str) -> bool:
        before = len(self.targets)
        self.targets = [t for t in self.targets if t.id != target_id]
        if len(self.targets) != before:
            self.save()
            return True
        return False

    def remove_by_umo(self, umo: str) -> bool:
        target = self.find_by_umo(umo)
        return self.remove_target(target.id) if target else False

    def replace_targets(self, items: list[dict[str, Any]]) -> None:
        """整表替换，供 WebUI 面板保存时调用。"""
        targets: list[Target] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            target = Target.from_dict(item)
            if not target.umo or not target.platform_id:
                continue
            if not target.id or target.id in seen:
                target.id = gen_id()
            seen.add(target.id)
            targets.append(target)
        self.targets = targets
        self.save()

    def known_umos(self) -> set[str]:
        return {t.umo for t in self.targets}

    # ------------------------------------------------------------ 机器人配置

    def get_bot(self, platform_id: str) -> BotConfig:
        """取机器人配置，不存在时返回默认值（不落盘）。"""
        cfg = self.bots.get(platform_id)
        if cfg is None:
            cfg = BotConfig(schedule=default_schedule())
        return cfg

    def known_bot_ids(self) -> list[str]:
        """出现过的机器人实例 id：有单独配置的 + 有转发目标的。"""
        ids = list(self.bots.keys())
        for target in self.targets:
            if target.platform_id not in ids:
                ids.append(target.platform_id)
        return ids

    def resolve_bot(self, spec: str) -> str:
        """把推送里写的机器人标识解析成实例 id，解析不出返回空串。

        实例 id 优先，其次不分大小写再试一次（推送方手抄 id 时大小写常对不上），
        最后才认面板上的备注 —— 备注是机器人卡片上显示的那行字，不少人会照它写。

        备注必须唯一才认：重名时宁可解析失败让推送方看到报错，也不要猜一个
        发到别人的群里去。
        """
        text = (spec or "").strip()
        if not text:
            return ""
        ids = self.known_bot_ids()
        if text in ids:
            return text
        lowered = text.lower()
        hits = [i for i in ids if i.lower() == lowered]
        if len(hits) == 1:
            return hits[0]
        by_remark = [
            pid
            for pid, cfg in self.bots.items()
            if (cfg.remark or "").strip().lower() == lowered
        ]
        return by_remark[0] if len(by_remark) == 1 else ""

    def set_bot(self, platform_id: str, cfg: BotConfig) -> None:
        self.bots[platform_id] = cfg
        self.save()

    def resolve_schedule(self, target: Target) -> dict[str, Any] | None:
        """取目标实际生效的时段：勾选继承时用所属机器人的。"""
        if target.schedule_inherit:
            return self.get_bot(target.platform_id).schedule
        return target.schedule

    def bot_enabled(self, platform_id: str) -> bool:
        return self.get_bot(platform_id).enabled


class HistoryStore:
    """推送历史，环形保留最近 N 条，用于面板展示与排障。"""

    def __init__(self, data_dir: Path, max_records: int = 200) -> None:
        self.path = data_dir / "history.json"
        self.max_records = max(1, max_records)
        self.records: list[dict[str, Any]] = load_json(self.path, []) or []
        self._dirty = False

    def add(self, record: dict[str, Any]) -> None:
        self.records.append(record)
        if len(self.records) > self.max_records:
            self.records = self.records[-self.max_records :]
        self._dirty = True

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """按时间倒序返回最近的记录。"""
        return list(reversed(self.records[-max(1, limit) :]))

    def flush(self) -> None:
        if not self._dirty:
            return
        atomic_write_json(self.path, self.records)
        self._dirty = False

    def clear(self) -> None:
        self.records = []
        self._dirty = True
        self.flush()
