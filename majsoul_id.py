# -*- coding: utf-8 -*-
"""雀魂三套 ID 互转（纯函数，无第三方依赖）。

★ 为什么需要它
雀魂有 **三套 ID**，普通玩家在**游戏客户端里看到的「ID」是 friend_id（加好友 ID）**，
而牌谱屋 / 协议内部用的是 **account_id**。两者数值完全不同，且**可能撞车**：
例：某个 friend_id 可能恰好等于**另一个账号**的 account_id
——「把客户端 ID 直接当 account_id 查」会绑到**别人**身上。

- `account_id`：协议 / 牌谱屋 ID（★ 插件查询与绑定用这个）
- `friend_id` ：游戏客户端显示的加好友 ID（用户最常复制的就是它）
- `match_id`  ：牌谱链接最后那串数字

算法来源：社区逆向实现（Fat-pig-Cui/misc-code 的 `AccountDecode.py`）。
本仓库已用官方样例与多组真实账号数据**双向验证通过**（`acc2friend` 与 `fri2account` 互为逆运算）：
  acc2friend(15628582)=42121878 / fri2account(42121878)=15628582   ← 官方公开样例
"""
from __future__ import annotations

OFFSET = 10_000_000          # friend_id 的固定偏移（算法回加）
XOR_CODE = 6139246
ALPHABET = 67108863          # 2^26 - 1
EXCEEDED = ALPHABET + 1
OFFSET_2 = (1117113, 1358437)
XOR_CODE_2 = 86216345

MIN_FRIEND_ID = OFFSET       # friend_id 恒 ≥ 1e7
MAX_ACCOUNT_ID = 999_999_999  # account_id 实测最长 9 位


def acc2friend(account_id: int) -> int:
    """`account_id` → `friend_id`（客户端显示的加好友 ID）。"""
    data = int(account_id) ^ XOR_CODE
    tmp = data & ALPHABET
    tmp = (tmp & 524287) << 7 | tmp >> 19
    return int(tmp + (data & EXCEEDED) + OFFSET)


def fri2account(friend_id: int) -> int:
    """`friend_id`（客户端显示）→ `account_id`；输入 < 1e7 时返回 0（不可能是 friend_id）。"""
    data = int(friend_id) - OFFSET
    if data < 0:
        return 0
    tmp = data & ALPHABET
    tmp = (tmp & 127) << 19 | tmp >> 7
    return int(((data & EXCEEDED) + tmp) ^ XOR_CODE)


def acc2match(account_id: int) -> int:
    """`account_id` → `match_id`（牌谱链接尾号）。"""
    return int((7 * int(account_id) + OFFSET_2[0] ^ XOR_CODE_2) + OFFSET_2[1])


def mat2account(match_id: int) -> int:
    """`match_id`（牌谱链接尾号）→ `account_id`。"""
    return int(((int(match_id) - OFFSET_2[1] ^ XOR_CODE_2) - OFFSET_2[0]) / 7)


def friend_of(account_id: int) -> int:
    """安全版 `acc2friend`：非法/空输入返回 0（用于展示「客户端 ID」）。"""
    try:
        return acc2friend(int(account_id)) if int(account_id) > 0 else 0
    except Exception:  # noqa: BLE001
        return 0


def candidates(raw: int) -> list[tuple[int, str]]:
    """把纯数字输入解析为**可能的 `account_id` 候选**，按可信度排序。

    返回 `[(account_id, kind)]`，`kind` ∈ `{"friend_id", "account_id"}`。

    排序策略：客户端显示的是 friend_id，用户最可能直接复制它 → **friend_id 解释优先**；
    同时保留「原样当 account_id」作为兜底候选，由调用方按「能否查到对局」裁决。
    """
    raw = int(raw)
    if raw <= 0:
        return []
    out: list[tuple[int, str]] = []
    if raw >= MIN_FRIEND_ID:
        acc = fri2account(raw)
        if 0 < acc <= MAX_ACCOUNT_ID and acc != raw:
            out.append((acc, "friend_id"))
    if raw <= MAX_ACCOUNT_ID:
        out.append((raw, "account_id"))
    return out
