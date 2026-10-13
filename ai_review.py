# -*- coding: utf-8 -*-
"""麻将 AI 复盘：`mjai-reviewer --json` 报告 → 结构化摘要 + QQ 文案（纯函数）。

**来源（Feature-08 方案 B）**：服务器 `/opt/mjai-rev/` 自建的
[mjai-reviewer](https://github.com/Equim-chan/mjai-reviewer)（Rust，1.5.10）+
[akochan](https://github.com/critter-mj/akochan)（C++，纯 CPU，无需模型权重）。
调用形态：`mjai-reviewer -e akochan --akochan-dir … -i <tenhou6.json> -a <座位> --json`。

**报告结构（2026-09-28 实跑实测，1 局 58KB）**：
    ｜`engine` / `game_length` / `version` / `player_id` / `review_time` / `mjai_log`
    └ `review`: `total_reviewed`（评估手数）/ **`rating`（一致率 0~1）** / `kyokus[]`
       └ `kyokus[i]`: `kyoku` / `honba` / `end_status[]` / `entries[]`
          └ `entries[j]`: `junme` / `tiles_left` / `tile` / `state{tehai,fuuros}`
             / **`expected[]`（AI 推荐）/ `actual[]`（实际）** / `acceptance`(agree|disagree)
             / `details[]`（每个候选动作的 `review.pt_exp_after` 等）

★ **关键失误口径**（实测定案）：损失 = **`max(候选 pt_exp_after) − 实际动作的 pt_exp_after`**，
  按损失降序 = 「关键失误手」。⚠️ **不要用 `acceptance` 排序**——它只是离散标记，
  量不出严重程度；`details` 里的期望值才能给出可比较的差距。
★ **`acceptance` 是三档**（2026-09-28 实测，别当布尔用）：`agree` / **`tolerable`（可容忍）** /
  `disagree`；`rating` 是 mjai-reviewer 的**连续加权分**（实测 16 手：11 同意 + 1 可容忍 + 4 分歧
  → `rating`=0.8402，**不等于**任何简单计数比）→ **直接用报告里的 `rating`，不要自己重算**。
★ 缺失态要显式（用户口径）：无失误写「与 AI 建议一致」，报告为空写「AI 分析数据不可用」。
"""
from __future__ import annotations

from typing import Any

# 动作类型 → 中文前缀（`dahai` 单列，因为要带牌名）
_MOVE_NAMES = {
    "reach": "立直", "chi": "吃", "pon": "碰", "kan": "杠", "ankan": "暗杠",
    "kakan": "加杠", "hora": "和牌", "ryukyoku": "流局", "none": "过",
}

# 局数 → 风名（tenhou6 从 0 起：0-3 东 1-4、4-7 南 1-4）
_WINDS = ("东", "南", "西", "北")


def _first_move(moves: list[Any] | None) -> dict[str, Any] | None:
    """动作列表 → 第一个可比动作（`dahai` / `reach` / 鸣牌 / `hora`）。"""
    for m in moves or []:
        if isinstance(m, dict) and m.get("type"):
            return m
    return None


def _tile_names(items: list[Any] | None) -> list[str]:
    """牌名数组 → **只保留字符串元素**（非字符串一律丢弃）。

    ★ 防「乱码块」（2026-09-29 实测）：报告里的 `state.fuuros` 元素是**动作 dict**，
    一旦被 `str()` 成 Python repr 交给卡片，就会当成一张牌去贴图（素材不存在 →
    `_paste_tile` 回退画占位文字，用户看到深绿乱码块）。这里宁可丢，也不放行脏数据。
    """
    return [str(x) for x in items or [] if isinstance(x, str) and str(x)]


def _fuuro_tiles(fuuros: list[Any] | None) -> list[str]:
    """`state.fuuros[]` → **展开成牌名列表**（一摞面子摊平，供卡片逐张画牌图）。

    报告实测形态：`{"type":"pon","target":3,"pai":"S","consumed":["S","S"]}`（碰南）→
    `["S","S","S"]`；吃/杠同理（`pai` + `consumed`）。字符串元素**原样保留**以兼容老格式。
    """
    out: list[str] = []
    for f in fuuros or []:
        if isinstance(f, str):
            if f:
                out.append(f)
            continue
        if not isinstance(f, dict):
            continue
        pai = f.get("pai")
        if isinstance(pai, str) and pai:
            out.append(pai)
        out.extend(str(c) for c in (f.get("consumed") or []) if isinstance(c, str) and c)
    return out


def move_label(mv: dict[str, Any] | None) -> str:
    """动作 → 「打 3p」/「立直」/「碰 2m」（供文案与卡片共用）。"""
    if not mv:
        return "—"
    t = str(mv.get("type"))
    if t == "dahai":
        return f"打 {mv.get('pai')}"
    if t in _MOVE_NAMES:
        pai = mv.get("pai")
        return f"{_MOVE_NAMES[t]} {pai}" if pai else _MOVE_NAMES[t]
    return t


def kyoku_label(kyoku: int, honba: int) -> str:
    """`(kyoku, honba)` → 「东2局1本场」（tenhou6 的 `kyoku` 从 0 起）。"""
    k = int(kyoku or 0)
    wind = _WINDS[(k // 4) % 4] if k >= 0 else "?"
    return f"{wind}{k % 4 + 1}局" + (f"{int(honba)}本场" if honba else "")


def end_text(end_status: list[Any] | None) -> str:
    """`end_status[]` → 「和牌：seat2 ← seat3（+8000/-8000）」/「流局」。"""
    parts: list[str] = []
    for e in end_status or []:
        if not isinstance(e, dict):
            continue
        t = str(e.get("type"))
        if t == "hora":
            actor, target = e.get("actor"), e.get("target")
            deltas = e.get("deltas") or []
            d = f"（{deltas[actor]:+d}）" if isinstance(actor, int) and actor < len(deltas) else ""
            who = f"seat{actor}←seat{target}" if target is not None else f"seat{actor}"
            parts.append(f"和牌 {who}{d}")
        elif t == "ryukyoku":
            parts.append("流局")
        else:
            parts.append(t)
    return "；".join(parts) if parts else "—"


def _candidates(entry: dict[str, Any]) -> list[tuple[dict[str, Any], float, float | None]]:
    """一条 entry 的 `details[]` → `[(动作, pt_exp_after, 放铳概率)]`（只保留有期望值的候选）。

    `放铳概率` = `review.total_houjuu_hai_prob_now`（**该牌打出去就放铳的绝对概率**，实测分布
    0~28%、p50 0.16%、p90 5.4%）；缺失时为 `None`（不参与「危险牌」排序）。
    """
    out: list[tuple[dict[str, Any], float, float | None]] = []
    for det in entry.get("details") or []:
        if not isinstance(det, dict):
            continue
        mv = _first_move(det.get("moves"))
        if not mv:
            continue
        rv = det.get("review") or {}
        try:
            exp = float(rv.get("pt_exp_after"))
        except (TypeError, ValueError):
            continue
        prob: float | None
        try:
            prob = float(rv.get("total_houjuu_hai_prob_now"))
        except (TypeError, ValueError):
            prob = None
        out.append((mv, exp, prob))
    return out


def _acceptance(entry: dict[str, Any]) -> str:
    """`acceptance` 归一化：`agree` / `tolerable` / `disagree`（未知值归 `disagree`，宁严不宽）。"""
    a = str(entry.get("acceptance") or "").strip().lower()
    return a if a in ("agree", "tolerable", "disagree") else "disagree"


def _mistake_of(entry: dict[str, Any], kyoku: int, honba: int,
                cands: list[tuple[dict[str, Any], float, float | None]] | None = None
                ) -> dict[str, Any] | None:
    """一条 entry → 偏差条目；`agree` 或拿不到期望值时返回 `None`。`cands` 可复用（省一次解析）。"""
    acc = _acceptance(entry)
    if acc == "agree":
        return None
    actual = _first_move(entry.get("actual"))
    cands = cands if cands is not None else _candidates(entry)
    if not actual or not cands:
        return None
    best_mv, best_exp, _ = max(cands, key=lambda x: x[1])
    act_exp = next((e for m, e, _p in cands if m.get("type") == actual.get("type")
                    and m.get("pai") == actual.get("pai")), None)
    if act_exp is None:
        return None
    state = entry.get("state") or {}
    return {
        "kyoku": int(kyoku or 0),
        "honba": int(honba or 0),
        "label": kyoku_label(kyoku, honba),
        "acceptance": acc,
        "junme": int(entry.get("junme") or 0),
        "tiles_left": int(entry.get("tiles_left") or 0),
        "drawn": str(entry.get("tile") or ""),
        "actual": actual,
        "best": best_mv,
        "loss": round(float(best_exp) - float(act_exp), 3),
        "best_exp": round(float(best_exp), 3),
        "actual_exp": round(float(act_exp), 3),
        "tehai": _tile_names(state.get("tehai")),
        "fuuros": _fuuro_tiles(state.get("fuuros")),
    }


def _danger_of(entry: dict[str, Any], kyoku: int, honba: int, *,
               threshold: float,
               cands: list[tuple[dict[str, Any], float, float | None]] | None = None
               ) -> dict[str, Any] | None:
    """一手 → 「危险打牌」条目（**实际打出的牌**放铳概率 ≥ `threshold`）；否则 `None`。

    ★ 关键区分（2026-09-29 实测口径）：`ai_agrees=True` 表示 **AI 也建议打这张**——那说明当时
      别无选择/必须进攻（实测 6s 放铳概率 28.1% 时 AI 也建议打 6s），**不算失误**；只有
      `ai_agrees=False` 才是「可避免的危险」（AI 建议的牌更安全却没打）。
    """
    actual = _first_move(entry.get("actual"))
    if not actual:
        return None
    cands = cands if cands is not None else _candidates(entry)
    if not cands:
        return None
    act = next(((m, e, p) for m, e, p in cands
                if m.get("type") == actual.get("type") and m.get("pai") == actual.get("pai")), None)
    if act is None or act[2] is None or float(act[2]) < float(threshold):
        return None
    best_mv, _best_exp, best_prob = max(cands, key=lambda x: x[1])
    safest_mv, _se, safest_prob = min(
        cands, key=lambda x: (x[2] if x[2] is not None else 0.0))
    ai_agrees = (best_mv.get("pai") == actual.get("pai")
                 and best_mv.get("type") == actual.get("type"))
    state = entry.get("state") or {}
    return {
        "kyoku": int(kyoku or 0), "honba": int(honba or 0),
        "label": kyoku_label(kyoku, honba),
        "junme": int(entry.get("junme") or 0),
        "actual": actual, "prob": round(float(act[2]), 4),
        "best": best_mv,
        "best_prob": round(float(best_prob), 4) if best_prob is not None else None,
        "safest": safest_mv,
        "safest_prob": round(float(safest_prob), 4) if safest_prob is not None else None,
        "ai_agrees": bool(ai_agrees),
        "acceptance": _acceptance(entry),
        "tehai": _tile_names(state.get("tehai")),
        "fuuros": _fuuro_tiles(state.get("fuuros")),
    }


# ---------------------------------------------------------------- 表现分（自建口径）
# ★ 2026-09-29（用户要求「类似 rating 的评分」）——先说清口径：
#   报告里的 `review.rating` 是**逐手一致率**（akochan 语义），**不是** Mortal 那种经校准的
#   牌力分（Mortal rating 需要 Mortal 权重才有）。所以这里由三个**可得**指标加权成 0~100
#   表现分，纯本地计算、**只在本插件内可比**，不要对外声称等于雀魂段位：
#     55% 一致率 + 30%「非偏差率」 + 15% 期望损失项（每手平均损失 ≥ SCORE_LOSS_REF 记 0 分）
SCORE_W_RATING, SCORE_W_DEVIATION, SCORE_W_LOSS = 0.55, 0.30, 0.15
SCORE_LOSS_REF = 0.10                    # 每手平均期望损失的「参考上限」
_SCORE_GRADES = ((90.0, "S", "极佳"), (80.0, "A", "优秀"), (70.0, "B", "良好"),
                 (60.0, "C", "一般"), (0.0, "D", "失误偏多"))


def score_of(rating: float, deviations: int, reviewed: int,
             avg_loss: float) -> tuple[float, str, str]:
    """`(一致率, 偏差手数, 评估手数, 每手平均期望损失)` → `(0~100 分, 等级, 等级说明)`。

    实测参照（2026-09-29）：12 局半庄（一致率 92.5%、偏差率 33.3%、均损 0.039）→ **79.9 B**；
    1 局东场样本（一致率 84.0%、均损 0.501）→ **66.8 C**——短局里几个大失误会被放大，符合直觉。
    """
    r = max(0.0, min(1.0, float(rating or 0.0)))
    n = max(1, int(reviewed or 0))
    dev_rate = max(0.0, min(1.0, float(deviations or 0) / n))
    loss_term = 1.0 - min(1.0, max(0.0, float(avg_loss or 0.0)) / SCORE_LOSS_REF)
    score = round(max(0.0, min(100.0, 100.0 * (
        SCORE_W_RATING * r + SCORE_W_DEVIATION * (1.0 - dev_rate)
        + SCORE_W_LOSS * loss_term))), 1)
    for thr, grade, text in _SCORE_GRADES:
        if score >= thr:
            return score, grade, text
    return score, "D", "失误偏多"


def parse_review(report: dict[str, Any], *, max_mistakes: int = 6,
                 max_dangers: int = 3, danger_threshold: float = 0.05) -> dict[str, Any]:
    """报告 → 复盘摘要（纯函数）。`report` 为空/异常结构时返回 `available=False` 的结果。

    `max_dangers` / `danger_threshold`：**危险打牌**（实际打出的牌放铳概率 ≥ 阈值，默认 5%）
      的列出上限与阈值——实测 159 手里 10 手超过 5%（阈值取 5% 时不至于刷屏）。
    """
    review = (report or {}).get("review") or {}
    kyokus_in = review.get("kyokus") or []
    kyokus: list[dict[str, Any]] = []
    mistakes: list[dict[str, Any]] = []
    dangers: list[dict[str, Any]] = []
    agree = tolerable = total = unquantified = 0
    prob_seen = False
    for k in kyokus_in:
        if not isinstance(k, dict):
            continue
        ky, hb = int(k.get("kyoku") or 0), int(k.get("honba") or 0)
        entries = [e for e in (k.get("entries") or []) if isinstance(e, dict)]
        k_agree = sum(1 for e in entries if _acceptance(e) == "agree")
        k_tol = sum(1 for e in entries if _acceptance(e) == "tolerable")
        agree += k_agree
        tolerable += k_tol
        total += len(entries)
        kyokus.append({
            "kyoku": ky, "honba": hb, "label": kyoku_label(ky, hb),
            "entries": len(entries), "agree": k_agree, "tolerable": k_tol,
            "end": end_text(k.get("end_status")),
        })
        for e in entries:
            acc = _acceptance(e)
            cands = _candidates(e)
            if not prob_seen and any(p is not None for _m, _x, p in cands):
                prob_seen = True
            if acc != "agree":
                m = _mistake_of(e, ky, hb, cands)
                if m:
                    mistakes.append(m)
                else:
                    unquantified += 1
            dg = _danger_of(e, ky, hb, threshold=danger_threshold, cands=cands)
            if dg:
                dangers.append(dg)

    mistakes.sort(key=lambda m: (-float(m["loss"]), m["kyoku"], m["junme"]))
    dangers.sort(key=lambda d: (-float(d["prob"]), d["kyoku"], d["junme"]))
    try:
        rating = float(review.get("rating"))
    except (TypeError, ValueError):
        rating = 0.0
    available = bool(total and kyokus_in)
    # ★ 表现分：用**全部**偏差手（不只是列出的前 N 手）与「每评估手平均期望损失」
    loss_total = sum(float(m["loss"]) for m in mistakes if m.get("loss") is not None)
    avg_loss = loss_total / max(1, total)
    score, grade, grade_text = score_of(rating, total - agree, total, avg_loss)
    return {
        "available": available,
        "engine": str((report or {}).get("engine") or ""),
        "game_length": str((report or {}).get("game_length") or ""),
        "review_time": str((report or {}).get("review_time") or ""),
        "rating": rating,
        "score": score,
        "grade": grade,
        "grade_text": grade_text,
        "loss_total": round(loss_total, 3),
        "avg_loss": round(avg_loss, 4),
        "reviewed": total,
        "agree": agree,
        "tolerable": tolerable,
        "disagree": total - agree - tolerable,
        "deviations": total - agree,
        "unquantified": unquantified,
        "kyokus": kyokus,
        "mistakes": mistakes[:max_mistakes],
        "mistake_count": len(mistakes),
        "dangers": dangers[:max_dangers],
        "danger_count": len(dangers),
        "danger_threshold": float(danger_threshold),
        "has_probs": bool(prob_seen),
    }


def format_review(summary: dict[str, Any], *, compact: bool = False,
                  title: str = "🀄 雀魂 AI 复盘", meta: str = "") -> str:
    """复盘摘要 → QQ 文案；`compact=True` 为有图时的精简版（明细交给图片卡）。

    缺失态显式：无失误 → 「与 AI 建议一致」；整体不可用 → 「AI 分析数据不可用」。
    """
    if not summary or not summary.get("available"):
        return f"{title}\nAI 分析数据不可用（报告为空或引擎未产出结果）"

    head = [f"{title}（{summary.get('engine') or 'AI'}）"]
    if meta:
        head.append(meta)
    if summary.get("score") is not None:
        head.append(f"⭐ 表现分 {float(summary['score']):.1f} / 100"
                    f"（{summary.get('grade') or '?'} {summary.get('grade_text') or ''}）"
                    f"｜平均每手期望损失 {float(summary.get('avg_loss') or 0):.3f}")
    head.append(f"评估 {summary['reviewed']} 手｜一致率 {summary['rating'] * 100:.1f}%"
                f"（同意 {summary['agree']} / 可容忍 {summary.get('tolerable', 0)}"
                f" / 分歧 {summary['disagree']}）")
    if compact:
        # 有图时的精简版：明细交给图片卡，但**危险牌结论要留一句**（图片只列高概率的那几手，
        # 「一手都没有」与「引擎没给数据」必须能区分——用户口径：缺失态显式写）。
        dgs_c = summary.get("dangers") or []
        dth_c = float(summary.get("danger_threshold") or 0.05)
        if not summary.get("has_probs"):
            head.append("危险牌：引擎未提供放铳概率数据")
        elif dgs_c:
            head.append(f"危险牌 {int(summary.get('danger_count') or len(dgs_c))} 手"
                        f"（最高 {float(dgs_c[0]['prob']) * 100:.1f}%，详见图片）")
        else:
            head.append(f"危险牌：无（未打出放铳概率 ≥{dth_c * 100:.0f}% 的牌）")
        return "\n".join(head)

    lines = list(head)
    lines.append("各局：" + "｜".join(
        f"{k['label']} {k['agree']}/{k['entries']}" for k in summary.get("kyokus") or []))

    ms = summary.get("mistakes") or []
    dev = int(summary.get("deviations") or 0)
    unq = int(summary.get("unquantified") or 0)
    if not ms:
        if dev:
            lines.append(f"关键失误：本场 {dev} 手与 AI 建议不同，但引擎未给出可比较的期望值"
                         "（无法评估严重程度）")
        else:
            lines.append("关键失误：本场与 AI 建议一致（无明显失误）")
    else:
        tail = f"，另有 {unq} 手偏差无期望值可比" if unq else ""
        lines.append(f"关键失误（可量化 {len(ms)} 手 / 共 {dev} 手偏差{tail}）：")
        for i, m in enumerate(ms, 1):
            tag = "（可容忍）" if m.get("acceptance") == "tolerable" else ""
            lines.append(
                f"　{i}. {m['label']} {m['junme']} 巡：实际 {move_label(m['actual'])}"
                f"，AI 建议 {move_label(m['best'])}（期望差 {m['loss']:.2f}）{tag}")

    # ---- 危险牌（实际打出的牌放铳概率高）----
    dgs = summary.get("dangers") or []
    dth = float(summary.get("danger_threshold") or 0.05)
    if not summary.get("has_probs"):
        lines.append("危险牌：本报告未提供放铳概率数据（引擎未输出）")
    elif dgs:
        lines.append(f"危险牌（打出时放铳概率 ≥{dth * 100:.0f}%：共 "
                     f"{int(summary.get('danger_count') or len(dgs))} 手，列前 {len(dgs)}）：")
        for i, d in enumerate(dgs, 1):
            if d.get("ai_agrees"):
                who = "AI 也建议打（无奈之举）"
            else:
                bp = d.get("best_prob")
                lbl = move_label(d.get("best"))
                if bp is None:
                    who = f"AI 建议 {lbl}"
                elif float(bp) < float(d["prob"]):
                    who = f"更安全的选择 {lbl}（{float(bp) * 100:.1f}%）"
                else:
                    # AI 也可能为了期望值打更危险的牌 → 写清，别让人以为 AI 建议的更安全
                    who = f"AI 建议 {lbl}（{float(bp) * 100:.1f}%，但期望值更高）"
            lines.append(f"　{i}. {d['label']} {d['junme']} 巡：{move_label(d['actual'])}"
                         f"（{float(d['prob']) * 100:.1f}%）——{who}")
    else:
        lines.append(f"危险牌：本场未打出放铳概率 ≥{dth * 100:.0f}% 的牌（相对安全）")
    return "\n".join(lines)
