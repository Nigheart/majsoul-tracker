# -*- coding: utf-8 -*-
"""牌谱解析：`GameRecord` → 结构化对局结果 → 推送文案（纯函数，便于单测）。

牌谱二进制解析**复用开源实现**（[yf-dev/majsoul-record](https://github.com/yf-dev/majsoul-record)
的 `load_and_process_game_log` 思路，MIT）：`Wrapper` → `GameDetailRecords` →
`actions[].result`（新版）或 `records[]`（旧版）→ 每条再 `Wrapper` 解出 `.lq.<类名>`
→ `getattr(pb, 类名)` 动态反序列化。实测：227 action → 3 局 2 和牌。
"""
from __future__ import annotations

import re
import time
from typing import Any, Iterator

from . import majsoul_id, rank_level, yaku_map
from .ms import protocol_pb2 as pb
from .provider_protocol import GameRecord

PAIPU_URL = "https://game.maj-soul.com/1/?paipu={uuid}"

# 牌谱 uuid：`<YYMMDD>-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`（日期 6 位 + 5 段 uuid4）
PAIPU_UUID_RE = re.compile(
    r"\d{6}-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# 起始点数（雀魂固定：四麻 25000 / 三麻 35000）——把「终局得点」还原成「点数收支」的兜底基准
INIT_POINTS = {4: 25000, 3: 35000}


def init_point_of(players: list[dict]) -> int:
    """本局起始点数：**优先用「点数守恒」反推**（终局总和 ÷ 人数），回退标准值。

    ★ 2026-09-22：不能用牌谱 `detail_rule.init_point` —— 实测该字段恒为 0（雀魂未填）。
    """
    n = len(players)
    if n:
        total = sum(int(p.get("point") or 0) for p in players)
        if total > 0:
            guess = int(round(total / n / 1000.0)) * 1000
            if guess > 0:
                return guess
    return INIT_POINTS.get(n, 25000)


# ---------------------------------------------------------------- 牌谱遍历

def iter_actions(data: bytes) -> Iterator[tuple[str, Any]]:
    """遍历牌谱内的动作事件，产出 `(类名, 消息对象)`。"""
    if not data:
        return
    wrapper = pb.Wrapper()
    try:
        wrapper.ParseFromString(data)
        details = pb.GameDetailRecords()
        details.ParseFromString(wrapper.data)
    except Exception:  # noqa: BLE001 —— 牌谱格式异常时静默返回，不影响对局结果推送
        return

    if len(details.records) > 0:          # 旧格式
        items = list(details.records)
    else:                                  # 新格式：actions[].result
        items = [a.result for a in details.actions if len(a.result) > 0]

    for item in items:
        rw = pb.Wrapper()
        try:
            rw.ParseFromString(item)
        except Exception:  # noqa: BLE001
            continue
        cls_name = rw.name[4:] if rw.name.startswith(".lq.") else rw.name
        cls = getattr(pb, cls_name, None)
        if cls is None:
            continue
        obj = cls()
        try:
            obj.ParseFromString(rw.data)
        except Exception:  # noqa: BLE001
            continue
        yield cls_name, obj


def _extract_hules(data: bytes) -> tuple[list[dict], int]:
    """从牌谱提取和牌事件与局数。

    ★ 2026-09-26 血战规则兼容（线上事故）：`RecordHuleXueZhanMid` / `RecordHuleXueZhanEnd`
    的 `hules[]` 是 **`HuInfoXueZhanMid`**（不是 `HuleInfo`），字段只有
    `seat/hand_count/hand/ming/hu_tile/zimo/yiman/count/fans/fu/title_id` ——
    **没有 `point_sum` / `point_rong` / `point_zimo_*` / `dadian` / `liqi`**。
    原先 `int(hu.point_sum)` 硬取属性 → `AttributeError: point_sum` → 解析崩 → tick 崩 →
    `seen` 未落盘 → 每轮重试直至触发 `code=540`。**所有字段一律 `getattr` 兜底**。
    """
    hules: list[dict] = []
    rounds = 0
    dealer = -1
    for cls_name, obj in iter_actions(data):
        if cls_name == "RecordNewRound":
            rounds += 1
            # ★ 2026-10-06：本局亲家 seat（= `ju`，与 `paipu_tenhou` 同口径）。
            #   `hand_point` 判「亲家自摸」要用它——协议在亲家自摸时 `point_zimo_qin=0`，
            #   没有这个字段就会把自摸收入少算一份（用户报的「倍满 16000 显示 +8000」）。
            dealer = int(getattr(obj, "ju", -1))
        elif cls_name in ("RecordHule", "RecordHuleXueZhanMid", "RecordHuleXueZhanEnd"):
            for hu in obj.hules:
                fans = []
                for f in getattr(hu, "fans", []):
                    fans.append({"id": int(f.id), "val": int(f.val), "name": yaku_map.yaku_name(int(f.id))})
                hules.append({
                    "seat": int(hu.seat),
                    "zimo": bool(hu.zimo),
                    # ★ 2026-10-06：本局亲家 seat（`RecordNewRound.ju`；无新局事件时 -1）
                    "dealer": dealer,
                    "hu_tile": hu.hu_tile,
                    # ★ 血战规则的 `HuInfoXueZhanMid` 无 `point_sum` —— 必须 getattr 兜底
                    "point_sum": int(getattr(hu, "point_sum", 0) or 0),
                    # ★ 2026-09-21：番/符与点数构成留档（用户要求「详细到番/符/倍满」）
                    "count": int(getattr(hu, "count", 0) or 0),
                    "fu": int(getattr(hu, "fu", 0) or 0),
                    "dadian": int(getattr(hu, "dadian", 0) or 0),
                    "point_rong": int(getattr(hu, "point_rong", 0) or 0),
                    "point_zimo_qin": int(getattr(hu, "point_zimo_qin", 0) or 0),
                    "point_zimo_xian": int(getattr(hu, "point_zimo_xian", 0) or 0),
                    # ★ 2026-09-22：和牌牌型（协议直接给，无需逐动作重放）
                    "hand": [str(t) for t in getattr(hu, "hand", [])],
                    "ming": [str(m) for m in getattr(hu, "ming", [])],
                    "liqi": bool(getattr(hu, "liqi", False)),
                    "fans": fans,
                    # ★ 2026-09-26：本手结算收支（放铳归属要靠它，见 `parse_record` 的「最大放铳」）
                    "delta_scores": [int(x) for x in getattr(obj, "delta_scores", [])],
                })
    return hules, rounds


# ---------------------------------------------------------------- 和牌牌型

SUIT_ORDER = {"m": 0, "p": 1, "s": 2, "z": 3}


def tile_key(t: str) -> tuple:
    """牌排序键：万→筒→索→字；赤 5（`0m/0p/0s`）按 5 排。"""
    t = str(t)
    if len(t) < 2:
        return (9, 0)
    num, suit = t[:-1], t[-1]
    try:
        n = 5 if num == "0" else int(num)
    except ValueError:
        n = 0
    return (SUIT_ORDER.get(suit, 9), n)


MING_RE = re.compile(r"(\w+)\(([^)]*)\)")


def parse_ming(ming_list) -> list[dict]:
    """副露字符串 → 结构化：`['angang(3p,3p,3p,3p)']` → `[{'kind','tiles'}]`。

    `kind`：`chi`（吃）/ `peng`（碰）/ `gang`·`minggang`（明杠）/ `angang`（暗杠）。
    """
    out: list[dict] = []
    for s in (ming_list or []):
        m = MING_RE.match(str(s))
        if m:
            out.append({"kind": m.group(1),
                        "tiles": [t.strip() for t in m.group(2).split(",") if t.strip()]})
    return out


def build_best_hand(my_hules: list[dict], player_count: int = 4) -> dict | None:
    """从本场该玩家的所有和牌里挑「名义打点最高」的一手，补齐展示字段。

    返回结构可直接作为 `card.render_result` 的 `hand` 入参：
    `{hand, win_tile, mings, zimo, count, fu, point, rank_name, fans_txt, ...}`。
    """
    if not my_hules:
        return None
    best = dict(max(my_hules, key=lambda h: hand_point(h, player_count)))
    best["point"] = hand_point(best, player_count)
    best["win_tile"] = str(best.get("hu_tile") or "")
    best["hand"] = sorted([str(t) for t in (best.get("hand") or [])], key=tile_key)
    best["mings"] = parse_ming(best.get("ming"))
    best["rank_name"] = yaku_map.hand_rank_name(best.get("count", 0), best.get("fu", 0))
    best["fans_txt"] = _fmt_fans(best.get("fans"))
    return best


def extract_deal_ins(hules: list[dict], players: list[dict] | None = None) -> list[dict]:
    """从和牌列表提取**放铳记录**（纯函数，可离线单测）。

    ★ 口径（与铁律 19 的「放铳次数」同源）：**荣和**时 `delta_scores` 中**唯一负值且
    seat ≠ 和牌者**的座位就是放铳者（荣和付款者必为一人）。⚠️ 不能用
    `delta_scores[s] == -point_rong` —— 本场棒/供托（立直棒 1000、本场 300）会并入收支，
    实测 `point_rong=8000` 而放铳者实付 **-8600**。多负值（口径不成立）时**不归属**。
    ★ 2026-09-26 用户追加口径（原话）：「最大放铳**我也想要和最大一手一样详细的程度**」
    → 每条记录的结构与 `build_best_hand()` 对齐（`hand` / `mings` / `win_tile` /
    `rank_name` / `fans_txt`），卡片可直接复用「最大一手」的渲染（手牌图 + 番/符/役种）。
    """
    who = {int(p["seat"]): (p.get("nickname") or "") for p in (players or [])}
    out: list[dict] = []
    for h in hules:
        if h.get("zimo"):
            continue                                  # 自摸不是放铳
        ds = [int(x) for x in (h.get("delta_scores") or [])]
        losers = [s for s, v in enumerate(ds) if v < 0 and s != int(h["seat"])]
        if len(losers) != 1:
            continue
        s = losers[0]
        win_seat = int(h["seat"])
        f_list = list(h.get("fans") or [])
        out.append({
            "seat": s,
            "loss": int(ds[s]),                       # 负数（含本场棒/供托）
            "tile": str(h.get("hu_tile") or ""),      # 对方和的牌 = 我打出的那张
            "winner": win_seat,
            "winner_name": who.get(win_seat) or f"座位{win_seat}",
            "zimo": False,
            "count": int(h.get("count") or 0),
            "fu": int(h.get("fu") or 0),
            "fans": f_list,
            "hand": sorted([str(t) for t in (h.get("hand") or [])], key=tile_key),
            "mings": parse_ming(h.get("ming")),
            "win_tile": str(h.get("hu_tile") or ""),
            "rank_name": yaku_map.hand_rank_name(int(h.get("count") or 0),
                                                 int(h.get("fu") or 0)),
            "fans_txt": _fmt_fans(f_list),
            "point": abs(int(ds[s])),                 # 展示用正数（`loss` 保留负数）
        })
    return out


# ---------------------------------------------------------------- 结构化解析

def parse_record(rec: GameRecord, focus_account_id: int) -> dict[str, Any]:
    """把一条对局详情解析为结构化结果（含 focus 玩家的名次/得点/和牌役种）。"""
    # seat → 昵称/账号（head.accounts 才有身份信息，result.players 只有 seat）
    seat_meta = {a["seat"]: a for a in rec.accounts}
    focus_seat = next((a["seat"] for a in rec.accounts if a["account_id"] == int(focus_account_id)), None)

    players = []
    for p in rec.result_players:
        seat = p["seat"]
        meta = seat_meta.get(seat, {})
        players.append({
            "seat": seat,
            "account_id": meta.get("account_id", 0),
            "nickname": meta.get("nickname", ""),
            "point": p.get("part_point_1", 0),          # 终局得点
            # ★ 2026-09-22：牌谱的 `total_point` **不是点数变化**！实测 =（终局得点 − 起始点数）
            #   + 顺位点（ウマ：1位+15000/2位+5000/3位−5000/4位−15000），即「竞技分」，
            #   是 PT 的计算基础（四家合计恰为 0）。点数变化必须自己算：终局 − 起始。
            "arena_point": p.get("total_point", 0),
            "pt": p.get("grading_score", 0),
            "gold": p.get("gold", 0),
            # 牌谱 head.accounts 的段位快照（level=四麻 / level3=三麻）
            "level_id": meta.get("level_id", 0),
            "level_score": meta.get("level_score", 0),
            "level_id3": meta.get("level_id3", 0),
            "level_score3": meta.get("level_score3", 0),
        })
    # 名次：按最终得点降序（与雀魂服务端一致）
    for i, p in enumerate(sorted(players, key=lambda x: x["point"], reverse=True), start=1):
        p["rank"] = i
    init_point = init_point_of(players)          # ★ 起始点数（守恒反推，回退 25k/35k）
    for p in players:
        p["delta"] = int(p["point"]) - init_point

    me = next((p for p in players if p["seat"] == focus_seat), None)
    hules, rounds = _extract_hules(rec.data)
    my_hules = [h for h in hules if h["seat"] == focus_seat] if focus_seat is not None else []

    # ★ 2026-09-26 新增「最大放铳」（用户要求：「增加一个最大放铳吧」；细节口径见
    #   `extract_deal_ins` 的 docstring）。此处只负责挑出「我放铳额绝对值最大」的那一次。
    deal_ins = extract_deal_ins(hules, players)
    my_deal_ins = ([d for d in deal_ins if d["seat"] == focus_seat]
                   if focus_seat is not None else [])
    max_deal_in = max(my_deal_ins, key=lambda d: abs(int(d["loss"]))) if my_deal_ins else None

    # 击飞（Feature-04）：终局点数 < 0 者被击飞；最后一手和牌者视为「击飞者」
    negatives = [p for p in players if int(p["point"]) < 0]
    kicker_seat = int(hules[-1]["seat"]) if hules else 0
    kicker = next((p for p in players if p["seat"] == kicker_seat), None)
    kicked = bool(me is not None and int(me["point"]) < 0)
    kicked_targets: list[str] = []
    if (not kicked) and negatives and focus_seat is not None and hules \
            and int(hules[-1]["seat"]) == focus_seat:
        kicked_targets = [p["nickname"] or f"座位{p['seat']}"
                          for p in negatives if p["seat"] != focus_seat]
    flight = {
        "kicked": kicked,
        "kicker_seat": kicker_seat,
        "kicker_name": (kicker["nickname"] or "") if kicker else "",
        "kicked_targets": kicked_targets,
    }

    return {
        "uuid": rec.uuid,
        "start_time": rec.start_time,
        "end_time": rec.end_time,
        "category": rec.category,
        "mode": rec.mode,
        "mode_id": rec.mode_id,
        "room_id": rec.room_id,
        "place": yaku_map.place_full(rec.mode, rec.mode_id, rec.room_id),
        "seat": focus_seat,
        "rank": me["rank"] if me else 0,
        "point": me["point"] if me else 0,
        "delta": me["delta"] if me else 0,           # ★ 点数收支 = 终局 − 起始（非牌谱 total_point）
        "arena_point": me["arena_point"] if me else 0,  # 竞技分（含顺位点），排障用
        "init_point": init_point,
        "pt": me["pt"] if me else 0,
        "gold": me["gold"] if me else 0,
        # ★ 2026-09-21：人数兜底不能再用 `rec.category`（它是「对局类别」不是麻种）→ 走 mode 判定
        "player_count": len(players) or (3 if yaku_map.is_three_player_mode(rec.mode_id, rec.mode) else 4),
        # 段位快照（**局前**，来自牌谱 head.accounts；局后段位以 GameBrief 的 level 为准）
        "level_id": me["level_id"] if me else 0,
        "level_score": me["level_score"] if me else 0,
        "level_id3": me["level_id3"] if me else 0,
        "level_score3": me["level_score3"] if me else 0,
        "flight": flight,
        "players": players,
        "hules": hules,
        "my_hules": my_hules,
        # ★ 2026-09-26：我的放铳记录（全部）与「最大放铳」（绝对值最大的一次）
        "deal_ins": my_deal_ins,
        "max_deal_in": max_deal_in,
        "deal_in_count": len(my_deal_ins),
        # ★ 2026-09-24：带上牌谱原始字节，供 `player_stats_rows()` 提取全玩家统计
        #   （和牌/自摸/荣和/放铳/立直）；不需要重放牌谱。
        "data": rec.data,
        # ★ 2026-09-22：本场最大一手（含和牌全手牌 + 副露），供图片卡展示
        "best_hand": build_best_hand(my_hules, len(players) or 4),
        "rounds": rounds,
    }


# ---------------------------------------------------------------- 全玩家统计（2026-09-24）

HULE_CLASSES = ("RecordHule", "RecordHuleXueZhanMid", "RecordHuleXueZhanEnd")


def extract_player_stats(data: bytes) -> dict[str, dict[int, int]]:
    """从牌谱提取**所有 seat** 的统计：和牌/自摸/荣和/放铳/立直。

    ★ 2026-09-24（用户需求「包括对局玩家、事件、每个玩家的段位和点数变化，
      以及立直/荣和/自摸/放铳 的次数」）——口径均经真实牌谱实测确认：

    - **和牌/自摸**：`HuleInfo.zimo`（True=自摸，False=荣和）按 seat 计数；
    - **立直**：`RecordDiscardTile.is_liqi == True` 按 seat 计数。
      ⚠️ **不能**用 `RecordLiqi` 类——实测老格式牌谱 0 命中；`HuleInfo.liqi` 是
      「和牌时是否立直」布尔，**不是次数**。
    - **放铳**：荣和时 `RecordHule.delta_scores` 中**唯一负值且 seat ≠ 和牌者**的那个。
      ⚠️ **不能**用 `delta_scores[s] == -point_rong` 判定——**本场棒/供托**（立直棒 1000、
      本场 300）会并入收支，实测 `point_rong=8000` 但放铳者实付 **-8600**。荣和时付款者
      必为一人，故「唯一负值者」是稳的口径。守恒：放铳合计应等于荣和合计。

    返回 `{"agari": {seat: n}, "zimo": ..., "rong": ..., "shokou": ..., "liqi": ...}`。
    """
    agari: dict[int, int] = {}
    zimo: dict[int, int] = {}
    rong: dict[int, int] = {}
    shokou: dict[int, int] = {}
    liqi: dict[int, int] = {}

    def bump(d: dict[int, int], k: int) -> None:
        d[k] = d.get(k, 0) + 1

    for cls_name, obj in iter_actions(data):
        if cls_name == "RecordDiscardTile":
            if bool(getattr(obj, "is_liqi", False)):
                bump(liqi, int(getattr(obj, "seat", -1)))
        elif cls_name in HULE_CLASSES:
            ds = [int(x) for x in getattr(obj, "delta_scores", [])]
            for hu in obj.hules:
                seat = int(hu.seat)
                bump(agari, seat)
                if bool(hu.zimo):
                    bump(zimo, seat)
                else:
                    bump(rong, seat)
                    if ds:
                        losers = [s for s, v in enumerate(ds) if v < 0 and s != seat]
                        if len(losers) == 1:       # 荣和付款者必为一人
                            bump(shokou, losers[0])
    return {"agari": agari, "zimo": zimo, "rong": rong, "shokou": shokou, "liqi": liqi}


def player_stats_rows(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """把 `parse_record` 结果 + 牌谱统计合成**全玩家统计表行**（供 `card.render_result`）。

    ★ 段位：`head.accounts[]` 对**所有玩家**都给 `level_id`(四麻)/`level_id3`(三麻)，
    按麻种取（`player_count == 3` 用三麻版）——见铁律 11（`level.id` 前缀判麻种）。
    """
    stats = extract_player_stats(parsed.get("data") or b"") if parsed.get("data") is not None \
        else parsed.get("_stats") or {}
    three = int(parsed.get("player_count") or 4) == 3
    rows: list[dict[str, Any]] = []
    for p in sorted(parsed.get("players") or [], key=lambda x: int(x.get("rank") or 9)):
        s = int(p.get("seat") or 0)
        lv = int(p.get("level_id3") or 0) if three else int(p.get("level_id") or 0)
        if not lv:                                  # 兜底：另一麻种字段也试一次
            lv = int(p.get("level_id3") or p.get("level_id") or 0)
        rows.append({
            "rank": int(p.get("rank") or 0),
            "seat": s,
            "account_id": int(p.get("account_id") or 0),
            "name": p.get("nickname") or f"座位{s}",
            "point": int(p.get("point") or 0),
            "delta": int(p.get("delta") or 0),
            "pt": int(p.get("pt") or 0),
            "level_id": lv,
            "level_name": rank_level.level_name(lv) if lv else "",
            "agari": stats.get("agari", {}).get(s, 0),
            "liqi": stats.get("liqi", {}).get(s, 0),
            "rong": stats.get("rong", {}).get(s, 0),
            "zimo": stats.get("zimo", {}).get(s, 0),
            "shokou": stats.get("shokou", {}).get(s, 0),
        })
    return rows


# ---------------------------------------------------------------- 文案

def _fmt_delta(v: int) -> str:
    return f"+{v}" if v > 0 else str(v)


# 宝牌类役种（`val` = 张数；其余役种 `val` = 番数）——2026-09-21
# ★ 名称对齐雀魂中文客户端（用户截图：32=「红宝牌」、34=「拔北宝牌」）
BONUS_YAKUS = ("宝牌", "红宝牌", "里宝牌", "拔北宝牌")


def fmt_tile(tile: Any) -> str:
    """雀魂牌编码 → 可读牌名：`0m/0p/0s` 是**赤 5**（如 `0s` → `赤5s`）。"""
    t = str(tile or "").strip()
    if not t:
        return "-"
    if len(t) == 2 and t[0] == "0":
        return f"赤5{t[1]}"
    return t


def _fmt_fans(fans: list[dict], limit: int = 0) -> str:
    """役种列表 → 「立直·宝牌×2」（★ 数量取自 `val`，`val=0` 的条目不显示）。

    ★ 2026-09-21 修复「最大一手数据错误」：旧实现按**条目个数**累计、完全忽略 `val`，于是
      ①「宝牌 val=4」显示成「宝牌」（张数丢失）；②雀魂对**未成立**的宝牌也会下发条目
      （如没有里宝牌时的「里宝牌」`val=0`），旧实现把它照样列出来 → 役种与实际不符。
    现在的口径：宝牌类 → `名称×张数`；普通役 → 只列名称（`val` 是番数，写「×N」易被误读成多个役）。

    `limit > 0` 时最多列 `limit` 个役（文案用，超出的补「等」）——避免牌谱明细文本超长。
    """
    normal: list[str] = []
    bonus: list[str] = []
    for f in fans or []:
        name = str(f.get("name") or "")
        n = int(f.get("val") or 0)
        if not name or n <= 0:
            continue                     # val=0 → 该役实际未成立（如无里宝牌），不显示
        if name in BONUS_YAKUS:
            bonus.append(f"{name}×{n}" if n > 1 else name)
        else:
            normal.append(name)
    parts = normal + bonus
    if limit > 0 and len(parts) > limit:
        parts = parts[:limit] + ["等"]
    return "·".join(parts) if parts else "无役（流局/无和牌）"


def hand_point(h: dict[str, Any], player_count: int = 4) -> int:
    """该手牌的**名义打点**（与游戏内结算画面/牌谱一致）。

    ★ 2026-09-21 修复（用户提供游戏内截图对照）：**不能直接取 `point_sum`** —— 荣和时它与
    实际得点不符（实证：荣和满贯游戏内显示「8000 点」，我们却读成 20000，且因此把"最大一手"
    选成了这手 8000 的牌，漏掉真正最大的 12000 倍满）。改用支付字段自行求和：
      - 荣和：`point_rong`（放铳者支付额）
      - 自摸：见下（2026-10-06 修正）
    两者都取不到时才回退 `point_sum`。

    ★ 2026-10-06 修复「**亲家自摸少算一份**」（用户报：三麻亲家自摸倍满 16000，卡片显示 +8000）：
    协议在**亲家自摸**时 `point_zimo_qin = 0`（自己就是亲家 → 没有「亲家支付」这一项），
    旧公式 `qin + xian × (人数−2)` 于是只数到「其他闲家」，正好少一份。正确口径：
      - **亲家自摸**：`xian × (人数−1)`（四麻 4000×3=12000；三麻 8000×2=**16000**）
      - **子家自摸**：`qin + xian × (人数−2)`（四麻三麻均如此，三麻天然带「自摸损」）
    亲家判定用 `h["dealer"]`（`_extract_hules` 从 `RecordNewRound.ju` 写入）；字段缺失
    （血战/老数据）时兜底启发式「`qin == 0` 且 `xian > 0` = 亲家自摸」——协议实证如此。
    实证：本地全部样本 **18 手自摸零反例**（`_probe/diag_zimo_samples.py`），其中 9 手亲家
    自摸正是旧公式减半的那些。
    """
    n = int(player_count or 4)
    if h.get("zimo"):
        qin = int(h.get("point_zimo_qin") or 0)
        xian = int(h.get("point_zimo_xian") or 0)
        raw_dealer = h.get("dealer")
        dealer = int(raw_dealer) if raw_dealer is not None else -1
        if dealer >= 0:
            is_dealer = dealer == int(h.get("seat", -2))
        else:
            is_dealer = (qin == 0 and xian > 0)      # 兜底：亲家自摸协议不给 qin
        total = (xian * max(0, n - 1)) if is_dealer else (qin + xian * max(0, n - 2))
        return total or int(h.get("point_sum") or 0)
    rong = int(h.get("point_rong") or 0)
    return rong or int(h.get("point_sum") or 0)


def format_result(parsed: dict[str, Any], display_name: str,
                  with_paipu: bool = True,
                  comments: list[str] | None = None,
                  compact: bool = False) -> str:
    """生成群友友好的对局结果文案（≤500 字）；`comments` = Feature-04 增强评论行。

    ★ 2026-09-24 用户口径：「有图片的话，文字说明就简化一下吧」——`compact=True` 时输出
    **精简版**：只保留「谁 · 在哪 · 什么结果」一行 + 牌谱链接，明细全部交给图片卡
    （图片卡已含最大一手全手牌、段位进度条、全玩家统计表）。调用方在有图时用精简版，
    无图（渲染/发图失败回退）时用完整版——**文字信息量随图的有无自适应**。
    """
    if compact:
        lines = ["🀄 雀魂对局结果"]
        lines.append(f"群友「{display_name}」在 {parsed['place']} 打完一场："
                     f"#{parsed['rank']} · {parsed['point']}"
                     f"（{_fmt_delta(parsed['delta'])}) · PT {_fmt_delta(parsed['pt'])}")
        if comments:
            lines.extend(comments)
        if with_paipu:
            lines.append(f"牌谱：{PAIPU_URL.format(uuid=parsed['uuid'])}")
        return "\n".join(lines)

    lines = ["🀄 雀魂对局结果"]
    lines.append(f"群友「{display_name}」在 {parsed['place']} 打完一场：")
    lines.append(f"名次 #{parsed['rank']} · 得点 {parsed['point']}（{_fmt_delta(parsed['delta'])}）"
                 f" · PT {_fmt_delta(parsed['pt'])}")

    my_hules = parsed.get("my_hules") or []
    # ★ 2026-09-22：统一走 `best_hand`（按名义打点挑最大一手，含和牌手牌/副露字段）
    best = parsed.get("best_hand")
    if best is None and my_hules:
        best = build_best_hand(my_hules, int(parsed.get("player_count") or 4))
    if best:
        how = "自摸" if best.get("zimo") else "荣和"
        lines.append(f"和牌 {len(my_hules)} 次，最大一手：{how} {fmt_tile(best.get('win_tile'))} "
                     f"+{int(best.get('point') or 0)}")
        # ★ 2026-09-21（用户要求）：明细行给「番/符/档次｜每个役的番数或张数」
        head = " ".join(x for x in (
            f"{best.get('count') or 0} 番",
            f"{best.get('fu') or 0} 符",
            best.get("rank_name")
            or yaku_map.hand_rank_name(best.get("count", 0), best.get("fu", 0))) if x)
        fans_txt = best.get("fans_txt") or _fmt_fans(best.get("fans") or [])
        lines.append(f"　{head}｜{fans_txt}" if head else f"　{fans_txt}")
    else:
        lines.append("本场未和牌")

    # ★ 2026-09-26 新增：最大放铳（**与「最大一手」同详细度**：点数 + 番/符/役种；
    #   ★ 仅完整版文字，`compact` 版交给图）
    if parsed.get("data") is not None or parsed.get("players"):
        deal = parsed.get("max_deal_in")
        if deal:
            who = str(deal.get("winner_name") or "")
            dhow = (f"被 {who} 荣和 {deal.get('tile') or '?'}" if who
                    else f"放铳 {deal.get('tile') or '?'}")
            lines.append(f"放铳 {len(parsed.get('deal_ins') or [])} 次，最大放铳："
                         f"{dhow} {int(deal.get('loss') or 0)}")
            dhead = " ".join(x for x in (
                f"{deal.get('count') or 0} 番",
                f"{deal.get('fu') or 0} 符",
                deal.get("rank_name")
                or yaku_map.hand_rank_name(deal.get("count", 0), deal.get("fu", 0))) if x)
            dfans = deal.get("fans_txt") or _fmt_fans(deal.get("fans") or [])
            lines.append(f"　{dhead}｜{dfans}" if dhead else f"　{dfans}")
        else:
            lines.append("本场未放铳")

    # ★ 2026-09-24：全玩家统计（图文关系＝「图 + 完整原文」，故文本也带一张紧凑表）
    if parsed.get("data") is not None or parsed.get("players"):
        rows = player_stats_rows(parsed)
        if len(rows) > 1:
            lines.append("全局统计（立直/荣和/自摸/放铳）：")
            for r in rows:
                nm = r["name"] or f"座位{r['seat']}"
                lines.append(f"　#{r['rank']} {nm} {_fmt_delta(r['delta'])} "
                             f"PT {_fmt_delta(r['pt'])}｜立{r['liqi']} 荣{r['rong']} "
                             f"自{r['zimo']} 铳{r['shokou']}")

    if comments:
        lines.extend(comments)

    if with_paipu:
        lines.append(f"牌谱：{PAIPU_URL.format(uuid=parsed['uuid'])}")
    return "\n".join(lines)


def brief_place(brief: Any) -> str:
    """对局摘要 → 场次名（★ 实测摘要的 `sub_tag` 就等于 `meta.mode_id`）。"""
    return (yaku_map.mode_id_name(int(getattr(brief, "sub_tag", 0) or 0))
            or yaku_map.tag_name(int(getattr(brief, "tag", 0) or 0)))


def format_brief(brief: Any, display_name: str, focus_account_id: int = 0,
                 with_paipu: bool = True,
                 comments: list[str] | None = None) -> str:
    """降级文案：牌谱接口不可用（如风控 `code=540`）时，仅用对局摘要生成结果。

    只依赖 `GameBrief`（名次/得点/PT），不含和牌役种明细。
    ★ 2026-09-21：对局摘要的 `sub_tag` **实测就等于 `meta.mode_id`**（同一局牌谱 mode_id=2 → 摘要
    sub_tag=2），故降级路径也能给出精确场次（「玉之间·四人南」），不再退化成「三麻·段位场」。
    """
    place = brief_place(brief)
    me = next((p for p in (brief.players or [])
               if int(getattr(p, "account_id", 0)) == int(focus_account_id or 0)), None)
    rank = getattr(me, "rank", 0) or brief.rank
    point = getattr(me, "final_point", 0) or brief.final_point
    pt = int(getattr(me, "grading_score", 0)) if me else 0
    lines = ["🀄 雀魂对局结果"]
    lines.append(f"群友「{display_name}」在 {place or '对局'} 打完一场：")
    lines.append(f"名次 #{rank} · 得点 {point} · PT {_fmt_delta(pt)}")
    lines.append("（牌谱明细暂不可用）")
    if comments:
        lines.extend(comments)
    if with_paipu:
        lines.append(f"牌谱：{PAIPU_URL.format(uuid=brief.uuid)}")
    return "\n".join(lines)


def format_room_battle(parsed: dict[str, Any], names: dict[int, str],
                       with_paipu: bool = True) -> str:
    """同室对战战报（赛后兜底）：多个绑定群友在同一自定义房的一局。"""
    lines = ["🀄 雀魂 · 同室对战战报"]
    room = parsed.get("room_id") or 0
    who = "、".join(f"「{names.get(p['account_id'], p['nickname'] or '群友')}」"
                   for p in parsed["players"] if p["account_id"] in names)
    lines.append(f"{who} 在房间 {room} 决一死战！")
    for p in sorted(parsed["players"], key=lambda x: x["rank"]):
        nm = names.get(p["account_id"], p["nickname"] or f"座位{p['seat']}")
        mark = "👑 " if p["rank"] == 1 else "　 "
        lines.append(f"{mark}#{p['rank']} 「{nm}」{p['point']}（{_fmt_delta(p['delta'])}）PT {_fmt_delta(p['pt'])}")
    if with_paipu:
        lines.append(f"牌谱：{PAIPU_URL.format(uuid=parsed['uuid'])}")
    return "\n".join(lines)


def fmt_time(ts: int) -> str:
    """时间戳 → `MM-DD HH:MM`。"""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:  # noqa: BLE001
        return "-"


# ================================================================ 牌谱链接查询（Feature-07，2026-09-28）
#
# 用户需求（原话）：「玩家给出一个牌谱链接，回复这局类似于对局明细的信息。」
# 与「自动推送」的区别：**用户主动给链接**，所以整局的人是别人也照查（实测测试小号
# 拉到了自己没参与的牌谱，0.6s，`fetchGameRecord` 对任意牌谱公开可用）。


def paipu_url_of(uuid: str, match_id: int = 0) -> str:
    """牌谱分享链接；给了 `match_id` 就**保留 `_a<match_id>` 尾号**（纯函数）。

    ★ 2026-09-28：尾号不是装饰——雀魂客户端「分享牌谱」生成的链接**恒带** `_a<match_id>`，
      用户在浏览器里打开时客户端会用它定位分享者/回放入口。所以「用户给我们一条链接」
      的场景要把原尾号**原样还回去**；自动推送（uuid 来自对局摘要，没有尾号）维持旧格式。
    """
    url = PAIPU_URL.format(uuid=uuid)
    return f"{url}_a{int(match_id)}" if match_id else url


def parse_paipu_link(text: str) -> dict[str, Any] | None:
    """从「牌谱链接 / 裸 uuid / uuid_a尾号」解析出结构化信息（纯函数）。

    返回 `{"uuid", "match_id", "sharer_account_id", "kind", "raw"}`，解析失败返回 `None`。

    ★ 实测（用一条真实的分享链接验证）：
      `https://game.maj-soul.com/1/?paipu=<uuid>_a<match_id>`
      → uuid 是牌谱标识，末尾 `_a<match_id>` 就是 **match_id**，
        用 `majsoul_id.mat2account(<match_id>)` 可反推出**分享者的 account_id**，
        与牌谱里该 seat 上的玩家一致 —— 所以**链接尾号能直接告诉我们「这局牌谱属于谁」**，
        用它当明细的「分享者/主角」，比猜更可靠（算法来源见 `majsoul_id` 的 docstring）。
    尾号缺失（裸 uuid）时不猜主角，由调用方兜底（如改用「查询者自己已绑定的账号」）。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    m = PAIPU_UUID_RE.search(raw)
    if not m:
        return None
    uuid = m.group(0)
    kind = "url" if ("://" in raw or "paipu=" in raw) else "uuid"
    tail = re.match(r"_a(\d+)", raw[m.end():])
    match_id = int(tail.group(1)) if tail else 0
    sharer = 0
    if match_id > 0:
        guess = majsoul_id.mat2account(match_id)
        # ★ 往返校验：`mat2account` 带浮点除法，必须能反算回同一个 match_id 才可信
        if 0 < guess <= majsoul_id.MAX_ACCOUNT_ID and majsoul_id.acc2match(guess) == match_id:
            sharer = int(guess)
    return {"uuid": uuid, "match_id": match_id, "sharer_account_id": sharer,
            "kind": kind, "raw": raw}


def _fmt_duration(sec: int) -> str:
    """秒 → 「20 分 44 秒」/「1 小时 3 分」。"""
    sec = max(0, int(sec or 0))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return f"{h} 小时 {m} 分"
    return f"{m} 分 {s} 秒" if m else f"{s} 秒"


def _hand_line(tag: str, hand: dict[str, Any] | None, *, fans_limit: int = 5) -> str:
    """一手牌 → 「自摸 7z +12000（7番40符 跳满｜役种）」；`hand=None` → 缺失态文案。"""
    if not hand:
        return ""
    how = "自摸" if hand.get("zimo") else "荣和"
    pts = int(hand.get("point") or 0)
    mid = " ".join(x for x in (f"{hand.get('count') or 0} 番", f"{hand.get('fu') or 0} 符",
                               hand.get("rank_name") or "") if x)
    fans = hand.get("fans_txt") or _fmt_fans(hand.get("fans") or [], limit=fans_limit)
    body = f"{how} {fmt_tile(hand.get('win_tile'))} +{pts}"
    if pts <= 0:
        body = f"{how} {fmt_tile(hand.get('win_tile'))}"
    detail = f"{mid}｜{fans}" if mid else fans
    return f"{tag} {body}（{detail}）"


def _deal_line(deal: dict[str, Any] | None, *, fans_limit: int = 5) -> str:
    """一次放铳 → 「被 XXX 荣和 2m -8000（4番30符｜役种）」；`deal=None` → 缺失态文案。"""
    if not deal:
        return ""
    who = str(deal.get("winner_name") or "")
    tile = fmt_tile(deal.get("tile"))
    loss = int(deal.get("loss") or 0)
    body = f"被 {who} 荣和 {tile} {loss}" if who else f"放铳 {tile} {loss}"
    mid = " ".join(x for x in (f"{deal.get('count') or 0} 番", f"{deal.get('fu') or 0} 符",
                               deal.get("rank_name") or "") if x)
    fans = deal.get("fans_txt") or _fmt_fans(deal.get("fans") or [], limit=fans_limit)
    detail = f"{mid}｜{fans}" if mid else fans
    return f"最大放铳 {body}（{detail}）"


def format_paipu_detail(detail: dict[str, Any], *, compact: bool = False,
                        with_paipu: bool = True) -> str:
    """牌谱明细文案（≤ 700 字）；`compact=True` = **有图时的精简版**（明细交给图片卡）。

    ★ 2026-09-24 用户口径「有图片的话，文字说明就简化一下吧」的延续：图已承载的信息
      文字不重复；但**出图失败时文本恢复完整版**，绝不图文双失（铁律 14）。
    """
    place = str(detail.get("place") or "对局")
    # 链接优先用 detail 里的（可能带 `_a<match_id>` 尾号，见 paipu_url_of）
    url = detail.get("paipu_url") or paipu_url_of(str(detail.get("uuid") or ""),
                                                  int(detail.get("match_id") or 0))
    st, et = int(detail.get("start_time") or 0), int(detail.get("end_time") or 0)
    period = fmt_time(st)
    if et:
        period += "→" + fmt_time(et)[-5:]
    meta = (f"{place}｜{period}（{_fmt_duration(et - st)}）｜"
            f"{int(detail.get('rounds') or 0)} 局｜{int(detail.get('player_count') or 0)} 人")

    sharer = detail.get("sharer") or None
    label = "分享者" if detail.get("sharer_source") == "link" else "本人"
    lines = ["🀄 雀魂牌谱明细", meta]
    if sharer:
        lv = f" · {sharer['level_name']}" if sharer.get("level_name") else ""
        lines.append(f"{label}「{sharer.get('name') or '?'}」：#{sharer.get('rank')} · "
                     f"{sharer.get('point')}（{_fmt_delta(sharer.get('delta'))}）"
                     f" · PT {_fmt_delta(sharer.get('pt'))}{lv}")
    if compact:
        if with_paipu:
            lines.append(f"牌谱：{url}")
        return "\n".join(lines)

    if sharer:
        lines.append(_hand_line("最大一手：", sharer.get("best_hand")) or "最大一手：本场未和牌")
        lines.append(_deal_line(sharer.get("max_deal_in")) or "最大放铳：本场未放铳")

    rows = detail.get("players") or []
    if rows:
        lines.append("全玩家统计（立直/荣和/自摸/放铳）：")
        for r in rows:
            nm = r.get("name") or f"座位{r.get('seat')}"
            lines.append(f"　#{r.get('rank')} {nm} {r.get('point')}"
                         f"（{_fmt_delta(r.get('delta'))}）PT {_fmt_delta(r.get('pt'))}"
                         f"｜立{r.get('liqi')} 荣{r.get('rong')} 自{r.get('zimo')} 铳{r.get('shokou')}")

    # 其他玩家按顺位排序（`hands` 按座位序给出，直接遍历会得到 #2 #4 #1 这种乱序）
    others = sorted((h for h in (detail.get("hands") or []) if not h.get("is_sharer")),
                    key=lambda h: int(h.get("rank") or 99))
    if others:
        lines.append("其他玩家：")
        for h in others:
            nm = h.get("name") or f"座位{h.get('seat')}"
            one = _hand_line("最大一手", h.get("best")) or "本场未和牌"
            two = _deal_line(h.get("deal"))
            seg = one + ("；" + two if two else "；本场未放铳" if h.get("best") else "")
            lines.append(f"　#{h.get('rank')} {nm} {seg}")

    if with_paipu:
        lines.append(f"牌谱：{url}")
    return "\n".join(lines)
