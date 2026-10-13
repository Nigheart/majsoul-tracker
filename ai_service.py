# -*- coding: utf-8 -*-
"""麻将 AI 复盘编排层（Feature-08，方案 B：服务器自建 mjai-reviewer + akochan）。

职责链：**雀魂牌谱 → tenhou.net/6（`paipu_tenhou`）→ mjai-reviewer + akochan（子进程）
→ `--json` 报告 → `ai_review.parse_review()` 摘要**，并把**原始报告落盘缓存**
（同一局同一座位**不重复分析**——分析极慢，见下）。

**为什么要异步 + 缓存 + 串行**（2026-09-28 实测）：单局东场 1 局 ≈ **1 分 23 秒**（4 核
OpenMP 全用满），一局 12 盘的半庄会到**十几分钟**量级 → 绝不能阻塞命令 handler；
且并发跑多个会互相抢 4 个核（还和机器人本体抢）→ 用**全局信号量串行**（`AI_CONCURRENCY=1`）。

**引擎路径**（服务器 `/opt/mjai-rev/`，搭建过程见 `docs/` 的 Feature-08 记录）：
    <AI_DIR>/src/mjai-reviewer/target/release/mjai-reviewer
    <AI_DIR>/src/akochan/{system.exe,libai.so,tactics.json}
可用环境变量 `MAJSOUL_AI_DIR` 覆盖根目录（测试/换机用）。
★ `tactics.json` 必须是 `{"tactics": {...}}` 外层包装；★ 必须给子进程
`LD_LIBRARY_PATH=<akochan dir>`（否则找不到 `libai.so`）。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from . import ai_review, paipu_parser

AI_DIR = os.environ.get("MAJSOUL_AI_DIR", "/opt/mjai-rev")
MJAI_REVIEWER = os.path.join(AI_DIR, "src", "mjai-reviewer", "target", "release", "mjai-reviewer")
AKOCHAN_DIR = os.path.join(AI_DIR, "src", "akochan")
AKOCHAN_TACTICS = os.path.join(AKOCHAN_DIR, "tactics.json")
WORK_DIR = os.path.join(AI_DIR, "work")          # 临时 tenhou6 / 报告落盘处
CACHE_KEEP = 20                                  # 报告缓存保留份数（一份最大几百 KB）
ANALYZE_TIMEOUT = 1800                           # 单次分析超时（秒）：12 盘半庄留足余量
AI_CONCURRENCY = 1                               # 并发上限（4 核，跑一个就接近满载）

_engine_missing = ""                             # 引擎缺失原因（缓存，供文案用）
_sem: asyncio.Semaphore | None = None


def engine_available() -> tuple[bool, str]:
    """引擎是否可用 → `(ok, 说明)`；不可用时说明给用户看（缺失哪个文件）。"""
    need = (
        (MJAI_REVIEWER, "mjai-reviewer 可执行文件"),
        (os.path.join(AKOCHAN_DIR, "system.exe"), "akochan system.exe"),
        (os.path.join(AKOCHAN_DIR, "libai.so"), "akochan libai.so"),
        (AKOCHAN_TACTICS, "akochan tactics.json"),
    )
    missing = [f"{desc}（{path}）" for path, desc in need if not os.path.exists(path)]
    if missing:
        return False, "AI 引擎未就绪：" + "、".join(missing)
    return True, ""


# ---------------------------------------------------------------- 缓存

def _work_dir() -> Path:
    p = Path(WORK_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_path(uuid: str, seat: int) -> Path:
    key = f"{str(uuid).strip()}_{int(seat)}".replace("/", "_")
    return _work_dir() / f"report_{key}.json"


def cache_get(uuid: str, seat: int) -> dict[str, Any] | None:
    """读缓存的**原始报告**（不做摘要，调用方自行 `parse_review`）；无/损坏返回 `None`。"""
    fp = _cache_path(uuid, seat)
    if not fp.is_file():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 损坏的缓存当未命中
        return None


def _cache_put(uuid: str, seat: int, report: dict[str, Any]) -> None:
    fp = _cache_path(uuid, seat)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, fp)
    # 按 mtime 淘汰，只留最近 CACHE_KEEP 份
    files = sorted(_work_dir().glob("report_*.json"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    for old in files[CACHE_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- 引擎调用

def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(AI_CONCURRENCY)
    return _sem


async def run_engine(tenhou6: dict[str, Any], seat: int, *,
                     tag: str = "job", timeout: int = ANALYZE_TIMEOUT) -> dict[str, Any]:
    """把 tenhou6 牌谱喂给 mjai-reviewer + akochan，返回 `--json` 报告 dict。

    串行（全局信号量）+ 超时 + 子进程 kill；任何失败抛 `RuntimeError`（调用方回退文本）。
    """
    ok, why = engine_available()
    if not ok:
        raise RuntimeError(why)
    wd = _work_dir()
    stamp = f"{tag}_{int(time.time())}"
    in_path = wd / f"in_{stamp}.json"
    out_path = wd / f"out_{stamp}.json"
    in_path.write_text(json.dumps(tenhou6, ensure_ascii=False), encoding="utf-8")

    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = AKOCHAN_DIR + (
        os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    args = [MJAI_REVIEWER, "-e", "akochan", "--akochan-dir", AKOCHAN_DIR,
            "--akochan-tactics", AKOCHAN_TACTICS,
            "-i", str(in_path), "-a", str(int(seat)),
            "--json", "--no-open", "-o", str(out_path)]

    async with _semaphore():
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=AI_DIR, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            _, _out = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"AI 分析超时（>{timeout}s），本局盘数较多时会比较慢，稍后再试")
        if proc.returncode != 0:
            tail = (_out or b"").decode("utf-8", "replace")[-300:]
            raise RuntimeError(f"AI 引擎退出码 {proc.returncode}：{tail.strip()}")

    if not out_path.is_file():
        raise RuntimeError("AI 引擎未产出报告文件")
    report = json.loads(out_path.read_text(encoding="utf-8"))
    for fp in (in_path, out_path):
        try:
            fp.unlink()
        except OSError:
            pass
    return report


_OVERVIEW_MISTAKES, _OVERVIEW_DANGERS = 6, 3   # 概览：失误 6 行 / 危险牌 3 条（用户拍板）
_DETAIL_LIMIT = 400            # 详细模式列出上限（远超实际手数＝「全列」）


def _parse_summary(report: dict[str, Any], detailed: bool) -> dict[str, Any]:
    """报告 → 摘要。概览＝**6 手失误 / 3 条危险牌**（用户 2026-09-29 拍板）；详细＝全列。"""
    if detailed:
        return ai_review.parse_review(report, max_mistakes=_DETAIL_LIMIT,
                                      max_dangers=_DETAIL_LIMIT)
    return ai_review.parse_review(report, max_mistakes=_OVERVIEW_MISTAKES,
                                  max_dangers=_OVERVIEW_DANGERS)


async def analyze(rec: Any, seat: int, *, uuid: str = "",
                  timeout: int = ANALYZE_TIMEOUT, detailed: bool = False) -> dict[str, Any]:
    """`GameRecord` → `{"report", "summary", "elapsed", "from_cache"}`（带缓存 + 摘要）。

    缓存键＝`(uuid, seat)`，**缓存的是原始报告**——所以「概览 / 详细」两种模式共用同一份缓存，
    只是重新 `parse_review` 时给的列出上限不同（零额外引擎开销）。
    """
    key = str(uuid or getattr(rec, "uuid", "") or "").strip()
    cached = cache_get(key, seat) if key else None
    if cached is not None:
        return {"report": cached, "summary": _parse_summary(cached, detailed),
                "elapsed": 0.0, "from_cache": True}

    from . import paipu_tenhou                       # 延迟导入：转换器缺失时不影响其它功能

    # ★ 座位不进 tenhou6——它在整份牌谱里是「玩家序号」，谁被复盘由 mjai-reviewer 的 `-a` 决定
    tenhou6 = paipu_tenhou.to_tenhou6(rec)
    t0 = time.time()
    report = await run_engine(tenhou6, seat, tag=key[:40] or "job", timeout=timeout)
    elapsed = time.time() - t0
    if key:
        try:
            _cache_put(key, seat, report)
        except OSError:
            pass
    return {"report": report, "summary": _parse_summary(report, detailed),
            "elapsed": elapsed, "from_cache": False}


def format_summary_text(result: dict[str, Any], *, meta: str = "",
                        compact: bool = False, detailed: bool = False) -> str:
    """`analyze()` 结果 → QQ 文案（含「本次分析耗时」与缓存提示）。

    `detailed=True`（详细模式）：概览摘要前加一行「📋 详细模式：共 N 手失误 / M 手危险牌」；
    有图时明细交给图（文本只留摘要），**无图时列出全部**（调用方需分段发送，否则 QQ 单条过长）。
    """
    out = ai_review.format_review(result.get("summary") or {}, meta=meta, compact=compact)
    if detailed:
        s = result.get("summary") or {}
        head = (f"📋 详细模式：共 {int(s.get('mistake_count') or 0)} 手失误、"
                f"{int(s.get('danger_count') or 0)} 手危险牌")
        out = f"{head}\n{out}" if compact else f"{head}\n{out}"
    if result.get("from_cache"):
        out += "\n（命中本地分析缓存，未重复跑 AI）"
    elif not compact and result.get("elapsed"):
        out += f"\n（本次分析耗时 {int(result['elapsed'])} 秒）"
    return out


def seat_of(rec: Any, account_id: int) -> int:
    """牌谱里 `account_id` 的初始座位号（0-3）；查不到返回 `-1`（调用方需处理）。

    ★ 委托 `paipu_tenhou.seat_of_account`（它按 `record.accounts[].seat` 取，兼容 dict/protobuf
      两种 accounts 元素形态）——**不要自己写 `a.account_id`**，实测那样取不到值。
    """
    try:
        from . import paipu_tenhou
        seat = paipu_tenhou.seat_of_account(rec, int(account_id or 0))
    except Exception:  # noqa: BLE001 —— 转换器缺失/结构异常时退化，调用方按 -1 处理
        seat = None
    return int(seat) if seat is not None and 0 <= int(seat) <= 3 else -1


def meta_of(rec: Any) -> str:
    """牌谱 → 一句话元信息（场次 + 局数 + 时长），复用既有解析口径。"""
    base = paipu_parser.parse_record(rec, 0)
    rounds = int(base.get("rounds") or 0)
    return f"{base.get('place') or '对局'}｜{rounds} 局"
