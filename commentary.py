# -*- coding: utf-8 -*-
"""对局结果增强评论（纯函数，便于离线单测）。

输入：`ctx`（本局事实）+ `history`（上一局为止的顺位历史）+ `settings`（阈值），
输出：评论行列表（按优先级排序，最多 `MAX_LINES` 行）。

规则表（见 Feature-04 §2.2）：
- A1 段位分变化行（恒显示）  A2 升段  A3 降段  A4 距升段临界  A5 距掉段临界
- B1 连续一位  B2 连续垫底  B3 连续无一位  B4 连续无垫底
- C1 被击飞  C2 击飞别人

口径（用户 2026-09-20 拍板）：
- 段位分 = 段位分变化 + 终局得点 + 顺位（不做局内走势）；
- 三麻以**末位（第三位）**替代「第四位」；
- 连续统计**按麻种分开**（调用方保证 history 只含同麻种序列）。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from . import rank_level as rl

MAX_LINES = 4                    # 评论总行数上限（防刷屏）
DEFAULT_GAP_LEVELS = (200, 100, 50)
DEFAULT_STREAK_REPEAT = 3        # 连续同顺位（连续一位 / 连续垫底）
DEFAULT_STREAK_ABSENT = 5        # 连续未获得某顺位（未获一位 / 未垫底）


def _int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _parse_gap_levels(raw: Any) -> list[int]:
    """`"200,100,50"` → `[200, 100, 50]`（降序、去重、正整数）。"""
    if raw is None or raw == "":
        return list(DEFAULT_GAP_LEVELS)
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = str(raw).replace("，", ",").split(",")
    out: list[int] = []
    for it in items:
        n = _int(it, 0)
        if n > 0 and n not in out:
            out.append(n)
    return sorted(out, reverse=True) or list(DEFAULT_GAP_LEVELS)


def _streak_tail(seq: Iterable[int], pred: Callable[[int], bool]) -> int:
    """从序列**尾部**起连续满足条件的个数。"""
    n = 0
    for v in reversed(list(seq)):
        if pred(v):
            n += 1
        else:
            break
    return n


def build(ctx: dict[str, Any], history: dict[str, Any] | None = None,
          settings: dict[str, Any] | None = None) -> list[str]:
    """生成本局评论行（可能为空列表）。"""
    ctx = ctx or {}
    settings = settings or {}
    gap_levels = _parse_gap_levels(settings.get("commentary_gap_levels"))
    max_gap, min_gap = gap_levels[0], gap_levels[-1]
    s_repeat = _int(settings.get("commentary_streak_repeat"), DEFAULT_STREAK_REPEAT) or DEFAULT_STREAK_REPEAT
    s_absent = _int(settings.get("commentary_streak_absent"), DEFAULT_STREAK_ABSENT) or DEFAULT_STREAK_ABSENT

    cat = _int(ctx.get("category"), 1)
    rank = _int(ctx.get("rank"))
    player_count = _int(ctx.get("player_count")) or (3 if cat == 2 else 4)
    final_point = _int(ctx.get("final_point"))
    lid = _int(ctx.get("level_id"))                       # 局后段位
    score = _int(ctx.get("level_score"))                  # 局后分数
    before_lid = _int(ctx.get("before_level_id")) or lid  # 局前段位
    before_score = _int(ctx.get("before_level_score"))    # 局前分数
    pt = _int(ctx.get("pt"))                              # 本局段位分变化

    head: list[str] = []
    comments: list[str] = []

    promoted = before_lid > 0 and lid > 0 and lid > before_lid
    demoted = before_lid > 0 and lid > 0 and lid < before_lid
    kyuuten = rl.is_kyuuten(lid)

    # ---------------- A1 段位分变化行 ----------------
    if lid > 0:
        if promoted or demoted:
            head.append(f"段位分：{rl.level_name(before_lid)} {before_score} → "
                        f"{rl.level_name(lid)}（{pt:+d}）")
        elif before_score > 0 and before_score != score:
            head.append(f"段位分：{rl.level_name(lid)} {before_score} → {score}（{pt:+d}）")
        elif before_score > 0:
            head.append(f"段位分：{rl.level_name(lid)} {before_score}（{pt:+d}）")
        else:
            head.append(f"段位分：{rl.level_name(lid)} {score}")

    # ---------------- C 击飞（优先级最高）----------------
    kicked_names = [str(n) for n in (ctx.get("kicked_names") or []) if n]
    if ctx.get("kicked"):
        kicker = str(ctx.get("kicker_name") or "").strip()
        comments.append(f"💥 被「{kicker}」击飞（终局 {final_point}）" if kicker
                        else f"💥 被击飞（终局 {final_point}）")
    if kicked_names:
        comments.append("⚔️ 你击飞了" + "、".join(f"「{n}」" for n in kicked_names))

    # ---------------- A2/A3 升段 / 降段 ----------------
    if promoted:
        comments.append(f"🎉 升段！{rl.level_name(before_lid)} → {rl.level_name(lid)}")
    elif demoted:
        comments.append(f"💧 降段：{rl.level_name(before_lid)} → {rl.level_name(lid)}")

    # ---------------- A5/A4 掉段 / 升段临界（未跨段时）----------------
    if lid > 0 and not kyuuten and not promoted and not demoted:
        if 0 < score <= max_gap:
            comments.append(f"⚠️ 距掉段只剩 {score} 分")
        gap = rl.gap_to_upgrade(lid, score)
        if gap is not None and 0 < gap <= max_gap:
            icon = "🔥" if gap <= min_gap else "📈"
            comments.append(f"{icon} 距升段还差 {gap} 分（{rl.next_level_name(lid)} 需 {rl.upgrade_score(lid)}）")

    # ---------------- B 连续顺位 ----------------
    seq = list((history or {}).get(str(cat)) or (history or {}).get(cat) or [])
    seq = [_int(v) for v in seq]
    if rank:
        seq.append(rank)
    if rank and seq:
        last_pos = player_count
        n_win = _streak_tail(seq, lambda v: v == 1)
        n_last = _streak_tail(seq, lambda v: v == last_pos)
        if n_win >= s_repeat:
            comments.append(f"🔥 连续 {n_win} 把一位！")
        elif n_last >= s_repeat:
            comments.append(f"🥶 连续 {n_last} 把垫底")
        else:
            n_no_win = _streak_tail(seq, lambda v: v != 1)
            n_no_last = _streak_tail(seq, lambda v: v != last_pos)
            if n_no_win >= s_absent:
                comments.append(f"😶 连续 {n_no_win} 把没吃到一位")
            elif n_no_last >= s_absent:
                comments.append(f"🍀 连续 {n_no_last} 把没垫底")

    return (head + comments)[:MAX_LINES]
