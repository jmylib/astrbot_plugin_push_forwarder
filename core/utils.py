"""通用工具：原子写 JSON、ID 生成、IP 白名单匹配。"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any


def now_ts() -> float:
    """当前 Unix 时间戳（秒）。"""
    return time.time()


def gen_id(prefix: str = "t") -> str:
    """生成短随机 ID，用于标识转发目标。"""
    return f"{prefix}_{secrets.token_hex(4)}"


def gen_token() -> str:
    """生成推送鉴权 Token。"""
    return secrets.token_urlsafe(24)


def load_json(path: Path, default: Any) -> Any:
    """读取 JSON 文件。文件不存在或损坏时返回 default。

    损坏的文件会被重命名为 .bad 保留现场，避免静默丢数据。
    """
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        try:
            path.replace(path.with_suffix(path.suffix + ".bad"))
        except OSError:
            pass
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    """原子写 JSON：先写同目录临时文件再替换，避免写入中断导致文件损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ip_allowed(remote_ip: str, whitelist: list[str]) -> bool:
    """判断来源 IP 是否命中白名单。白名单为空表示不限制。

    支持精确 IP 与 CIDR 网段；无法解析的条目按不匹配处理。
    """
    if not whitelist:
        return True
    try:
        addr = ipaddress.ip_address(remote_ip)
    except ValueError:
        return False
    for entry in whitelist:
        entry = str(entry).strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False
