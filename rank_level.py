# -*- coding: utf-8 -*-
"""雀魂段位表（纯数据 + 纯函数，无外部依赖）。

`level.id` 编码（2026-09-20 三方交叉验证）= `{1=四麻｜2=三麻}{大段位两位}{星一位}`：
- 大段位：`01` 初心 / `02` 雀士 / `03` 雀杰 / `04` 雀豪 / `05` 雀圣 / `06` 魂天
- 例：`10401` = 四麻雀豪1星；`20302` = 三麻雀杰2星

段位分规则（用户提供官方表 + 游戏内实测）：
- **初始分**：升级到该段位后获得的分数（实际可以低于它，不掉段）；
- **升级分**：分数**超过**它即升段；
- **掉段**：分数 **< 0**。

★ 魂天（`06xx`）为魂珠制（每 Lv 初始 10 珠、升级 20 珠），首版不做升降段评论。
"""
from __future__ import annotations

from typing import Any

MAJOR_NAMES: dict[int, str] = {
    1: "初心", 2: "雀士", 3: "雀杰", 4: "雀豪", 5: "雀圣", 6: "魂天",
}
STAR_NAMES: dict[int, str] = {1: "1星", 2: "2星", 3: "3星"}
CATEGORY_NAMES: dict[int, str] = {1: "四麻", 2: "三麻"}

# 升级分：分数超过它即升段（魂天不入表）
UPGRADE_SCORE: dict[int, dict[int, int]] = {
    1: {1: 20, 2: 80, 3: 200},
    2: {1: 600, 2: 800, 3: 1000},
    3: {1: 1200, 2: 1400, 3: 2000},
    4: {1: 2800, 2: 3200, 3: 3600},
    5: {1: 4000, 2: 6000, 3: 9000},
}

# 初始分：升级到该段位后获得的分数（下跌可低于它，不掉段）
INIT_SCORE: dict[int, dict[int, int]] = {
    1: {1: 0, 2: 0, 3: 0},
    2: {1: 300, 2: 400, 3: 500},
    3: {1: 600, 2: 700, 3: 1000},
    4: {1: 1400, 2: 1600, 3: 1800},
    5: {1: 2000, 2: 3000, 3: 4500},
}

DROP_THRESHOLD = 0     # 段位分 < 此值即掉段（用户确认：低于 0 掉段）
KYUUTEN_MAJOR = 6      # 魂天大段位编号（魂珠制，首版不评论升降段）


# ---------------------------------------------------------------- id 解析

def parse_level(level_id: int) -> tuple[int, int, int]:
    """`level.id` → `(麻种, 大段位, 星)`；非法/未定级返回 `(0, 0, 0)`。"""
    try:
        lid = int(level_id or 0)
    except (TypeError, ValueError):
        return 0, 0, 0
    if lid <= 0:
        return 0, 0, 0
    cat = lid // 10000
    major = (lid // 100) % 100
    star = lid % 100
    if cat not in CATEGORY_NAMES or not (1 <= major <= KYUUTEN_MAJOR) or star < 1:
        return 0, 0, 0
    return cat, major, star


def is_kyuuten(level_id: int) -> bool:
    """是否魂天（魂珠制，分数体系与一般段位不同）。"""
    _cat, major, _star = parse_level(level_id)
    return major == KYUUTEN_MAJOR


def level_name(level_id: int, *, with_category: bool = False) -> str:
    """段位 id → 可读名称，如 `雀豪1星` / `四麻雀豪1星`。"""
    cat, major, star = parse_level(level_id)
    if not major:
        return "未知段位"
    name = MAJOR_NAMES.get(major, f"段位{major}")
    if major == KYUUTEN_MAJOR:
        txt = f"{name}Lv{star}"
    else:
        txt = f"{name}{STAR_NAMES.get(star, f'{star}星')}"
    if with_category and cat in CATEGORY_NAMES:
        return f"{CATEGORY_NAMES[cat]}{txt}"
    return txt


def next_level_name(level_id: int) -> str:
    """升段后的段位名（用于「距升段」文案）。"""
    cat, major, star = parse_level(level_id)
    if not major or cat not in CATEGORY_NAMES:
        return "下一段位"
    if star >= 3:
        nxt = major + 1
        if nxt > KYUUTEN_MAJOR:
            return "魂天"
        return level_name(cat * 10000 + nxt * 100 + 1)
    return level_name(cat * 10000 + major * 100 + (star + 1))


# ---------------------------------------------------------------- 分数查询

def upgrade_score(level_id: int) -> int | None:
    """该段位的升级分（超过即升段）；魂天/未知返回 `None`。"""
    _cat, major, star = parse_level(level_id)
    return (UPGRADE_SCORE.get(major) or {}).get(star)


def init_score(level_id: int) -> int | None:
    """该段位的初始分（升级至此获得）；魂天/未知返回 `None`。"""
    _cat, major, star = parse_level(level_id)
    return (INIT_SCORE.get(major) or {}).get(star)


def gap_to_upgrade(level_id: int, score: int) -> int | None:
    """距升段还差多少分（≤0 表示已达/超过升级分）；魂天返回 `None`。"""
    up = upgrade_score(level_id)
    if up is None:
        return None
    try:
        return int(up) - int(score or 0)
    except (TypeError, ValueError):
        return None


def gap_to_drop(score: int) -> int:
    """距掉段还差多少分（分数 < 0 掉段，故即当前分数）。"""
    try:
        return int(score or 0) - DROP_THRESHOLD
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- 段位推算

def next_level_id(level_id: int) -> int:
    """升一级后的段位 id（星满 → 进大段位 1 星）；魂天/未知返回 0。"""
    cat, major, star = parse_level(level_id)
    if not major or cat not in CATEGORY_NAMES:
        return 0
    if star >= 3:
        nxt = major + 1
        if nxt > KYUUTEN_MAJOR:
            return 0
        return cat * 10000 + nxt * 100 + 1
    return cat * 10000 + major * 100 + (star + 1)


def prev_level_id(level_id: int) -> int:
    """降一级后的段位 id（1 星 → 退大段位 3 星）；已是最低段/未知返回 0。"""
    cat, major, star = parse_level(level_id)
    if not major or cat not in CATEGORY_NAMES:
        return 0
    if star <= 1:
        prev = major - 1
        if prev < 1:
            return 0
        return cat * 10000 + prev * 100 + 3
    return cat * 10000 + major * 100 + (star - 1)


def after_level(level_id: int, after_score: int) -> int:
    """由「本局段位 + 局后分数」推算**局后段位**（跨段时升/降一级）。"""
    _cat, major, _star = parse_level(level_id)
    if not major:
        return 0
    if major == KYUUTEN_MAJOR:       # 魂天魂珠制，不做普通升降段推断
        return int(level_id)
    up = upgrade_score(level_id)
    try:
        total = int(after_score)
    except (TypeError, ValueError):
        return int(level_id)
    if up is not None and total > up:
        return next_level_id(level_id) or int(level_id)
    if total < DROP_THRESHOLD:
        return prev_level_id(level_id) or int(level_id)
    return int(level_id)


def after_score_of(level_id: int, before_score: int, pt: int) -> int:
    """局后分数：同段位内 = 局前分 + 本局 PT（精确）；跨段（升/降）按新段位初始分近似。"""
    try:
        total = int(before_score or 0) + int(pt or 0)
    except (TypeError, ValueError):
        return 0
    new_lid = after_level(level_id, total)
    if new_lid and new_lid != int(level_id or 0):
        init = init_score(new_lid)
        if init is not None:
            return int(init)
    return total


# ---------------------------------------------------------------- 取值适配

def _get(obj: Any, key: str) -> int:
    """同时兼容 dataclass（PlayerResult）与 dict（parse_record 的 players）。"""
    if isinstance(obj, dict):
        val = obj.get(key, 0)
    else:
        val = getattr(obj, key, 0)
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def pick_level(player: Any, category: int) -> tuple[int, int]:
    """按麻种取该玩家的 `(段位 id, 段位分)`。

    ★ 优先**按 id 前缀**匹配麻种（`level` / `level3` 两套都试），
    避免字段名约定（level=四麻、level3=三麻）万一与实际相反时取错体系；
    前缀都不匹配时按字段名约定兜底。
    """
    cat = int(category or 0)
    pairs = (
        (_get(player, "level_id"), _get(player, "level_score")),
        (_get(player, "level_id3"), _get(player, "level_score3")),
    )
    for lid, sc in pairs:
        if lid and parse_level(lid)[0] == cat:
            return lid, sc
    return pairs[1] if cat == 2 else pairs[0]
