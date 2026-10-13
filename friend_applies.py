# -*- coding: utf-8 -*-
"""好友申请状态持久化（2026-09-20 第二轮，防重复申请）。

场景：首次绑定 → 小号 `applyFriend` 发申请 → 对方在游戏内同意需要时间。
若在「等待接受」窗口内重新绑定（解绑→重绑），查好友列表时对方还不是好友，
会重复发申请——故以 account_id 为键**持久化「已申请过」**（friend_applies.json），
已申请过的不再发；**成为好友后清除**记录。
"""
from __future__ import annotations

import time
from typing import Any

from . import bindings

FILE = "friend_applies.json"


def _load() -> dict[str, Any]:
    return bindings._load_json(FILE, {})


def _save(data: dict[str, Any]) -> None:
    bindings._save_json(FILE, data)


def applied_at(account_id: int) -> int | None:
    """该账号此前是否已发过申请；返回申请时间戳，未发过返回 None。"""
    v = _load().get(str(account_id))
    return int(v) if v else None


def record(account_id: int, ts: int | None = None) -> None:
    """记录「已向该账号发过申请」（默认当前时间）。"""
    d = _load()
    d[str(account_id)] = int(ts if ts is not None else time.time())
    _save(d)


def clear(account_id: int) -> None:
    """成为好友 / 解绑后清除申请记录。"""
    d = _load()
    if str(account_id) in d:
        del d[str(account_id)]
        _save(d)


def prune(friend_ids: set[int]) -> int:
    """已是好友的账号，清除其申请记录；返回清理条数（实时轮询/绑定路径顺带调用）。"""
    d = _load()
    before = len(d)
    for k in list(d):
        if int(k) in friend_ids:
            del d[k]
    if len(d) != before:
        _save(d)
    return before - len(d)


def plan(is_friend: bool, prev_applied_at: int | None, *,
         nickname: str = "", account_id: int = 0) -> tuple[str, str]:
    """决定好友动作，返回 `(action, 回执文案)`（纯函数，可离线单测）。

    action ∈ {"already", "applied", "send"}：
    - already：已是好友 → 调用方无需发申请，可顺带 `clear()`
    - applied：此前已申请、等待对方同意 → **不重复发**，提示可强制重发
    - send   ：首次申请 → 调用方需执行 `applyFriend` 并 `record()`
    """
    if is_friend:
        return "already", "（已是小号雀魂好友，无需重新申请）"
    if prev_applied_at:
        return ("applied",
                f"（此前已向「{nickname or account_id}」发过申请，等待对方在游戏内同意；"
                "如需强制重发请用 /雀魂重新申请好友）")
    return ("send",
            f"已向「{nickname or account_id}」发出雀魂好友申请，"
            "请到游戏内同意——成为好友后实时监控生效")