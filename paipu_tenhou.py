# -*- coding: utf-8 -*-
"""雀魂牌谱（`GameRecord`）→ **tenhou.net/6 JSON**（mjai-reviewer / akochan 唯一接受的输入格式）。

Feature-08「麻将 AI 分析」的**第一半**：只有把雀魂牌谱转成 tenhou6，才能喂给
`mjai-reviewer -e akochan`。本模块是**纯函数**（不依赖 nonebot / 网络 / 文件系统），
输入 `provider_protocol.GameRecord`，输出可直接 `json.dumps` 的 dict。

★ 目标格式的权威语义（改本文件前务必先读它们）
  · `mjai-reviewer/convlog/src/tenhou/json_scheme.rs` —— 字段结构（`RawKyoku` 是 **17 元组**）；
  · `mjai-reviewer/convlog/src/tenhou/log.rs` —— 每局如何被消费（`results` 的交替结构）；
  · `mjai-reviewer/convlog/src/tenhou/tile.rs` —— tile 编码表；
  · `mjai-reviewer/convlog/src/conv.rs` —— tenhou6 → mjai 的**逐步状态机**，本模块的
    `takes`/`discards` 顺序、副露字符串的「标记字符位置」语义全部按它对齐；
  · `convlog/tests/testdata/*.json` —— 官方样本（吃碰杠/双响/宝牌/流局）。
  参考实现：tensoul `convert.js`（雀魂 → tenhou6，本模块的骨架来源）。

★ 格式速查
  · 每局 17 元组：
    ``[meta, scoreboard, dora, ura, haipai0, takes0, discards0, ... haipai3, takes3,
    discards3, results]``；``meta = [4*chang + ju, ben, liqibang]``。
  · tile 是 **2 位数字**：11-19 万 / 21-29 筒 / 31-39 索 / 41-47 東南西北白發中 /
    51-53 赤5万筒索；``60`` = 摸切标记。
  · ``takes`` = 摸牌（数字）或副露自身（字符串）；``discards`` = 打牌（数字或 60）、
    立直 ``"r<XX>"``、暗槓 ``"ccccccaXX"``、加槓 ``"cccckXXXX"``、**大明槓占位 ``0``**。
  · 副露字符串的标记字符位置编码「从哪家来」：碰 ``p`` 在 0/2/4，大明槓 ``m`` 在 0/2/6
    （0=上家 / 1=対面 / 2=下家，即字符位 0/2/4 与 0/2/6）；吃 ``c`` 恒在 0（只可能吃上家）；
    暗槓 ``a`` 恒在 6；加槓 ``k`` 同碰。位置与「谁打的」的对应见 ``_rel_index``。
  · ``results[0] == "和了"`` 时，其后是 ``deltas, [who, target, who, 点数文案, 役…]`` 交替；
    否则 ``results[1]`` 是该局收支数组。

★ 不支持（显式抛 ``UnsupportedRecordError``，绝不产出错误数据）
  三麻（`mode` 11/12 或 `scores` 非 4 家）、活动/大会局（血战规则 `RecordHuleXueZhan*`）、
  拔北（`RecordBaBei`）。
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable

from . import yaku_map
from .paipu_parser import iter_actions
from .provider_protocol import GameRecord

__all__ = [
    "Tenhou6Error",
    "UnsupportedRecordError",
    "RecordStructureError",
    "to_tenhou6",
    "dumps_tenhou6",
    "tile_to_tenhou",
    "tenhou_to_tile",
    "seat_of_account",
    "TSUMOGIRI",
]

# ---------------------------------------------------------------- 常量

TSUMOGIRI = 60                    # tenhou6 的摸切标记（conv.rs：`ActionItem::Tsumogiri`）
SUIT_NUM = {"m": 1, "p": 2, "s": 3, "z": 4}
NUM_SUIT = {v: k for k, v in SUIT_NUM.items()}
AKA_BASE = {1: 51, 2: 52, 3: 53}  # 赤 5m / 5p / 5s

# 一局 17 元组的下标（可读性用）
_K_META, _K_SCORE, _K_DORA, _K_URA = 0, 1, 2, 3
_K_ACTIONS_FROM = 4               # 每个玩家 3 个槽：haipai / takes / discards

# 事件类名白名单（`paipu_parser.iter_actions` 会 yield 的牌谱内动作）
_KNOWN_EVENTS = frozenset({
    "RecordNewRound", "RecordDealTile", "RecordDiscardTile", "RecordChiPengGang",
    "RecordAnGangAddGang", "RecordHule", "RecordNoTile", "RecordLiuJu",
})
# 明确不支持（血战/活动规则、三麻拔北）
_UNSUPPORTED_EVENTS = frozenset({
    "RecordHuleXueZhanMid", "RecordHuleXueZhanEnd", "RecordBaBei",
    "RecordHuleXueZhan", "RecordHuleXueZhanEndMid",
})

# 途中流局类型（雀魂 `RecordLiuJu.type`）→ tenhou6 的 results[0] 文案。
# conv.rs 只判断 results[0] 是否等于「和了」，其余一律按流局取 results[1] 收支；
# 这里的文案因此只作展示，但仍保持与 tenhou.net/6 官方样本一致。
_LIUJU_NAMES = {
    1: "九種九牌",
    2: "四風連打",
    3: "四家立直",
    4: "四開槓",
    5: "三家和了",
}


class Tenhou6Error(Exception):
    """转换失败基类（调用方应把它当「本局无法分析」处理，不要当成程序 bug）。"""


class UnsupportedRecordError(Tenhou6Error):
    """牌谱合法但本转换器不支持（三麻 / 活动局血战规则 / 拔北）。"""


class RecordStructureError(Tenhou6Error):
    """牌谱结构与预期不符（缺字段、家数不对、宝牌指示牌数量异常等）。"""


# ---------------------------------------------------------------- 牌编码

def tile_to_tenhou(tile: Any) -> int:
    """雀魂牌串（``"1m"``/``"0p"``/``"5z"``）→ tenhou6 数字编码。

    ``0m/0p/0s`` 是赤 5（→ 51/52/53），其余 ``<数字><花色>``；``z`` 是字牌 1-7。
    非法输入抛 `RecordStructureError`（不静默产出错牌）。
    """
    t = str(tile or "").strip()
    # ★ 雀魂牌串**恒为 2 字符**（`1m`/`0p`/`5z`）——必须卡死长度，否则 `"10m"` 会被
    #   误读成 1m（t[0]=='1' + t[-1]=='m'），静默产出错牌。
    if len(t) != 2:
        raise RecordStructureError(f"无法识别的牌：{tile!r}")
    num_ch, suit = t[0], t[1].lower()
    suit_n = SUIT_NUM.get(suit)
    if suit_n is None or not num_ch.isdigit():
        raise RecordStructureError(f"无法识别的牌：{tile!r}")
    num = int(num_ch)
    if num == 0:
        # 赤 5：只有 m/p/s 有；`0z` 是活动规则的「赤白」，本转换器不支持
        aka = AKA_BASE.get(suit_n)
        if aka is None:
            raise UnsupportedRecordError(f"不支持的赤牌：{tile!r}")
        return aka
    if 1 <= num <= 9:
        return 10 * suit_n + num
    raise RecordStructureError(f"无法识别的牌：{tile!r}")


def tenhou_to_tile(code: int) -> str:
    """tenhou6 数字编码 → 雀魂牌串（``51``→``"0m"``）。``60``/``0`` 无对应牌，抛异常。"""
    c = int(code)
    if c == TSUMOGIRI:
        raise RecordStructureError("60 是摸切标记，不是牌")
    if c == 0:
        raise RecordStructureError("0 是大明槓占位，不是牌")
    for suit_n, aka in AKA_BASE.items():
        if c == aka:
            return "0" + NUM_SUIT[suit_n]
    suit_n, num = divmod(c, 10)
    if suit_n in NUM_SUIT and 1 <= num <= 9:
        return f"{num}{NUM_SUIT[suit_n]}"
    raise RecordStructureError(f"无法识别的 tenhou 编码：{code}")


def _deaka(code: int) -> int:
    """赤 5 → 普通 5 的 tenhou 编码（``51``→``15``，与 `convert.js` 的 `deaka` 同口径）。"""
    c = int(code)
    if 51 <= c <= 53:
        return 10 * (c % 10) + 5
    return c


def _as_int_list(values: Iterable[Any]) -> list[int]:
    return [int(v) for v in values]


def _pon_base_code(naki: str) -> int | None:
    """从碰/加槓的 naki 串里取出「被碰的那张」的 tenhou 编码。

    ★ 标记字符的位置是变化的（碰 ``p`` 在 0/2/4、加槓 ``k`` 也是），**不能假设在开头**。
    """
    for ch in ("p", "k"):
        i = naki.find(ch)
        if i >= 0 and len(naki) >= i + 3:
            seg = naki[i + 1:i + 3]
            if seg.isdigit():
                return int(seg)
    return None


# ---------------------------------------------------------------- 单局构建

class _KyokuBuilder:
    """一局（`RecordNewRound` … 和了/流局）的 tenhou6 17 元组构建器。

    ★ 核心不变量：``takes`` / ``discards`` 必须严格按**真实发生顺序**追加。
      `conv.rs` 是一个「按 take 前进、用 discard 配对」的状态机，顺序错一点就会
      报 `UnexpectedNaki` / `InsufficientTakes`，或产出错的动作流。
    """

    def __init__(self, nr, player_count: int = 4):
        self.n = player_count
        chang, ju = int(nr.chang), int(nr.ju)
        self.meta = [4 * chang + ju, int(nr.ben), int(nr.liqibang)]
        self.scores = _as_int_list(nr.scores)
        self.doras = self._read_doras(nr)
        self.ura: list[int] = []
        self.dealer = int(nr.ju)

        self.haipai: list[list[int]] = [[] for _ in range(player_count)]
        self.takes: list[list[Any]] = [[] for _ in range(player_count)]
        self.discards: list[list[Any]] = [[] for _ in range(player_count)]
        # 运行时状态（不进入 tenhou6，供暗槓/加槓判断用）
        self.hands: list[list[int]] = [[] for _ in range(player_count)]
        self.pons: list[list[str]] = [[] for _ in range(player_count)]

        self.ldseat = -1          # 最近一次「打牌」的座位（荣和的放铳者由它决定）
        self.nriichi = 0          # 本局立直宣言数（立直棒结算用）
        self.popped: int | None = None   # 庄家开局「第 14 张」（视为已摸牌）

        for seat in range(player_count):
            tiles = [str(t) for t in getattr(nr, f"tiles{seat}", [])]
            if len(tiles) < 13:
                raise RecordStructureError(
                    f"第 {seat} 家开局手牌只有 {len(tiles)} 张（应为 13/14）")
            codes = [tile_to_tenhou(t) for t in tiles]
            self.haipai[seat] = codes[:13]
            self.hands[seat] = list(codes)
            if seat == self.dealer and len(codes) > 13:
                # ★ 雀魂把庄家「配牌 13 张 + 第 1 巡摸牌」一起给了（共 14 张），
                #   第 14 张按摸牌处理（与 `convert.js` 的 `poppedtile` 同口径）。
                self.popped = codes[13]
                self.takes[seat].append(self.popped)

    # -------------------------------------------------- 宝牌指示牌

    @staticmethod
    def _read_doras(msg) -> list[int]:
        doras = [str(t) for t in getattr(msg, "doras", [])]
        if not doras:
            single = str(getattr(msg, "dora", "") or "")
            if single:
                doras = [single]
        return [tile_to_tenhou(t) for t in doras]

    def _merge_doras(self, msg) -> None:
        """杠后新宝牌：雀魂把「全部指示牌」累积给出，长度变长时整体替换。

        ★ `conv.rs` 用 `dora_indicators` 的顺序逐个消费（开局 1 张 → 每次杠 1 张），
          所以顺序必须与真实翻牌顺序一致——雀魂的累计数组正好如此。
        """
        new = self._read_doras(msg)
        if len(new) > len(self.doras):
            if new[:len(self.doras)] != self.doras:
                raise RecordStructureError("宝牌指示牌序列不是前缀累积，无法对齐")
            self.doras = new

    # -------------------------------------------------- 手牌维护

    def _drop_hand(self, seat: int, code: int) -> None:
        try:
            self.hands[seat].remove(code)
        except ValueError:
            # 手牌推演失败不致命（暗槓挑牌才真正需要手牌）；退化为「不记录」
            pass

    # -------------------------------------------------- 事件

    def on_deal(self, d) -> None:
        seat = int(d.seat)
        tile = tile_to_tenhou(d.tile)
        self.takes[seat].append(tile)
        self.hands[seat].append(tile)
        self._merge_doras(d)

    def on_discard(self, d) -> None:
        seat = int(d.seat)
        tile_ch = str(d.tile)
        code = tile_to_tenhou(tile_ch)

        sym: Any = TSUMOGIRI if bool(d.moqie) else code
        # ★ 庄家开局第 1 打：雀魂不给 moqie（因为第 14 张不是 DealTile 给的），
        #   这里用「打的正是那第 14 张」补判为摸切（`convert.js` 同口径）。
        if (seat == self.dealer and not self.discards[seat]
                and self.popped is not None and sym == self.popped):
            sym = TSUMOGIRI

        is_reach = bool(getattr(d, "is_liqi", False)) or bool(getattr(d, "is_wliqi", False))
        if is_reach:
            sym = f"r{sym}"
            self.nriichi += 1

        self.discards[seat].append(sym)
        self.ldseat = seat
        self._drop_hand(seat, code)
        self._merge_doras(d)

    def on_chi_peng_gang(self, c) -> None:
        seat = int(c.seat)
        typ = int(c.type)
        tiles = [str(t) for t in c.tiles]
        froms = _as_int_list(c.froms)
        codes = [tile_to_tenhou(t) for t in tiles]
        if len(froms) != len(codes):
            raise RecordStructureError(f"副露 froms 与 tiles 长度不符：{tiles} / {froms}")
        src = [i for i, f in enumerate(froms) if f != seat]
        if len(src) != 1:
            raise RecordStructureError(f"副露来源不唯一：tiles={tiles} froms={froms} seat={seat}")
        si = src[0]
        target = froms[si]
        pai = codes[si]
        rest = [code for i, code in enumerate(codes) if i != si]

        if typ == 0:                    # 吃（只可能吃上家）
            if len(rest) != 2:
                raise RecordStructureError(f"吃副露牌数异常：{tiles}")
            naki = "c" + f"{pai:02d}" + "".join(f"{x:02d}" for x in rest)
        elif typ == 1:                  # 碰
            if len(rest) != 2:
                raise RecordStructureError(f"碰副露牌数异常：{tiles}")
            naki = self._with_marker(rest, "p", pai, self._rel_index(seat, target))
            self.pons[seat].append(naki)
        elif typ == 2:                  # 大明槓
            if len(rest) != 3:
                raise RecordStructureError(f"大明槓副露牌数异常：{tiles}")
            naki = self._with_marker(rest, "m", pai, self._rel_index(seat, target))
        else:
            raise RecordStructureError(f"未知的吃碰杠 type={typ}")

        self.takes[seat].append(naki)
        for i, code in enumerate(codes):
            if froms[i] == seat:
                self._drop_hand(seat, code)
        if typ == 2:
            # ★ 大明槓在 discards 里留一个 `0` 占位：`conv.rs` 的 `finalize_discards`
            #   会用 `pai == t!(?)` 把它删掉，从而对齐 takes / discards（`convert.js` 同）。
            self.discards[seat].append(0)

    def on_an_gang_add_gang(self, a) -> None:
        seat = int(a.seat)
        typ = int(a.type)
        code = tile_to_tenhou(a.tiles)

        if typ == 3:                    # 暗槓
            same = [c for c in self.hands[seat] if _deaka(c) == _deaka(code)]
            if len(same) < 4:
                # 手牌推演失败时的兜底：用同一张牌拼 4 张（赤牌分布可能退化，但仍合法）
                same = same + [_deaka(code)] * (4 - len(same))
            four = same[:4]
            pai = four[-1]
            naki = "".join(f"{c:02d}" for c in four[:-1]) + "a" + f"{pai:02d}"
            self.discards[seat].append(naki)
            for c in four:
                self._drop_hand(seat, c)
        elif typ == 2:                  # 加槓（把已有碰的 `p` 换成 `k<新牌>`）
            base = _deaka(code)
            idx = next((i for i, s in enumerate(self.pons[seat])
                        if _pon_base_code(s) is not None and _deaka(_pon_base_code(s)) == base),
                       None)
            if idx is None:
                raise RecordStructureError(f"加槓找不到对应的碰：seat={seat} tile={a.tiles}")
            old = self.pons[seat][idx]
            new = old.replace("p", f"k{code:02d}")
            self.pons[seat][idx] = new
            self.discards[seat].append(new)
            self._drop_hand(seat, code)
        else:
            raise RecordStructureError(f"未知的暗槓/加槓 type={typ}")

        self.ldseat = seat
        self._merge_doras(a)

    # -------------------------------------------------- 副露字符串

    @staticmethod
    def _rel_index(seat: int, target: int) -> int:
        """`target` 相对 `seat` 的位置 → 0=上家(kamicha) / 1=対面(toimen) / 2=下家(shimocha)。

        与 `conv.rs` 的 `Event::{Pon,Daiminkan}::target` 推导严格互逆：
        p/m 在字符位 0 ↔ 上家、2 ↔ 対面、4/6 ↔ 下家。

        ★ `convert.js` 用的是 ``relativeseating(seat, target) = (seat - target + 4 - 1) % 4``，
          与本函数 ``(seat - target) % 4`` 的映射是 ``{1:0, 2:1, 3:2}``——**不是**反过来的
          ``{3:0, 2:1, 1:2}``：`target = seat + 1` 是下家（差 3），`target = seat + 3` 才是上家（差 1）。
          写反会让碰/大明槓的「从哪家来」整体错位，`conv.rs` 直接报
          ``unexpected naki ... expected tile X from Some(N)``。
        """
        d = (seat - int(target)) % 4
        return {1: 0, 2: 1, 3: 2}.get(d, 0)

    @staticmethod
    def _with_marker(rest: list[int], marker: str, pai: int, pos: int) -> str:
        """按「标记字符位置」拼副露串：``pos`` = 0/1/2（上家/対面/下家）。"""
        codes = [f"{c:02d}" for c in rest]
        mk = f"{marker}{pai:02d}"
        if pos <= 0:
            return mk + "".join(codes)
        if pos == 1:
            return codes[0] + mk + "".join(codes[1:])
        return "".join(codes) + mk

    # -------------------------------------------------- 收尾

    def finish_hora(self, hule) -> list[Any]:
        """`RecordHule` → tenhou6 的 results 数组。"""
        hules = list(hule.hules)
        if not hules:
            raise RecordStructureError("RecordHule 没有任何 hules")
        declared = _as_int_list(getattr(hule, "delta_scores", []))

        deltas: list[list[int]] = []
        for i, h in enumerate(hules):
            seat = int(h.seat)
            if bool(h.zimo):
                target = seat
            else:
                # 荣和放铳者 = 最近打牌者（`conv.rs` 的 `kyoku.ldseat` 同口径）
                target = self.ldseat if self.ldseat >= 0 else seat
            d = self._hora_deltas(h, seat, target, is_head=(i == 0))
            deltas.append(d)

        # 单家（含自摸）：直接用牌谱自己的实际收支，最准（含本场/立直棒/包牌）；
        # 多家荣和：牌谱只给总收支，按对数字段分家，并做守恒兜底。
        if len(hules) == 1 and len(declared) >= self.n:
            deltas[0] = declared[:self.n]
        elif len(declared) >= self.n:
            total = [sum(col) for col in zip(*deltas)]
            diff = [a - b for a, b in zip(declared[:self.n], total)]
            if any(diff):
                deltas[0] = [a + b for a, b in zip(deltas[0], diff)]

        out: list[Any] = ["和了"]
        for h, d in zip(hules, deltas):
            seat = int(h.seat)
            if bool(h.zimo):
                target = seat
            else:
                target = self.ldseat if self.ldseat >= 0 else seat
            out.append(d)
            out.append([seat, target, seat, _hora_score_text(h, self.dealer)] + _fan_texts(h))
        return out

    def _hora_deltas(self, h, seat: int, target: int, *, is_head: bool) -> list[int]:
        """单个和牌者的收支（四麻）。

        口径与 `convert.js` 的 `parsehule` 一致：**立直棒 + 本场棒归头家**（多家荣和时
        只有第一家拿）；四麻不做自摸损/切上满贯。单家和牌时调用方会用牌谱实际收支覆盖。
        """
        n = self.n
        d = [0] * n
        stick = 1000 * (self.nriichi + int(self.meta[2])) if is_head else 0
        honba = 100 * int(self.meta[1]) if is_head else 0
        if bool(h.zimo):
            xian = int(getattr(h, "point_zimo_xian", 0) or 0)      # 闲家支付额
            qin = int(getattr(h, "point_zimo_qin", 0) or 0)        # 亲家支付额
            if seat == self.dealer:
                for i in range(n):
                    if i != seat:
                        d[i] = -(honba + xian)
                d[seat] = stick + (n - 1) * (honba + xian)
            else:
                total = 0
                for i in range(n):
                    if i == seat:
                        continue
                    pay = qin if i == self.dealer else xian
                    d[i] = -(honba + pay)
                    total += honba + pay
                d[seat] = stick + total
        else:
            rong = int(getattr(h, "point_rong", 0) or 0)
            amount = rong + (n - 1) * honba
            d[seat] = stick + amount
            d[target] = d[target] - amount
        return d

    def finish_ryukyoku(self, nt) -> list[Any]:
        """`RecordNoTile`（荒牌流局）→ results。"""
        delta = [0] * self.n
        for s in getattr(nt, "scores", []):
            for i, v in enumerate(_as_int_list(getattr(s, "delta_scores", []))):
                if i < self.n:
                    delta[i] += int(v)
        name = "流し満貫" if bool(getattr(nt, "liujumanguan", False)) else "流局"
        return [name, delta]

    def finish_liuju(self, lj) -> list[Any]:
        """`RecordLiuJu`（途中流局）→ results（无收支）。"""
        return [_LIUJU_NAMES.get(int(getattr(lj, "type", 0) or 0), "流局")]

    def to_tuple(self, results: list[Any]) -> list[Any]:
        entry: list[Any] = [self.meta, self.scores, self.doras, self.ura]
        for seat in range(self.n):
            entry.append(self.haipai[seat])
            entry.append(self.takes[seat])
            entry.append(self.discards[seat])
        entry.append(results)
        return entry


# ---------------------------------------------------------------- 点数文案

def _fan_texts(h) -> list[str]:
    out = []
    for f in getattr(h, "fans", []):
        val = int(getattr(f, "val", 0) or 0)
        name = yaku_map.yaku_name(int(getattr(f, "id", 0) or 0))
        out.append(f"{name}({val}飜)")
    return out


def _hora_score_text(h, dealer: int = -1) -> str:
    count = int(getattr(h, "count", 0) or 0)
    fu = int(getattr(h, "fu", 0) or 0)
    if bool(h.zimo):
        pts = int(getattr(h, "point_zimo_xian", 0) or 0)
        # ★ 2026-10-06：「亲家自摸」必须按真正的亲家 seat 判（旧代码写死 seat == 0，
        #   子家坐 0 号位时会被误标 ∀）。与 `_hora_deltas` 同口径（`self.dealer` = `ju`）。
        if int(h.seat) == dealer:
            return f"自摸 {pts}点∀"
        return f"自摸 {pts}点"
    pts = int(getattr(h, "point_rong", 0) or 0)
    return f"{fu}符{count}飜 {pts}点"


# ---------------------------------------------------------------- 顶层

def seat_of_account(record: GameRecord, account_id: int) -> int | None:
    """`account_id` 在本局的座位（0-3）；查不到返回 `None`（mjai-reviewer 的 `-a` 用）。"""
    aid = int(account_id or 0)
    for a in record.accounts or []:
        if int(a.get("account_id") or 0) == aid:
            seat = int(a.get("seat") or 0)
            return seat if 0 <= seat <= 3 else None
    return None


def _record_disp(record: GameRecord, *, tonpuu: bool, room_id: int) -> str:
    """tenhou6 的 `rule.disp`。

    ★ 两个硬约束（`log.rs`）：**不得含「三」**（否则判成三麻并报 `NotFourPlayer`）、
      **东场必须含「東」**（否则被判成半庄）。
    """
    if room_id > 0:
        room = "友人"
    else:
        name = yaku_map.MODE_ID_NAMES.get(int(record.mode_id or 0), "")
        room = name.split("·")[0] if name else "段位场"
    return f"{room}{'東' if tonpuu else '南'}喰赤"


def to_tenhou6(record: GameRecord, *, names: list[str] | None = None,
               title: str | None = None) -> dict[str, Any]:
    """雀魂 `GameRecord` → tenhou.net/6 的 dict（可直接 `json.dumps` 喂 mjai-reviewer）。

    返回 ``{"title": [str, str], "name": [str]*4, "rule": {...}, "log": [[17 元组], ...]}``。

    :raises UnsupportedRecordError: 三麻 / 活动局（血战规则）/ 拔北。
    :raises RecordStructureError: 牌谱结构与预期不符。
    """
    if int(record.mode or 0) in (11, 12):
        raise UnsupportedRecordError("三麻牌谱暂不支持 tenhou6 转换（mjai-reviewer 只吃四麻）")

    builders: list[_KyokuBuilder] = []
    cur: _KyokuBuilder | None = None
    log: list[list[Any]] = []

    for cls, obj in iter_actions(record.data):
        if cls in _UNSUPPORTED_EVENTS:
            raise UnsupportedRecordError(
                f"牌谱含不支持的事件 {cls}（活动/血战规则或三麻拔北）")
        if cls not in _KNOWN_EVENTS:
            continue

        if cls == "RecordNewRound":
            cur = _KyokuBuilder(obj)
            if cur.n != 4:
                raise UnsupportedRecordError(f"非四人牌谱（{cur.n} 家）暂不支持")
            builders.append(cur)
            continue

        if cur is None:                  # 局外事件（例如牌谱头部的噪声）
            continue

        if cls == "RecordDealTile":
            cur.on_deal(obj)
        elif cls == "RecordDiscardTile":
            cur.on_discard(obj)
        elif cls == "RecordChiPengGang":
            cur.on_chi_peng_gang(obj)
        elif cls == "RecordAnGangAddGang":
            cur.on_an_gang_add_gang(obj)
        elif cls == "RecordHule":
            # 里宝牌指示牌：多家和时取最长的一份（`convert.js` 同口径）
            ura: list[int] = []
            for h in obj.hules:
                li = [tile_to_tenhou(t) for t in getattr(h, "li_doras", [])]
                if len(li) > len(ura):
                    ura = li
            cur.ura = ura
            log.append(cur.to_tuple(cur.finish_hora(obj)))
        elif cls == "RecordNoTile":
            log.append(cur.to_tuple(cur.finish_ryukyoku(obj)))
        elif cls == "RecordLiuJu":
            log.append(cur.to_tuple(cur.finish_liuju(obj)))

    if not log:
        raise RecordStructureError("牌谱里没有可转换的对局（无 RecordNewRound）")

    if names is None:
        got: dict[int, str] = {}
        for a in record.accounts or []:
            seat = int(a.get("seat") or 0)
            if 0 <= seat <= 3:
                got[seat] = str(a.get("nickname") or "")
        names = [got.get(i) or f"Player{i}" for i in range(4)]
    names = (list(names) + ["", "", "", ""])[:4]

    tonpuu = int(record.mode or 0) == 1
    disp = _record_disp(record, tonpuu=tonpuu, room_id=int(record.room_id or 0))
    ts = int(record.end_time or 0) or int(record.start_time or 0)
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""

    return {
        # ★ 官方样本的 title 是 [房间/规则, 时间]；mjai-reviewer 不读它，仅便于人眼核对
        "title": [title or disp, when],
        "name": names,
        # aka=1 让 log.rs 的 has_aka 为真；aka51/52/53 如实标出四麻三种赤 5 各 1 张
        "rule": {"disp": disp, "aka": 1, "aka51": 1, "aka52": 1, "aka53": 1},
        "log": log,
    }


def dumps_tenhou6(record: GameRecord, **kwargs: Any) -> str:
    """`to_tenhou6` + `json.dumps`（`ensure_ascii=False`，separators 紧凑）。"""
    return json.dumps(to_tenhou6(record, **kwargs), ensure_ascii=False,
                      separators=(",", ":"))
