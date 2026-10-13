# -*- coding: utf-8 -*-
"""权限判定（纯逻辑，不依赖 nonebot，便于离线单测）。

口径（2026-09-21 用户拍板）：管理员命令 `/雀魂代绑定`、`/雀魂代解绑` 对
**机器人超管（`SUPERUSERS`）或 群主/群管理员** 开放；其余人会被拒绝并给自助指引。

- `is_admin()` 是纯函数：超管集合与角色由调用方传入（便于测试）；
- `superuser_ids()` 负责从 nonebot 配置读超管（读不到时回退环境变量 `SUPERUSERS`）；
- `role_of()` 从事件里取群内角色（私聊返回空串）。
"""
from __future__ import annotations

import os

ADMIN_ROLES = ("admin", "owner")   # 群管理员 / 群主


def superuser_ids() -> set[str]:
    """读取机器人超管列表（nonebot 配置优先，缺失时回退 `SUPERUSERS` 环境变量）。"""
    ids: set[str] = set()
    try:
        from nonebot import get_driver
        for x in (get_driver().config.superusers or []):
            ids.add(str(x))
    except Exception:  # noqa: BLE001 —— 离线/单测环境没有 nonebot 配置
        pass
    if not ids:
        raw = os.environ.get("SUPERUSERS") or ""
        ids.update(x for x in raw.replace(",", " ").split() if x)
    return ids


def is_admin(*, user_id: str, role: str = "", superusers: set[str] | None = None) -> bool:
    """是否可执行管理员命令：**超管 或 群主/群管理员**。"""
    uid = str(user_id or "").strip()
    sups = superuser_ids() if superusers is None else set(str(x) for x in superusers)
    if uid and uid in sups:
        return True
    return str(role or "").strip() in ADMIN_ROLES


def role_of(event) -> str:
    """取事件里的群内角色（私聊 / 取不到 → 空串）。"""
    return str(getattr(getattr(event, "sender", None), "role", "") or "")


def deny_text() -> str:
    """权限不足时的统一提示。"""
    return ("该命令仅**群主/群管理员**或**机器人超管**可用。\n"
            "群友自助绑定请直接发 /雀魂绑定 <雀魂ID>")
