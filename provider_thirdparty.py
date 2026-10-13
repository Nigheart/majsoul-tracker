# -*- coding: utf-8 -*-
"""第三方数据源（降级备选）：雀魂牌谱屋（amae-koromo）免登录 API。

⚠️ **能力边界**（M1 实测，见《协议探测结果.md》§9.3）：

- ✅ `search_player`（昵称 → account_id + 段位）——用于**绑定环节**把昵称解析成 ID；
- ❌ `player_records` / `games`（逐场对局明细）——已被 CAP 反爬拦截
  （HTTP 429 `x-cap-token-required`，浏览器 WASM 求解器，后端无法绕过）。

因此本模块只承担「昵称 → ID」解析；`fetch_recent_games()` 明确降级报错。
Phase 1 的对局数据一律走官方协议源（`provider_protocol`）。
"""
from __future__ import annotations

import urllib.parse
from typing import Any

import httpx

from .provider_protocol import ProviderError

BASE4 = "https://5-data.amae-koromo.com/api/v2/pl4"   # 四麻统计库
BASE3 = "https://5-data.amae-koromo.com/api/v2/pl3"   # 三麻统计库
TIMEOUT = 12.0
UA = "majsoul-tracker/0.1 (+nonebot plugin)"


async def search_player(nickname: str, limit: int = 10) -> list[dict[str, Any]]:
    """按昵称搜玩家（免登录）→ `[{account_id, nickname, level_id, level_score, source}]`。

    失败抛 `ProviderError`；无匹配返回空列表。
    """
    nick = (nickname or "").strip()
    if not nick:
        raise ProviderError("昵称为空")
    q = urllib.parse.quote(nick)
    out: dict[int, dict[str, Any]] = {}
    last_err = ""
    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": UA},
                                 follow_redirects=True) as client:
        for base, src in ((BASE4, "pl4"), (BASE3, "pl3")):
            url = f"{base}/search_player/{q}?limit={int(limit)}&tag=all"
            try:
                resp = await client.get(url)
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                continue
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                continue
            try:
                items = resp.json()
            except Exception as e:  # noqa: BLE001
                last_err = f"返回非 JSON：{e}"
                continue
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                aid = int(it.get("account_id") or it.get("id") or 0)
                if not aid:
                    continue
                lv = it.get("level") if isinstance(it.get("level"), dict) else {}
                nick_name = it.get("nickname") or it.get("name") or ""
                if aid in out:
                    continue
                out[aid] = {
                    "account_id": aid,
                    "nickname": nick_name,
                    "level_id": int(lv.get("id") or 0),
                    "level_score": int(lv.get("score") or 0),
                    "source": src,
                }
    if not out and last_err:
        raise ProviderError(f"昵称查询失败：{last_err}")
    return list(out.values())


async def fetch_recent_games(account_id: int, **_kw) -> list:
    """第三方源**拿不到**逐场明细（CAP 拦截），明确降级。"""
    raise ProviderError(
        "第三方数据源不可用：牌谱屋 player_records 已被 CAP 反爬拦截（HTTP 429 x-cap-token-required），"
        "无法获取对局明细。请使用官方协议源（provider=protocol）。"
    )


async def available() -> bool:
    """探测第三方源是否可用（昵称搜索端点连通性）。"""
    try:
        await search_player("abc", limit=1)
        return True
    except ProviderError:
        return False
