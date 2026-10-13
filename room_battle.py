# -*- coding: utf-8 -*-
"""同室对战判定与文案（纯函数）。

需求（用户 2026-09-18 原话）：「当一个群的多个已绑定好友开始同一好友房时，你需要对这种情况
独立处理（例如"xxx、xxx在房间xxxxx决一死战！"之类的）」。

两条路径共用本模块：
- **实时（Phase 2）**：`detect_room_battle()` 吃好友对局状态快照 → 合并播报「决一死战！」；
- **赛后兜底（Phase 1）**：`detect_room_battle_from_parsed()` 吃已解析对局结果 → 合并战报。

判定口径：同群（`group_ids` 有交集）+ 同时「对局中」+ `room_id` 相同且 **>0**（自定义房）。
`room_id=0`（段位场匹配）不算同室对战——各自独立匹配，只是巧合同场。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class PlayingState:
    """一个绑定玩家的「对局中」快照（Phase 2 用）。"""

    qq: str
    account_id: int
    nickname: str
    group_ids: list[str] = field(default_factory=list)
    game_uuid: str = ""
    category: int = 0
    mode_id: int = 0
    room_id: int = 0


@dataclass
class RoomBattle:
    """一场同室对战（同群 + 同房间）。"""

    room_id: int
    group_id: str
    members: list[PlayingState] = field(default_factory=list)


def detect_room_battle(states: Iterable[PlayingState]) -> list[RoomBattle]:
    """从对局状态快照中找出「同群 + 同好友房」的组合。

    同一房间可能在多个群各有成员，故按 `(room_id, group_id)` 分别成组；
    组内成员 ≥2 才产出。
    """
    rooms: dict[int, list[PlayingState]] = {}
    for st in states:
        if st.room_id and st.room_id > 0:
            rooms.setdefault(st.room_id, []).append(st)

    out: list[RoomBattle] = []
    for room_id, members in rooms.items():
        groups: set[str] = set()
        for m in members:
            groups.update(m.group_ids)
        for gid in groups:
            same = [m for m in members if gid in m.group_ids]
            if len(same) >= 2:
                out.append(RoomBattle(room_id=room_id, group_id=gid, members=same))
    return out


def detect_room_battle_from_parsed(parsed: dict[str, Any],
                                   account_map: dict[int, str]) -> list[int]:
    """赛后兜底：从已解析对局结果中找出本局的绑定群友 account_id（≥2 才算同室）。

    `account_map` = `{account_id: qq}`；仅当 `room_id>0` 时成立（段位场不算）。
    """
    if not parsed.get("room_id"):
        return []
    hits = [p["account_id"] for p in parsed.get("players", []) if p.get("account_id") in account_map]
    return hits if len(hits) >= 2 else []


def _names(battle: RoomBattle) -> str:
    return "、".join(f"「{m.nickname or m.qq}」" for m in battle.members)


def format_room_battle_start(battle: RoomBattle) -> str:
    """开局播报：xxx、xxx 在房间 xxxxx 决一死战！"""
    return (
        "🀄 雀魂 · 同室对战\n"
        f"{_names(battle)} 在房间 {battle.room_id} 决一死战！\n"
        "（约 5 分钟后可在游戏内观战）"
    )


def format_room_battle_plain(battle: RoomBattle) -> str:
    """`room_battle_enabled=false` 时退回的普通提示（逐个成员各一行）。"""
    lines = ["🀄 雀魂动态"]
    for m in battle.members:
        lines.append(f"群友「{m.nickname or m.qq}」开始了对局（房间 {battle.room_id}）")
    lines.append("约 5 分钟后可在游戏内观战")
    return "\n".join(lines)
