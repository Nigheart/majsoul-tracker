# -*- coding: utf-8 -*-
"""绑定读写与输入白名单校验（纯逻辑，不依赖 nonebot）。

数据文件：`plugin_data/majsoul_tracker/bindings.json`

```json
{
  "bindings": {
    "<QQ用户ID>": {
      "majsoul_id": "12345678",            // 用户输入的原始标识
      "account_id": 12345678,              // 归一化后的雀魂账号 ID（0=尚未解析）
      "majsoul_nickname": "示例玩家",   // 解析到的雀魂昵称（回显/校对用）
      "bind_type": "account_id|nickname",
      "group_ids": ["123456"],           // 在哪些群绑定 → 对局结果推送到这些群
      "bound_at": 1789740000
    }
  }
}
```

输入白名单（M1 实测：昵称 2~13 字符、account_id 1~9 位）：
- 纯数字 1~9 位 → 数字 ID（★ 可能是**两套 ID**：客户端显示的 `friend_id` 或协议 `account_id`，
  由 `majsoul_id.candidates()` 展开候选、按「能否查到对局」裁决）
- `account:12345678` / `friend:87654321` → 显式指定是哪套 ID（有歧义时用）
- 2~13 字符文本 → `nickname`（需第三方 API 解析为 ID）
- 牌谱链接 → 首版**不支持**（无法从牌谱确定"哪个账号是你"），给可读提示。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

PLUGIN_NAME = "majsoul_tracker"
BINDINGS_FILE = "bindings.json"

ACCOUNT_ID_RE = re.compile(r"^\d{1,9}$")
QQ_RE = re.compile(r"^\d{5,12}$")
PAIPU_RE = re.compile(r"(?:paipu=|/paipu/)([0-9]{6}-[0-9a-fA-F-]{20,})")
NICKNAME_MIN, NICKNAME_MAX = 2, 13

BIND_TYPE_ACCOUNT_ID = "account_id"
BIND_TYPE_FRIEND_ID = "friend_id"
BIND_TYPE_NICKNAME = "nickname"
BIND_TYPE_PAIPU = "paipu"

# 显式前缀：account:12345678（协议/牌谱屋 ID）/ friend:87654321（客户端加好友 ID）
ID_PREFIXES = (("account:", BIND_TYPE_ACCOUNT_ID), ("friend:", BIND_TYPE_FRIEND_ID))


# ---------------------------------------------------------------- 存储后端

def _local_path(filename: str) -> str:
    d = os.environ.get("MAJSOUL_DATA_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_localdata")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename)


def _load_json(filename: str, default: Any) -> Any:
    try:
        from web_admin.plugin_runtime import load_plugin_json
        return load_plugin_json(PLUGIN_NAME, filename, default)
    except Exception:  # noqa: BLE001 —— 离线/单测回退到本地目录
        path = _local_path(filename)
        if not os.path.exists(path):
            return default
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return default


def _save_json(filename: str, data: Any) -> None:
    try:
        from web_admin.plugin_runtime import save_plugin_json
        save_plugin_json(PLUGIN_NAME, filename, data)
    except Exception:  # noqa: BLE001
        path = _local_path(filename)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


# ---------------------------------------------------------------- 输入解析

def parse_input(text: str) -> tuple[str, str, str]:
    """解析绑定输入 → `(归一化值, bind_type, 错误说明)`。

    错误说明为空串表示通过校验。
    """
    raw = (text or "").strip()
    if not raw:
        return "", "", "请输入雀魂账号 ID 或昵称，例如：/雀魂绑定 12345678"

    low = raw.lower()
    for prefix, btype in ID_PREFIXES:
        if low.startswith(prefix):
            val = raw[len(prefix):].strip()
            if not ACCOUNT_ID_RE.match(val):
                return "", "", f"{prefix} 后面要填纯数字 ID，例如：/雀魂绑定 {prefix}12345678"
            return val, btype, ""

    m = PAIPU_RE.search(raw)
    if m:
        return m.group(1), BIND_TYPE_PAIPU, (
            "暂不支持用牌谱链接绑定（无法从牌谱判断哪个账号是你）。"
            "请改用雀魂 ID（推荐）或昵称，例如：/雀魂绑定 12345678"
        )

    if ACCOUNT_ID_RE.match(raw):
        return raw, BIND_TYPE_ACCOUNT_ID, ""

    if len(raw) < NICKNAME_MIN or len(raw) > NICKNAME_MAX:
        return "", "", f"昵称长度需为 {NICKNAME_MIN}~{NICKNAME_MAX} 个字符，或直接填纯数字雀魂 ID"

    if any(ch.isspace() for ch in raw):
        return "", "", "昵称中不能包含空格"

    return raw, BIND_TYPE_NICKNAME, ""


def is_account_id(value: str) -> bool:
    return bool(ACCOUNT_ID_RE.match((value or "").strip()))


# ---------------------------------------------------------------- 管理员命令参数

def parse_admin_args(text: str, at_qq: str = "") -> tuple[str, str, str]:
    """解析管理员命令参数 → `(目标QQ, 待绑定值, 错误说明)`（纯函数，便于单测）。

    两种写法都支持（`at_qq` 由调用方从消息的 at 段取出）：
    - `@群友 12345678` → `at_qq="<QQ>"`，`text` 里只剩待绑定值
    - `10086 12345678` → 文本形式，第一个纯数字当作目标 QQ
    """
    raw = (text or "").strip()
    target = str(at_qq or "").strip()

    if target:
        if not QQ_RE.match(target):
            return "", "", f"@ 到的 QQ「{target}」不是有效 QQ 号"
        raw = raw.lstrip("@").strip()      # 个别适配器会把「@」当文本带进来
        if not raw:
            return "", "", ("请补上雀魂 ID，例如：/雀魂代绑定 @群友 12345678")
        return target, raw, ""

    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return "", "", ("用法：/雀魂代绑定 <QQ号或@群友> <雀魂ID 或 昵称>\n"
                        "例：/雀魂代绑定 10086 12345678　或　/雀魂代绑定 @群友 12345678")
    first, rest = parts[0], parts[1].strip()
    if not QQ_RE.match(first.lstrip("@")):
        return "", "", f"「{first}」不是有效 QQ 号（应为 5~12 位数字），或改用 @群友 的写法"
    return first.lstrip("@"), rest, ""


def parse_admin_target(text: str, at_qq: str = "") -> tuple[str, str]:
    """解析管理员命令的**目标 QQ**（无第二个参数，用于 `/雀魂代解绑`）→ `(目标QQ, 错误说明)`。"""
    raw = (text or "").strip()
    target = str(at_qq or "").strip()
    if target:
        if not QQ_RE.match(target):
            return "", f"@ 到的 QQ「{target}」不是有效 QQ 号"
        return target, ""
    token = raw.split(maxsplit=1)[0].lstrip("@") if raw else ""
    if not token:
        return "", "用法：/雀魂代解绑 <QQ号或@群友>\n例：/雀魂代解绑 10086　或　/雀魂代解绑 @群友"
    if not QQ_RE.match(token):
        return "", f"「{token}」不是有效 QQ 号（应为 5~12 位数字），或改用 @群友 的写法"
    return token, ""


# ---------------------------------------------------------------- /雀魂战绩 参数
#
# ★ 2026-09-22（A+D）：麻种参数支持常见简写；`全部` 让两个麻种各出一张卡。
#   放在本模块（纯逻辑）而非 main，是为了能离线单测（import main 需要 nonebot 已初始化）。

CAT_TOKENS: dict[int, tuple[str, ...]] = {
    1: ("四麻", "4麻", "四人麻", "四人麻将", "四人", "4人", "四"),
    2: ("三麻", "3麻", "三人麻", "三人麻将", "三人", "3人", "三"),
}
ALL_TOKENS: tuple[str, ...] = ("全部", "所有", "两麻", "两块", "both")


def parse_record_args(text: str, at_qq: str = "") -> tuple[str, int, bool]:
    """解析 `/雀魂战绩` 参数 → `(目标QQ, 麻种 0=自动, 是否要全部麻种)`（纯函数）。

    顺序任意：`@群友` / QQ 号 / `四麻·4麻·四人` / `三麻·3麻·三人` / `全部`。
    """
    target = str(at_qq or "").strip()
    cat, want_all = 0, False
    for tok in (text or "").split():
        if tok in ALL_TOKENS:
            want_all = True
            continue
        hit = next((k for k, names in CAT_TOKENS.items() if tok in names), 0)
        if hit:
            cat = hit
            continue
        if QQ_RE.match(tok):
            target = target or tok
    return target, cat, want_all


# ---------------------------------------------------------------- 绑定读写

def load() -> dict[str, Any]:
    data = _load_json(BINDINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("bindings", {})
    if not isinstance(data["bindings"], dict):
        data["bindings"] = {}
    return data


def save(data: dict[str, Any]) -> None:
    _save_json(BINDINGS_FILE, data)


def get(qq: str) -> dict[str, Any] | None:
    return load()["bindings"].get(str(qq))


def bind(qq: str, value: str, bind_type: str, group_id: str | None = None, *,
         account_id: int = 0, nickname: str = "") -> tuple[bool, str]:
    """写入/更新绑定（一人一号；重复绑定同群时刷新群列表）。"""
    data = load()
    qq = str(qq)
    cur = data["bindings"].get(qq)
    gid = str(group_id) if group_id else None

    if cur is not None:
        same = str(cur.get("majsoul_id")) == str(value)
        if same:
            if gid and gid not in cur.get("group_ids", []):
                cur.setdefault("group_ids", []).append(gid)
                data["bindings"][qq] = cur
                save(data)
                return True, "已把本群加入推送范围"
            return False, "你已经绑定过该雀魂账号了，如需更换请先 /雀魂解绑"
        return False, "你已经绑定过雀魂账号了（一人一号），如需更换请先 /雀魂解绑"

    data["bindings"][qq] = {
        "majsoul_id": str(value),
        "account_id": int(account_id or 0),
        "majsoul_nickname": nickname or "",
        "bind_type": bind_type,
        "group_ids": [gid] if gid else [],
        "bound_at": int(time.time()),
    }
    save(data)
    return True, "绑定成功"


def unbind(qq: str) -> tuple[bool, str]:
    data = load()
    qq = str(qq)
    if qq not in data["bindings"]:
        return False, "你还没有绑定雀魂账号，可发送 /雀魂绑定 <雀魂ID>"
    data["bindings"].pop(qq, None)
    save(data)
    return True, "已解绑"


def resolve_account(qq: str, account_id: int, nickname: str = "") -> None:
    """昵称绑定解析出 account_id 后回填。"""
    data = load()
    qq = str(qq)
    cur = data["bindings"].get(qq)
    if cur is None:
        return
    cur["account_id"] = int(account_id)
    if nickname:
        cur["majsoul_nickname"] = nickname
    data["bindings"][qq] = cur
    save(data)


def remove_group(qq: str, group_id: str) -> None:
    """把某个群从该绑定的推送范围移除。"""
    data = load()
    qq = str(qq)
    cur = data["bindings"].get(qq)
    if cur is None:
        return
    cur["group_ids"] = [g for g in cur.get("group_ids", []) if g != str(group_id)]
    data["bindings"][qq] = cur
    save(data)


# ---------------------------------------------------------------- 查询视图

def all_bindings() -> dict[str, dict[str, Any]]:
    return load()["bindings"]


def account_to_qq() -> dict[int, str]:
    """`{account_id: qq}`（仅含已解析出 ID 的绑定）。"""
    out: dict[int, str] = {}
    for qq, b in all_bindings().items():
        aid = int(b.get("account_id") or 0)
        if aid:
            out[aid] = qq
    return out


def qq_to_account() -> dict[str, int]:
    return {qq: int(b.get("account_id") or 0) for qq, b in all_bindings().items()}


def targets() -> list[tuple[str, int, list[str]]]:
    """待轮询清单：`[(qq, account_id, group_ids)]`（跳过未解析 ID 的绑定）。"""
    out = []
    for qq, b in all_bindings().items():
        aid = int(b.get("account_id") or 0)
        if aid:
            out.append((qq, aid, [str(g) for g in b.get("group_ids", [])]))
    return out


def groups_for_account(account_id: int) -> list[str]:
    for b in all_bindings().values():
        if int(b.get("account_id") or 0) == int(account_id):
            return [str(g) for g in b.get("group_ids", [])]
    return []
