# -*- coding: utf-8 -*-
"""轮询调度、去重与推送（业务核心，不直接依赖 nonebot）。

数据文件：`plugin_data/majsoul_tracker/monitor.json`

```json
{
  "baseline": {"<account_id>": true},              // 是否已建立基线（首轮不补推）
  "seen":     {"<account_id>": ["<uuid>", ...]},   // 已见过（最近 N 条，防重复）
  "pushed":   {"<uuid>": 1789740000},              // 已推送时间戳
  "rate":     {"<group_id>": {"minute": 28900000, "count": 2}}
}
```

推送出口通过 `sender(group_id, text)` 回调注入——便于离线单测（无需 nonebot）。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable

from . import bindings, card, commentary, paipu_parser, rank_level, room_battle, yaku_map
from . import provider_protocol as protocol
from .provider_protocol import ProviderError

PLUGIN_NAME = "majsoul_tracker"
MONITOR_FILE = "monitor.json"
STATS_FILE = "player_stats.json"   # Feature-04：顺位历史 + 段位快照
SEEN_KEEP = 80          # 每账号保留的已见 uuid 条数
SEQ_KEEP = 50           # Feature-04：每账号**每麻种**保留的顺位条数（★ 2026-09-22 用户要求走势取 50 局）
PUSHED_KEEP = 400       # 已推送记录保留条数
BASELINE_LIMIT = 50     # 首轮建基线时多拉一些，避免后续"翻出旧对局"批量补推
FETCH_GAP = 1.0         # 连续拉牌谱的最小间隔（★ 对齐牌谱屋 logGames.js `throttle()` 的 1 秒/条）
END_GRACE = 120         # ★ 对局刚结束的静默期：不足 END_GRACE 秒不拉牌谱，留到下一轮（错峰降 540）
MAX_FETCH_PER_TICK = 5  # 单轮最多拉取的牌谱数（防一次性打爆接口）
MAX_FETCH_PER_HOUR = 40 # ★ 每小时牌谱拉取预算（超出则本轮全部走对局摘要推送）
BUSY_COOLDOWN = 900     # 触发牌谱风控（code=540）后的冷却秒数（与牌谱屋失败退避 15 分钟一致）

Sender = Callable[..., Awaitable[None]]   # sender(group_id, text, image=None)


def _log(msg: str, *, level: str = "info", event: str = "tick") -> None:
    """统一日志出口：同时输出 stdout（journalctl）与**网页端运行日志**。

    ★ 网页端「插件运行日志」读的是 `plugin_data/<插件>/runtime_logs.json`
    （由 `web_admin.plugin_runtime.append_plugin_log` 写入）——**只 `print` 网页端看不到**，
    这正是本插件早期在网页端无日志的原因。
    """
    print(f"[majsoul_tracker] {msg}", flush=True)
    try:
        from web_admin.plugin_runtime import append_plugin_log
        append_plugin_log(PLUGIN_NAME, level=level, event=event, message=msg)
    except Exception:  # noqa: BLE001 —— 网页日志写入失败不影响主流程
        pass


# ---------------------------------------------------------------- 存储

def _load() -> dict[str, Any]:
    try:
        from web_admin.plugin_runtime import load_plugin_json
        data = load_plugin_json(PLUGIN_NAME, MONITOR_FILE, {})
    except Exception:  # noqa: BLE001
        data = bindings._load_json(MONITOR_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("baseline", {})
    data.setdefault("seen", {})
    data.setdefault("pushed", {})
    data.setdefault("rate", {})
    data.setdefault("cooldown_until", 0)
    data.setdefault("paipu_rate", {})     # ★ Feature-07：牌谱查询防刷限频（群+人 → 时间戳）
    return data


def _save(data: dict[str, Any]) -> None:
    try:
        from web_admin.plugin_runtime import save_plugin_json
        save_plugin_json(PLUGIN_NAME, MONITOR_FILE, data)
    except Exception:  # noqa: BLE001
        bindings._save_json(MONITOR_FILE, data)


def _trim(data: dict[str, Any]) -> None:
    for aid, uuids in list(data["seen"].items()):
        if len(uuids) > SEEN_KEEP:
            # ★ 2026-09-26：`seen` 的「优先端」在**头部**（见 `_merge_seen`），截断也要同口径
            data["seen"][aid] = uuids[:SEEN_KEEP]
    if len(data["pushed"]) > PUSHED_KEEP:
        items = sorted(data["pushed"].items(), key=lambda kv: kv[1], reverse=True)[:PUSHED_KEEP]
        data["pushed"] = dict(items)


def _merge_seen(existing: list[str], new: list[str]) -> list[str]:
    """合并已见 uuid（★去重，否则每轮重复登记会撑爆 seen）。

    ★ 2026-09-26 修复（线上暴露）：`new` = **本轮拉取窗口**（`fetch_recent_games` 的返回，
    **最新在前**，50 条），它必须**优先保留**。旧实现 `(existing + new)[-SEEN_KEEP:]`
    假设「列表末尾 = 最新」，而 `new` 实际是**倒序**：existing 满 80 条时，合并后按尾部
    截断会把 `new` 的**前半段（最新的对局）挤掉** → 线上表现＝被跳过的活动局每轮都重新
    变回「新对局」（日志每 60s 刷一条「跳过 1 局活动/大会场对局」）。
    ⚠️ 影响不止活动局：**任何「处理失败 / 未写 pushed」的对局**都会因 seen 丢失而每轮
    重试（重复拉牌谱 → 风控隐患），所以「优先保留 new」是必要加固。
    """
    return list(dict.fromkeys(list(new) + list(existing)))[:SEEN_KEEP]


# ---------------------------------------------------------------- 顺位历史 / 段位快照
#
# 数据文件：`plugin_data/majsoul_tracker/player_stats.json`（Feature-04）
#
# ```json
# {
#   "accounts": {
#     "12345678": {
#       "seq":   {"1": [{"u": "<uuid>", "r": 1, "t": 1789740000}], "2": [...]},
#       "level": {"1": {"id": 10401, "score": 927, "t": 1789740000}, "2": {...}}
#     }
#   }
# }
# ```
# ★ 顺位序列与段位快照**按麻种分开**：四麻（"1"）与三麻（"2"）的连续统计不混算。

def _load_stats() -> dict[str, Any]:
    try:
        from web_admin.plugin_runtime import load_plugin_json
        data = load_plugin_json(PLUGIN_NAME, STATS_FILE, {})
    except Exception:  # noqa: BLE001
        data = bindings._load_json(STATS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("accounts", {})
    return data


def _save_stats(stats: dict[str, Any]) -> None:
    try:
        from web_admin.plugin_runtime import save_plugin_json
        save_plugin_json(PLUGIN_NAME, STATS_FILE, stats)
    except Exception:  # noqa: BLE001
        bindings._save_json(STATS_FILE, stats)


def _account_stats(stats: dict[str, Any], account_id: int) -> dict[str, Any]:
    return stats.setdefault("accounts", {}).setdefault(str(int(account_id)), {})


def _seq_ranks(stats: dict[str, Any], account_id: int, category: int) -> list[int]:
    """该账号该麻种的顺位序列（旧 → 新）。"""
    acc = (stats.get("accounts") or {}).get(str(int(account_id))) or {}
    items = (acc.get("seq") or {}).get(str(int(category))) or []
    out: list[int] = []
    for it in items:
        try:
            r = int((it or {}).get("r") or 0)
        except (TypeError, ValueError):
            r = 0
        if r:
            out.append(r)
    return out


def _prev_level(stats: dict[str, Any], account_id: int,
                category: int) -> tuple[int, int]:
    """该账号该麻种**上一局结束后**的 (段位 id, 段位分)；无记录返回 (0, 0)。"""
    acc = (stats.get("accounts") or {}).get(str(int(account_id))) or {}
    rec = (acc.get("level") or {}).get(str(int(category))) or {}
    try:
        return int(rec.get("id") or 0), int(rec.get("score") or 0)
    except (TypeError, ValueError):
        return 0, 0


def _record(stats: dict[str, Any], account_id: int, category: int, uuid: str,
            rank: int, level_id: int, level_score: int, ts: int,
            point: int | None = None, pt: int | None = None) -> None:
    """登记一局：顺位入队（按 uuid 幂等）+ 刷新段位快照。

    ★ 2026-09-22（P2）扩容每局字段，供战绩卡画「段位分曲线 / 得点趋势」：
    - `p`  = 终局得点
    - `pt` = 本局段位分变化
    - `ls` = **局后**段位分
    ⚠️ 老记录只有 `u/r/t`，读取方必须容忍缺字段（图表自动降级）。
    """
    acc = _account_stats(stats, account_id)
    cat_key = str(int(category))
    now = int(ts or time.time())
    if int(rank or 0) > 0 and uuid:
        items = acc.setdefault("seq", {}).setdefault(cat_key, [])
        if all((it or {}).get("u") != uuid for it in items[-SEQ_KEEP:]):
            rec: dict[str, Any] = {"u": str(uuid), "r": int(rank), "t": now}
            if point is not None:
                rec["p"] = int(point)
            if pt is not None:
                rec["pt"] = int(pt)
            if int(level_id or 0) > 0 and level_score is not None:
                rec["ls"] = int(level_score)
            items.append(rec)
            if len(items) > SEQ_KEEP:
                del items[:-SEQ_KEEP]
    if int(level_id or 0) > 0:
        acc.setdefault("level", {})[cat_key] = {
            "id": int(level_id), "score": int(level_score or 0), "t": now,
        }


def _backfill_baseline(stats: dict[str, Any], account_id: int, games: list[Any]) -> None:
    """建基线时回填历史顺位与段位快照（不推送）——让连续统计从绑定当天就可用。"""
    for g in sorted(games, key=lambda x: int(getattr(x, "end_time", 0) or 0)):
        cat = int(getattr(g, "category", 0) or 1)
        focus = protocol.focus_player(g, account_id)
        lid, before = rank_level.pick_level(focus, cat) if focus is not None else (0, 0)
        pt = int(getattr(focus, "grading_score", 0) or 0) if focus is not None else 0
        # ★ 协议 level.score 是「本局结算前」的分 → 回填时换算成局后分（与 _consume_game 同口径）
        after = rank_level.after_score_of(lid, before, pt) if lid else 0
        _record(stats, account_id, cat, getattr(g, "uuid", ""),
                int(getattr(g, "rank", 0) or 0), lid, after,
                int(getattr(g, "end_time", 0) or 0),
                point=int(getattr(focus, "final_point", 0) or 0) if focus is not None else None,
                pt=pt if focus is not None else None)


# ---------------------------------------------------------------- 战绩读取（P2）

def _items(stats: dict[str, Any], account_id: int, category: int) -> list[dict]:
    acc = (stats.get("accounts") or {}).get(str(int(account_id))) or {}
    return list((acc.get("seq") or {}).get(str(int(category))) or [])


def categories_of(stats: dict[str, Any], account_id: int) -> dict[int, int]:
    """该账号各麻种的局数：`{1: 13, 2: 30}`（用于自动挑「数据最多的麻种」）。"""
    acc = (stats.get("accounts") or {}).get(str(int(account_id))) or {}
    out: dict[int, int] = {}
    for k, v in (acc.get("seq") or {}).items():
        try:
            n = len(v or [])
        except TypeError:
            n = 0
        if n:
            out[int(k)] = n
    return out


def player_history(account_id: int, category: int = 0) -> dict[str, Any]:
    """读某账号的战绩历史（供 `/雀魂战绩` 与战绩卡用）。

    `category=0` 时自动挑**局数最多**的麻种。返回：
    `{account_id, category, items, ranks, points, level_scores, level_id, level_score,
    level_name, summary}`；无数据时 `items=[]`。
    """
    stats = _load_stats()
    cats = categories_of(stats, account_id)
    if not category:
        category = max(cats, key=lambda k: cats[k]) if cats else 1
    items = _items(stats, account_id, category)
    ranks = [int(it.get("r") or 0) for it in items if int(it.get("r") or 0)]
    points = [int(it["p"]) for it in items if it.get("p") is not None]
    ls = [int(it["ls"]) for it in items if it.get("ls") is not None]
    lid, score = _prev_level(stats, account_id, category)
    n = len(ranks)
    last_rank = category and (4 if category == 1 else 3)
    summary = {
        "games": n,
        "avg_rank": round(sum(ranks) / n, 2) if n else 0.0,
        "win_rate": round(sum(1 for r in ranks if r == 1) / n, 4) if n else 0.0,
        "top2_rate": round(sum(1 for r in ranks if r <= 2) / n, 4) if n else 0.0,
        "last_rate": round(sum(1 for r in ranks if r == last_rank) / n, 4) if n else 0.0,
        "best_streak": 0,
        "level_delta": (ls[-1] - ls[0]) if len(ls) >= 2 else 0,
        "has_points": bool(points),
        "has_levels": len(ls) >= 2,
    }
    # 最长连续一位
    streak = best = 0
    for r in ranks:
        streak = streak + 1 if r == 1 else 0
        best = max(best, streak)
    summary["best_streak"] = best
    return {
        "account_id": int(account_id),
        "category": int(category),
        "categories": cats,                    # ★ 各麻种局数 {1: 5, 2: 17}（提示「另有 X 麻 N 局」用）
        "items": items,
        "ranks": ranks,
        "points": points,
        "level_scores": ls,
        "level_id": lid,
        "level_score": score,
        "level_name": rank_level.level_name(lid) if lid else "",
        "summary": summary,
    }


def _consume_game(stats: dict[str, Any], account_id: int, brief: Any,
                  parsed: dict[str, Any] | None,
                  cs: dict[str, Any]) -> dict[str, Any]:
    """为一局生成评论行并登记历史。

    返回 `{"comments": [...], "level_*": ..., "pt": ...}`——除评论行外还给出**图片卡**
    需要的段位数据（本段起始分 / 当前分 / 本段上限 / 上一局分数）。
    """
    cat = int(getattr(brief, "category", 0) or 1)
    rank = int(getattr(brief, "rank", 0) or 0)
    ts = int(getattr(brief, "end_time", 0) or 0)
    focus = protocol.focus_player(brief, account_id)
    # ★ 协议 `level.score` 是**本局结算前**的段位分（2026-09-20 线上实测：四麻 790 + 137 = 927
    #   与用户游戏内读数一致），故「局后分」= 局前分 + 本局 PT；跨段时按新段位初始分近似。
    before_lid, before_score = rank_level.pick_level(focus, cat) if focus is not None else (0, 0)
    pt = int(getattr(focus, "grading_score", 0) or 0) if focus is not None else 0
    if before_lid:
        lid = rank_level.after_level(before_lid, before_score + pt)
        score = rank_level.after_score_of(before_lid, before_score, pt)
    else:
        lid, score = 0, 0
    final_point = int(getattr(focus, "final_point", 0) or 0) if focus is not None else 0
    player_count = 3 if cat == 2 else 4

    if parsed:
        rank = int(parsed.get("rank") or rank)
        final_point = int(parsed.get("point") or final_point)
        player_count = int(parsed.get("player_count") or player_count)
    flight = (parsed or {}).get("flight") or {}

    prev_lid, prev_score = _prev_level(stats, account_id, cat)
    ctx = {
        "category": cat,
        "rank": rank,
        "player_count": player_count,
        "final_point": final_point,
        "level_id": lid,
        "level_score": score,
        "before_level_id": before_lid,
        "before_level_score": before_score,
        "pt": pt,
        "prev_level_id": prev_lid,
        "prev_level_score": prev_score,
        "kicked": bool(flight.get("kicked")),
        "kicker_name": str(flight.get("kicker_name") or ""),
        "kicked_names": list(flight.get("kicked_targets") or []),
    }
    lines: list[str] = []
    if cs.get("commentary_enabled", True) is not False:
        lines = commentary.build(ctx, history={str(cat): _seq_ranks(stats, account_id, cat)},
                                 settings=cs)
    # ★ 无论评论开关如何都要登记历史（否则重新开启后连续统计会断档）
    #   ★ P2：一并登记得点 / 本局 PT / 局后段位分（供战绩卡画曲线）
    _record(stats, account_id, cat, getattr(brief, "uuid", ""), rank, lid, score, ts,
            point=final_point, pt=pt)
    return {
        "comments": lines,
        "level_name": rank_level.level_name(lid) if lid else "",
        "level_id": lid,                       # ★ P3：段位徽章用
        # ★ 图片卡用：本段起始分 / 本段上限（升级分）/ 上一局结算后的分数
        "level_init": rank_level.init_score(lid) if lid else None,
        "level_next": rank_level.upgrade_score(lid) if lid else None,
        "level_prev": before_score or prev_score,
        "level_score": score,
        "pt": pt,
    }


# ---------------------------------------------------------------- 图片卡渲染

async def _render_card(kind: str, payload: dict[str, Any], enabled: bool,
                       theme: str = "dark") -> bytes | None:
    """渲染图片卡；**任何失败都返回 None**（调用方回退纯文本）。"""
    if not enabled or not card.available():
        return None
    fn = {"result": card.render_result, "room": card.render_room_battle,
          "playing": card.render_playing_start, "record": card.render_record}.get(kind)
    if fn is None:
        return None
    data = dict(payload or {})
    data.setdefault("theme", theme)
    try:
        return await asyncio.to_thread(fn, data)
    except Exception as e:  # noqa: BLE001 —— 渲染失败绝不影响推送
        _log(f"渲染{kind}卡失败，回退纯文本：{type(e).__name__}: {e}", level="warn")
        return None


async def _display_name(qq: str, group_id: str, resolver) -> str:
    """解析显示名（QQ 群名片/昵称，由 main 注入的 resolver 提供）；失败返回空串。"""
    if resolver is None:
        return ""
    try:
        names = await resolver([str(qq)], str(group_id))
        return str((names or {}).get(str(qq)) or "")
    except Exception as e:  # noqa: BLE001
        _log(f"解析 QQ {qq} 昵称失败：{type(e).__name__}: {e}")
        return ""


def _result_card_payload(*, qq: str, account_id: int, brief: Any,
                         parsed: dict[str, Any] | None, info: dict[str, Any],
                         theme: str, display: str = "") -> dict[str, Any]:
    """组装「对局结果卡」payload（`parsed=None` 即降级路径）。"""
    b = bindings.get(qq) or {}
    nick = b.get("majsoul_nickname") or str(qq)
    p = parsed or {}
    focus = protocol.focus_player(brief, account_id)
    rank = int(p.get("rank") or getattr(focus, "rank", 0) or getattr(brief, "rank", 0) or 0)
    point = int(p.get("point") or getattr(focus, "final_point", 0)
                or getattr(brief, "final_point", 0) or 0)
    end_ts = int(getattr(brief, "end_time", 0) or 0) or int(time.time())
    return {
        "qq": str(qq),
        "nickname": display or nick,          # 优先 QQ 群名片/昵称
        "majsoul_nick": nick,
        "time": paipu_parser.fmt_time(end_ts),
        "place": p.get("place") or paipu_parser.brief_place(brief),
        "rank": rank,
        "point": point,
        "delta": int(p.get("delta") or 0),
        "pt": int(info.get("pt") or 0),
        "level_name": info.get("level_name") or "",
        "level_id": info.get("level_id"),
        "level_init": info.get("level_init"),
        "level_score": info.get("level_score") or 0,
        "level_next": info.get("level_next"),
        "level_prev": info.get("level_prev") or 0,
        "hand": p.get("best_hand") if p else None,
        # ★ 2026-09-26：最大放铳（未放铳时卡片写「本场未放铳」，与「本场未和牌」并列）
        "deal": (p or {}).get("max_deal_in"),
        "deal_count": int((p or {}).get("deal_in_count") or 0),
        # ★ 2026-09-24：全玩家统计表（对局玩家/段位/点数变化/立直·荣和·自摸·放铳）
        #   只有成功拉到牌谱（`parsed` 非空）才有统计；降级摘要路径没有牌谱数据 → 不出表。
        "players": paipu_parser.player_stats_rows(p) if p else None,
        "brief_note": "" if p else "（牌谱明细暂不可用）",
        "comments": info.get("comments") or [],
        "theme": theme,
    }


def _room_card_payload(parsed: dict[str, Any], names: dict[int, str],
                       account_to_qq: dict[int, str], theme: str, title: str = "",
                       display_of: dict[int, str] | None = None) -> dict[str, Any]:
    """组装「同室对战战报卡」payload（`names` = account_id → 麦名/雀魂昵称）。"""
    disp = display_of or {}
    players = []
    for pl in sorted(parsed.get("players") or [], key=lambda x: x.get("rank") or 9):
        aid = int(pl.get("account_id") or 0)
        players.append({
            "rank": int(pl.get("rank") or 0),
            "qq": str(account_to_qq.get(aid, "")),
            "name": disp.get(aid) or names.get(aid) or pl.get("nickname")
                    or f"座位{pl.get('seat')}",
            "point": int(pl.get("point") or 0),
            "delta": int(pl.get("delta") or 0),
            "pt": int(pl.get("pt") or 0),
        })
    return {
        "room_id": parsed.get("room_id") or 0,
        "title": title,
        "players": players,
        "theme": theme,
    }


# ---------------------------------------------------------------- 限频

def _rate_allow(data: dict[str, Any], group_id: str, limit: int) -> bool:
    if limit <= 0:
        return True
    minute = int(time.time() // 60)
    rec = data["rate"].get(group_id) or {}
    if int(rec.get("minute", 0)) != minute:
        rec = {"minute": minute, "count": 0}
    if int(rec.get("count", 0)) >= limit:
        data["rate"][group_id] = rec
        return False
    rec["count"] = int(rec.get("count", 0)) + 1
    data["rate"][group_id] = rec
    return True


def _settled_uuids(games: list[Any], now: float | None = None) -> list[str]:
    """★ 2026-09-21 降风控：只登记「已过静默期」的对局 uuid。

    刚结束不足 `END_GRACE` 秒的对局**不进 seen** → 它下一轮仍会被当作新对局，
    于是自然错峰拉牌谱（对齐牌谱屋 logGames.js「对局结束后等 5 分钟再拉」的策略）。
    """
    cut = (now if now is not None else time.time()) - END_GRACE
    return [g.uuid for g in games if int(getattr(g, "end_time", 0) or 0) <= cut]


# ---------------------------------------------------------------- 主流程

async def tick(sender: Sender, *, account: str = "", password: str = "",
               limit_per_account: int = 10, first_backfill: bool = False,
               rate_limit: int = 5, room_battle_enabled: bool = True,
               with_paipu: bool = True,
               commentary_settings: dict[str, Any] | None = None,
               image_cards: bool = False, card_theme: str = "dark",
               name_resolver=None) -> dict[str, Any]:
    """执行一轮轮询：拉取 → 去重 → 解析 → 推送（Feature-04：附增强评论）。

    - `image_cards` / `card_theme`：是否配图片卡与主题（由 main 从插件设置注入）；
    - `name_resolver`：`async (qqs, group_id) -> {qq: 群名片}`，由 main 注入（取 QQ 昵称）；
      未注入或失败时回退用雀魂昵称。
    返回统计 `{checked, new, pushed, skipped_rate, errors}`。
    """
    stat: dict[str, Any] = {"checked": 0, "new": 0, "pushed": 0,
                            "skipped_rate": 0, "errors": [], "no_group": 0,
                            "skipped_activity": 0}   # ★ 2026-09-26 活动/大会场（tag=3）屏蔽计数
    targets = bindings.targets()
    if not targets:
        return stat

    protocol.configure(account, password)
    data = _load()
    stats = _load_stats()          # Feature-04：顺位历史 + 段位快照
    cs = dict(commentary_settings or {})

    # 牌谱接口风控冷却期（code=540）内**不拉牌谱**，但仍要查对局列表并降级推送
    # （★ 早期实现此处直接 return → 风控期间整轮静默，连新对局都不推）
    cooldown = float(data.get("cooldown_until") or 0)
    in_cooldown = cooldown > time.time()
    if in_cooldown:
        stat["errors"].append(f"牌谱接口冷却中（剩 {int(cooldown - time.time())}s），本轮仅推送对局摘要")

    # ★ 2026-09-21 降风控（用户要求：参考牌谱屋的访问频率）：每小时牌谱拉取预算。
    #   正常使用每局 1 条远不会触顶；此上限用于兜住异常场景（如刚上线大量补拉）。
    _now = time.time()
    fetch_log = [float(t) for t in (data.get("fetch_log") or []) if _now - float(t) < 3600]
    data["fetch_log"] = fetch_log
    over_budget = len(fetch_log) >= MAX_FETCH_PER_HOUR
    if over_budget and not in_cooldown:
        _log(f"牌谱拉取已达每小时上限 {MAX_FETCH_PER_HOUR} 条 → 本轮改用对局摘要推送", level="warn")
    no_paipu = in_cooldown or over_budget

    # 1) 逐账号拉最近对局，收集「新对局」
    #    candidates: uuid -> [(qq, account_id, brief)]
    candidates: dict[str, list[tuple[str, int, Any]]] = {}
    for qq, aid, _groups in targets:
        stat["checked"] += 1
        seen = set(data["seen"].get(str(aid), []))
        is_baseline = bool(data["baseline"].get(str(aid)))
        # 首轮多拉一些建立基线，避免后续配置放大时"翻出旧对局"批量补推
        fetch_limit = limit_per_account if is_baseline else max(limit_per_account, BASELINE_LIMIT)
        try:
            games = await protocol.fetch_recent_games(aid, limit=fetch_limit)
        except ProviderError as e:
            stat["errors"].append(f"{aid}: {e}")
            _log(f"查询 {aid} 失败：{e}")
            continue

        new_uuids = [g.uuid for g in games if g.uuid not in seen and g.uuid not in data["pushed"]]

        if not is_baseline and not first_backfill:
            # 首轮建立基线：只登记、不推送（避免刚绑定就刷屏）
            data["seen"][str(aid)] = _merge_seen(list(seen), _settled_uuids(games))
            data["baseline"][str(aid)] = True
            # ★ Feature-04：回填历史顺位与段位快照，使连续统计从绑定当天即可用
            _backfill_baseline(stats, aid, games)
            _log(f"{aid} 建立基线，登记 {len(games)} 条（不推送；已回填顺位历史）")
            continue

        data["baseline"][str(aid)] = True
        data["seen"][str(aid)] = _merge_seen(list(seen), _settled_uuids(games))
        activity_here: list[str] = []
        for g in games:
            if g.uuid in new_uuids:
                # ★ 2026-09-26 用户口径「**所有活动局进行屏蔽，不做处理和汇报**」：
                #   摘要 `tag=3`（其他/大会）即活动场 → **只登记 seen**（否则每轮都会被当成
                #   新对局重复识别），**不拉牌谱、不推送、不进战绩统计**。
                #   （实测：修罗之战 4 人场 `tag=3` / `sub_tag=40` / 全员 `grading_score=0`）
                if yaku_map.is_activity_tag(getattr(g, "tag", 0)):
                    activity_here.append(g.uuid)
                    continue
                candidates.setdefault(g.uuid, []).append((qq, aid, g))
        stat["new"] += len(new_uuids) - len(activity_here)
        stat["skipped_activity"] += len(activity_here)
        if activity_here:
            _log(f"跳过 {len(activity_here)} 局活动/大会场对局（tag=3，不推送·不计入战绩）："
                 f"{'、'.join(activity_here[:5])}")

    # ★ 2026-09-26 P0（线上事故）：**候选收集完立即落盘 seen**。
    #   事故经过：血战规则牌谱（`HuInfoXueZhanMid` 无 `point_sum`）让解析抛异常 →
    #   异常冒泡出 tick → 函数末尾的 `_save()` 没执行 → `seen` 没落盘 → 同一局下一轮
    #   又被当成新对局 → 每 60s 崩一次（实测连续 6 轮），最后把牌谱接口打成账号级风控
    #   `code=540`。现在 seen 在进入逐局处理前就已持久化，**任何后续异常都不会再死循环**。
    _trim(data)
    _save(data)
    _save_stats(stats)          # 建基线回填的顺位/段位快照也一并落盘

    if not candidates:
        return stat

    # 2) 逐个新对局处理（同一 uuid 只拉一次详情；★同一群同一局只推一条）
    #    ★ Feature-04：按结束时间升序处理——顺位历史与段位快照必须按时序写入
    acct_map = bindings.account_to_qq()
    fetched = 0
    ordered = sorted(candidates.items(),
                     key=lambda kv: int(getattr(kv[1][0][2], "end_time", 0) or 0))
    for uuid, hits in ordered:
        if fetched >= MAX_FETCH_PER_TICK:
            _log(f"单轮拉取上限 {MAX_FETCH_PER_TICK} 已达，剩余 {len(candidates) - fetched} 条留待下轮")
            break
        fetched += 1
        rec = None
        stop_after = False
        if no_paipu:
            _log(f"牌谱接口暂不可用（冷却/小时预算）→ {uuid} 直接用对局摘要推送（不拉牌谱）")
        else:
            await asyncio.sleep(FETCH_GAP)   # ★ 限速 1s/条（对齐牌谱屋 logGames.js throttle()）
            try:
                rec = await protocol.fetch_game_record(uuid)
                data["fetch_log"].append(time.time())   # ★ 计入每小时预算
            except ProviderError as e:
                stat["errors"].append(f"{uuid}: {e}")
                if e.code == 540:
                    # 账号级风控：进入冷却并停止本轮剩余拉取；本局仍用摘要降级推送
                    data["cooldown_until"] = time.time() + BUSY_COOLDOWN
                    stop_after = True
                    _log(f"雀魂牌谱接口风控（code=540）→ 冷却 {BUSY_COOLDOWN}s，本局改用对局摘要推送")
                else:
                    _log(f"拉取牌谱 {uuid} 失败：{e}，本局改用对局摘要推送")

        # ★ Feature-04：先按人结算「评论 + 历史登记」（每人每局只做一次），再分群推送
        # ★ 2026-09-26 P0：解析**集中在这里**并逐个 aid 兜底——原先 `by_group` 的两个分支里
        #   还有「缓存未命中就现场 parse」且**没有 try 包裹**，任何解析异常都会冒泡出 tick
        #   （该局已落盘所以不再死循环，但本轮剩余对局会被整轮跳过）。
        parsed_cache: dict[int, dict[str, Any]] = {}
        per_member: dict[int, dict[str, Any]] = {}
        if rec is not None:
            for _qq, aid, _g in hits:
                if aid in parsed_cache:
                    continue
                try:
                    parsed_cache[aid] = paipu_parser.parse_record(rec, aid)
                except Exception as e:  # noqa: BLE001 —— 解析失败不影响推送
                    _log(f"解析牌谱失败（{uuid}/{aid}）：{type(e).__name__}: {e}", level="warn")
        # 一个人视角都解不出来 → 本局整体走「摘要降级」路径（与拉不到牌谱同处理）
        parse_ok = rec is not None and bool(parsed_cache)
        if rec is not None and not parse_ok:
            _log(f"牌谱 {uuid} 无法解析出任何玩家视角 → 本局改用对局摘要推送", level="warn")

        for qq, aid, g in hits:
            if aid in per_member:
                continue
            p = parsed_cache.get(aid) if parse_ok else None
            try:
                per_member[aid] = _consume_game(stats, aid, g, p, cs)
            except Exception as e:  # noqa: BLE001 —— 评论失败不影响推送
                per_member[aid] = {"comments": []}
                _log(f"生成评论失败（{uuid}/{aid}）：{type(e).__name__}: {e}")

        # 该对局涉及哪些群（多个绑定同群时合并为一条，避免重复刷屏）
        by_group: dict[str, list[tuple[str, int]]] = {}
        for qq, aid, _g in hits:
            groups = bindings.groups_for_account(aid)
            if not groups:
                # ★ 2026-09-21：绑定没进群 → 既不会推送、也不写任何日志，线上静默到
                #   看不出问题（实证：12345678 私聊绑定 group_ids=[]，对局只记 pushed 不推）
                stat["no_group"] += 1
                _log(f"{aid} 有新对局但未加入任何推送群 → 不推送；"
                     f"请在目标群里发一次 /雀魂绑定 {aid}", level="warn")
                continue
            for gid in groups:
                by_group.setdefault(gid, []).append((qq, aid))

        for gid, members in by_group.items():
            payload = None
            text_builder = None    # ★ 有图时用来重新生成「精简文案」的回调
            if not parse_ok:
                # ★ 降级路径：牌谱不可用（风控/超时）**或解析失败**→ 只用对局摘要（名次/得点/PT）
                parts = []
                head_hit = None
                for qq, aid in members:
                    brief = next((b for _q, a, b in hits if a == aid), None)
                    if brief is None:
                        continue
                    info = per_member.get(aid) or {}
                    display = (bindings.get(qq) or {}).get("majsoul_nickname") or qq
                    parts.append(paipu_parser.format_brief(brief, display, aid,
                                                           with_paipu=with_paipu,
                                                           comments=info.get("comments")))
                    if head_hit is None:
                        head_hit = (qq, aid, brief, info)
                text = "\n\n".join(parts)
                if not text:
                    continue
                qq, aid, brief, info = head_hit
                payload = ("result", {"qq": str(qq), "account_id": aid, "brief": brief,
                                      "parsed": None, "info": info})
            else:
                # 解析已在上面统一完成并缓存（全失败时 `parse_ok=False` 走降级分支）
                parsed = parsed_cache.get(members[0][1]) or next(iter(parsed_cache.values()))
                hit_ids = room_battle.detect_room_battle_from_parsed(parsed, acct_map)

                if room_battle_enabled and len(hit_ids) >= 2:
                    # 2a) 同室对战：同群 ≥2 个绑定群友 + 自定义房 → 合并战报
                    names = {aid: (bindings.get(qq) or {}).get("majsoul_nickname") or qq
                             for aid, qq in acct_map.items()}
                    text = paipu_parser.format_room_battle(parsed, names, with_paipu=with_paipu)
                    who = "、".join(f"「{names.get(a)}」" for a in hit_ids)
                    payload = ("room", {"room_id": parsed.get("room_id") or 0,
                                        "title": f"{who} 在房间 {parsed.get('room_id')} 决一死战！",
                                        "hit_ids": hit_ids, "names": names})
                elif len(members) == 1:
                    # 2b) 常规单人结果（★含 Feature-04 增强评论）
                    qq, aid = members[0]
                    info = per_member.get(aid) or {}
                    display = (bindings.get(qq) or {}).get("majsoul_nickname") or qq
                    # ★ 两版文案：有图用 compact，无图用完整版
                    def _mk_text(has_img: bool, _p=parsed, _d=display, _i=info) -> str:
                        return paipu_parser.format_result(
                            _p, _d, with_paipu=with_paipu, comments=_i.get("comments"),
                            compact=has_img)
                    text_builder = _mk_text
                    text = _mk_text(False)
                    payload = ("result", {"qq": str(qq), "account_id": aid,
                                          "brief": next((b for _q, a, b in hits if a == aid), None),
                                          "parsed": parsed, "info": info})
                else:
                    # 2c) 段位场巧合同场（room_id=0）：合并多人视角为一条（图片取首位群友）
                    parts = []
                    for qq, aid in members:
                        p = parsed_cache.get(aid) or next(iter(parsed_cache.values()))
                        info = per_member.get(aid) or {}
                        display = (bindings.get(qq) or {}).get("majsoul_nickname") or qq
                        parts.append(paipu_parser.format_result(p, display, with_paipu=False,
                                                                comments=info.get("comments")))
                        if payload is None:
                            payload = ("result", {
                                "qq": str(qq), "account_id": aid,
                                "brief": next((b for _q, a, b in hits if a == aid), None),
                                "parsed": p, "info": info})
                    text = "\n\n".join(parts)

            if not _rate_allow(data, gid, rate_limit):
                stat["skipped_rate"] += 1
                _log(f"群 {gid} 达到限频，跳过 {uuid}")
                continue

            # ★ 2026-09-24 用户口径「有图片的话，文字说明就简化一下吧」：
            #   先尝试出图；**出图成功 → 文本用精简版**（明细交给图），
            #   出图失败（回退纯文本）→ 文本保持完整版，信息量不丢失。
            image = None
            if payload is not None:
                kind, raw = payload
                if kind == "result":
                    display = await _display_name(raw["qq"], gid, name_resolver)
                    card_payload = _result_card_payload(
                        qq=raw["qq"], account_id=raw["account_id"], brief=raw["brief"],
                        parsed=raw["parsed"], info=raw["info"], theme=card_theme,
                        display=display)
                else:
                    display_of = {}
                    for aid in raw["hit_ids"]:
                        qq = acct_map.get(aid)
                        if qq:
                            nm = await _display_name(qq, gid, name_resolver)
                            if nm:
                                display_of[aid] = nm
                    card_payload = _room_card_payload(
                        {"room_id": raw["room_id"], "players": parsed.get("players") or []},
                        raw["names"], acct_map, card_theme, raw["title"], display_of)
                image = await _render_card(kind, card_payload, image_cards, card_theme)

            if image is not None and text_builder is not None:
                text = text_builder(True)          # 有图 → 精简文案

            await _safe_send(sender, gid, text, stat, image=image)

        data["pushed"][uuid] = int(time.time())
        if stop_after:
            _log(f"风控冷却中，本轮剩余 {max(0, len(candidates) - fetched)} 条对局留待下轮")
            break

    _trim(data)
    _save(data)
    _save_stats(stats)
    return stat


async def _safe_send(sender: Sender, group_id: str, text: str, stat: dict,
                     image: bytes | None = None) -> None:
    """单条推送失败不影响整轮（记日志继续）。

    `image` = 图片卡 PNG bytes（`None` 表示纯文本）；发图失败由出口回退文本，不影响计数。
    """
    try:
        await sender(group_id, text, image=image)
        stat["pushed"] += 1
    except Exception as e:  # noqa: BLE001
        stat["errors"].append(f"推送 {group_id}: {type(e).__name__}: {e}")
        _log(f"推送到群 {group_id} 失败：{type(e).__name__}: {e}")


async def reset_baseline(account_id: int | None = None) -> None:
    """清除基线（下次轮询重新建立）；不传则清全部。"""
    data = _load()
    if account_id is None:
        data["baseline"] = {}
    else:
        data["baseline"].pop(str(account_id), None)
    _save(data)


# ================================================================ 牌谱链接查询（Feature-07，2026-09-28）
#
# 用户需求（原话）：「玩家给出一个牌谱链接，回复这局类似于对局明细的信息。」
# 设计要点（全部为了**不把牌谱接口打成 code=540**，铁律 4 / 铁律 22）：
#  ① 拉回来的**原始响应字节落盘缓存**（`plugin_data/majsoul_tracker/paipu_cache/<uuid>.bin`）
#     → 同一条牌谱查第二次**零网络请求**（`protocol.record_from_raw` 离线重建）；
#  ② 真实拉取受**同一套降风控约束**：1s 限速、每小时预算（与轮询共享 `fetch_log`）、
#     `code=540` 冷却 15 分钟（与轮询共享 `cooldown_until`）；
#  ③ 命令侧还有「同一群/同一人 N 秒一次」的防刷限频（`paipu_rate_allow`）。

PAIPU_CACHE_DIR = "paipu_cache"
PAIPU_CACHE_KEEP = 60      # 缓存保留的牌谱条数（按文件修改时间淘汰最旧）
PAIPU_QUERY_GAP = 20       # 同一「群+人」两次牌谱查询的最小间隔（秒）


def _cache_dir() -> str:
    """牌谱缓存目录（线上 = `plugin_data/majsoul_tracker/paipu_cache/`）。

    优先走 `web_admin.plugin_runtime.get_plugin_data_dir`（与其它数据文件同处），
    离线脚本/无 nonebot 时回退到 `_localdata/paipu_cache/`（与 `bindings._local_path` 同源）。
    """
    try:
        from web_admin.plugin_runtime import get_plugin_data_dir
        d = os.path.join(str(get_plugin_data_dir(PLUGIN_NAME)), PAIPU_CACHE_DIR)
    except Exception:  # noqa: BLE001
        d = os.path.join(os.path.dirname(bindings._local_path("_")), PAIPU_CACHE_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(uuid: str) -> str:
    """★ 防目录穿越：uuid 只允许十六进制/数字/连字符（解析器已保证，这里再兜一层）。"""
    safe = "".join(c for c in str(uuid or "") if c.isalnum() or c in "-_")
    return os.path.join(_cache_dir(), f"{safe}.bin")


def cache_has(uuid: str) -> bool:
    """该牌谱是否已在本地缓存（用于「本次是否真的联网」的提示与自检）。"""
    try:
        return os.path.getsize(_cache_path(uuid)) > 0
    except Exception:  # noqa: BLE001
        return False


def _cache_write(uuid: str, raw: bytes) -> None:
    """写缓存（原子替换）+ 按修改时间淘汰最旧。缓存失败绝不影响主流程。"""
    if not raw:
        return
    try:
        path = _cache_path(uuid)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
        files = [os.path.join(_cache_dir(), n) for n in os.listdir(_cache_dir())
                 if n.endswith(".bin")]
        if len(files) > PAIPU_CACHE_KEEP:
            files.sort(key=lambda p: os.path.getmtime(p))
            for p in files[:len(files) - PAIPU_CACHE_KEEP]:
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception as e:  # noqa: BLE001
        _log(f"写牌谱缓存失败（{uuid}）：{type(e).__name__}: {e}", level="warn", event="paipu")


def _cache_read(uuid: str) -> bytes | None:
    """读牌谱缓存；缺失/损坏返回 None（调用方走联网拉取）。"""
    try:
        with open(_cache_path(uuid), "rb") as f:
            raw = f.read()
        return raw or None
    except Exception:  # noqa: BLE001
        return None


def paipu_rate_allow(key: str, *, interval: int = PAIPU_QUERY_GAP,
                     now: float | None = None) -> tuple[bool, int]:
    """命令侧防刷限频：同一 `key`（群+人）`interval` 秒内只允许一次。

    返回 `(是否放行, 还需等待秒数)`。**缓存命中也要限频**——否则有人重复贴同一条链接刷屏。
    """
    data = _load()
    rate = data.setdefault("paipu_rate", {})
    now = float(now if now is not None else time.time())
    last = float(rate.get(str(key)) or 0)
    wait = int(interval - (now - last))
    if last and wait > 0:
        return False, wait
    rate[str(key)] = now
    # 只保留最近 200 个 key，避免无限增长
    if len(rate) > 200:
        items = sorted(rate.items(), key=lambda kv: float(kv[1]), reverse=True)[:200]
        data["paipu_rate"] = dict(items)
    _save(data)
    return True, 0


def build_paipu_detail(rec: Any, sharer_account_id: int = 0,
                       fallback_account_id: int = 0, match_id: int = 0) -> dict[str, Any]:
    """把 `GameRecord` 组装成「牌谱明细」结构（纯本地解析，不发网络请求）。

    主角（`sharer`）判定优先级：
      ① `sharer_account_id`（= 链接尾号 `_a<match_id>` 反推的账号，实测就是牌谱归属者）
         → `sharer_source="link"`，文案称「分享者」；
      ② `fallback_account_id`（= 查询者**自己绑定**的账号，且确实在这局里）
         → `sharer_source="self"`，文案称「本人」；
      ③ 都没有 → `sharer=None`，**不设主角**（只出全玩家明细，绝不编造谁是主角）。

    `match_id` 只用于**还原文案里的分享链接尾号**（`paipu_url`，见 `paipu_parser.paipu_url_of`）。
    """
    aids = [int(a.get("account_id") or 0) for a in (rec.accounts or [])]
    sid, source = 0, ""
    if int(sharer_account_id or 0) in aids:
        sid, source = int(sharer_account_id), "link"
    elif int(fallback_account_id or 0) in aids:
        sid, source = int(fallback_account_id), "self"
    focus_aid = int(sid or (aids[0] if aids else 0))
    base = paipu_parser.parse_record(rec, focus_aid)
    rows = paipu_parser.player_stats_rows(base)
    by_aid = {int(r.get("account_id") or 0): r for r in rows}
    three = int(base.get("player_count") or 4) == 3

    hands: list[dict[str, Any]] = []
    for a in rec.accounts or []:
        aid = int(a.get("account_id") or 0)
        p = base if aid == focus_aid else paipu_parser.parse_record(rec, aid)
        hands.append({
            "seat": int(a.get("seat") or 0),
            "account_id": aid,
            "name": a.get("nickname") or "",
            "rank": int((by_aid.get(aid) or {}).get("rank") or 0),
            "is_sharer": bool(sid and aid == sid),
            "best": p.get("best_hand"),
            "deal": p.get("max_deal_in"),
        })
    for r in rows:
        r["is_sharer"] = bool(sid and int(r.get("account_id") or 0) == sid)

    sharer = None
    if sid:
        pl = next((x for x in (base.get("players") or [])
                   if int(x.get("account_id") or 0) == sid), {})
        lid = int((pl.get("level_id3") if three else pl.get("level_id")) or 0)
        before = int((pl.get("level_score3") if three else pl.get("level_score")) or 0)
        row = by_aid.get(sid) or {}
        pt = int(row.get("pt") or 0)
        # ★ 铁律 11：牌谱 head.accounts 的段位分是**局前**快照 → 局后 = 局前 + 本局 PT
        after = rank_level.after_score_of(lid, before, pt) if lid else before
        sp = base if focus_aid == sid else paipu_parser.parse_record(rec, sid)
        sharer = {
            "account_id": sid,
            "source": source,
            "name": row.get("name") or "",
            "rank": int(row.get("rank") or 0),
            "point": int(row.get("point") or 0),
            "delta": int(row.get("delta") or 0),
            "pt": pt,
            "level_id": lid,
            "level_name": rank_level.level_name(lid) if lid else "",
            "level_before": before,
            "level_after": after,
            "level_init": rank_level.init_score(lid) if lid else 0,
            "level_next": rank_level.upgrade_score(lid) if lid else 0,
            "best_hand": sp.get("best_hand"),
            "max_deal_in": sp.get("max_deal_in"),
        }

    return {
        "uuid": str(rec.uuid or ""),
        # ★ 用户给的链接尾号要原样还回去（客户端分享链接的标准形式，见 paipu_url_of）
        "match_id": int(match_id or 0),
        "paipu_url": paipu_parser.paipu_url_of(str(rec.uuid or ""), int(match_id or 0)),
        "place": base.get("place") or "",
        "start_time": int(rec.start_time or 0),
        "end_time": int(rec.end_time or 0),
        "rounds": int(base.get("rounds") or 0),
        "player_count": int(base.get("player_count") or 0),
        "mode_id": int(rec.mode_id or 0),
        "category": int(base.get("category") or 0),
        "sharer_account_id": int(sid or 0),
        "sharer_source": source,
        "sharer": sharer,
        "players": rows,
        "hands": hands,
        "hule_count": len(base.get("hules") or []),
        "from_cache": False,
    }


async def query_paipu(uuid: str, *, account: str = "", password: str = "",
                      prefer_account_id: int = 0,
                      fallback_account_id: int = 0,
                      match_id: int = 0) -> dict[str, Any]:
    """查一条牌谱的明细：**先读本地缓存，未命中才联网**（返回 `{ok, detail, error, ...}`）。

    风控约束（与轮询一致，见铁律 4）：
    - 联网前检查 `cooldown_until`（`code=540` 冷却中直接拒绝，并提示剩余时间）；
    - 检查每小时预算 `MAX_FETCH_PER_HOUR`（与轮询共享 `fetch_log`）；
    - 拉取前 `FETCH_GAP`(1s) 限速；成功后记账 `fetch_log` 并**立即落盘**；
    - 命中 `code=540` → 记 `cooldown_until`（与轮询共享）。

    ⚠️ 返回的 `from_cache` 必须如实回传（前端文案会写「已缓存」，不许拿缓存冒充实时）。
    """
    uid = str(uuid or "").strip()
    if not uid:
        return {"ok": False, "error": "缺少牌谱 ID", "from_cache": False, "detail": None}

    raw = _cache_read(uid)
    if raw:
        try:
            rec = protocol.record_from_raw(raw, uid)
            detail = build_paipu_detail(rec, prefer_account_id, fallback_account_id, match_id)
            detail["from_cache"] = True
            _log(f"牌谱查询命中缓存：{uid}（{len(raw)}B，无网络请求）", event="paipu")
            return {"ok": True, "error": "", "from_cache": True, "detail": detail,
                    "record": rec}
        except Exception as e:  # noqa: BLE001 —— 缓存损坏 → 当作未命中，走联网
            _log(f"牌谱缓存损坏，改走联网：{uid}（{type(e).__name__}: {e}）",
                 level="warn", event="paipu")

    protocol.configure(account, password)
    data = _load()
    now = time.time()
    cool = float(data.get("cooldown_until") or 0)
    if cool > now:
        return {"ok": False, "from_cache": False, "detail": None,
                "error": f"牌谱接口冷却中（剩 {int(cool - now)} 秒，账号级风控），稍后再试"}
    log = [float(t) for t in (data.get("fetch_log") or []) if now - float(t) < 3600]
    data["fetch_log"] = log
    if len(log) >= MAX_FETCH_PER_HOUR:
        _save(data)
        return {"ok": False, "from_cache": False, "detail": None,
                "error": f"本小时牌谱拉取已达上限（{MAX_FETCH_PER_HOUR} 条），稍后再试"}

    await asyncio.sleep(FETCH_GAP)          # ★ 降风控：1s 限速（对齐牌谱屋 throttle）
    try:
        raw = await protocol.fetch_game_record_raw(uid)
    except ProviderError as e:
        if int(getattr(e, "code", 0) or 0) == 540:
            data["cooldown_until"] = time.time() + BUSY_COOLDOWN
        _save(data)
        _log(f"牌谱查询失败：{uid}：{e}", level="warn", event="paipu")
        return {"ok": False, "from_cache": False, "detail": None,
                "error": f"拉取失败：{e}"}

    data["fetch_log"].append(time.time())
    _save(data)
    _cache_write(uid, raw)
    rec = protocol.record_from_raw(raw, uid)
    detail = build_paipu_detail(rec, prefer_account_id, fallback_account_id, match_id)
    _log(f"牌谱查询成功（联网拉取）：{uid}（{len(raw)}B）｜"
         f"{detail.get('place')}｜{detail.get('player_count')} 人", event="paipu")
    return {"ok": True, "error": "", "from_cache": False, "detail": detail,
            "record": rec}


def stats() -> dict[str, Any]:
    """给 `/雀魂查看` 或运维用的统计视图。"""
    data = _load()
    return {
        "accounts": len(data["baseline"]),
        "pushed_total": len(data["pushed"]),
        "last_pushed": max(data["pushed"].values()) if data["pushed"] else 0,
    }


async def sleep_forever() -> None:  # pragma: no cover - 占位，便于测试挂起
    await asyncio.sleep(3600)
