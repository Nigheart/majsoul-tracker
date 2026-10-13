# -*- coding: utf-8 -*-
"""好友实时状态轮询与推送（Phase 2，业务核心，不直接依赖 nonebot）。

需求（用户 2026-09-19 拍板**方案 B**）：
- **对局开始提示**：好友 `state.playing` 出现 → 「群友 xxx 开始了 三麻·段位场（约 5 分钟后可观战）」
- **上下线通知**：好友 `state.is_online` 变化 → 「群友 xxx 上线了 / 下线了」（同一好友节流）
- **实时同室对战播报**：同群 ≥2 名绑定群友进入**同一自定义房**（`room_id>0`）且同时对局中
  → 合并为一条「xxx、xxx 在房间 xxxxx 决一死战！」（复用 `room_battle` 的判定与文案）

★ 准入门槛：`fetchFriendList` **只能看到小号的好友**，群友需先加小号好友（见 `/雀魂加好友`）。

数据文件：`plugin_data/majsoul_tracker/live_state.json`

```json
{
  "baseline": true,                                  // 首轮只建基线不推送
  "friends": {
    "<account_id>": {
      "nickname": "示例玩家",
      "online": true,
      "playing_uuid": "260919-...",
      "category": 2, "room_id": 0, "mode_id": 21,
      "last_online_notify": 1789740000,              // 上下线节流时间戳
      "notified_playing": ["<uuid>", ...]            // 已播报对局（去重）
    }
  }
}
```

推送出口通过 `sender(group_id, text)` 回调注入——便于离线单测（无需 nonebot）。
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from . import bindings, room_battle, yaku_map
from . import monitor as phase1          # ★ 复用 Phase 1 的限频与安全推送（同一群共享每分钟上限）
from . import provider_protocol as protocol
from .provider_protocol import FriendState, ProviderError

PLUGIN_NAME = "majsoul_tracker"
STATE_FILE = "live_state.json"
ONLINE_COOLDOWN = 300      # 同一好友上下线通知的最小间隔（秒），防反复掉线刷屏
PLAYING_KEEP = 40          # 每好友保留的已播报对局 uuid 数
SPECTATE_DELAY = 300       # 对局开始后约 5 分钟可在游戏内观战（用户 2026-09-18 口径）

Sender = Callable[[str, str], Awaitable[None]]


def _log(msg: str, *, level: str = "info", event: str = "live") -> None:
    """委托 `monitor._log`（统一出口 → stdout + 网页端运行日志）。"""
    phase1._log(msg, level=level, event=event)


# ---------------------------------------------------------------- 存储

def _load() -> dict[str, Any]:
    try:
        from web_admin.plugin_runtime import load_plugin_json
        data = load_plugin_json(PLUGIN_NAME, STATE_FILE, {})
    except Exception:  # noqa: BLE001 —— 离线/单测回退
        data = bindings._load_json(STATE_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("baseline", False)
    data.setdefault("friends", {})
    return data


def _save(data: dict[str, Any]) -> None:
    try:
        from web_admin.plugin_runtime import save_plugin_json
        save_plugin_json(PLUGIN_NAME, STATE_FILE, data)
    except Exception:  # noqa: BLE001
        bindings._save_json(STATE_FILE, data)


# ---------------------------------------------------------------- 文案

def format_online(display: str, online: bool) -> str:
    """上下线提示。"""
    return (f"🀄 雀魂 · 在线状态\n"
            f"群友「{display}」{'上线了' if online else '下线了'}")


def _fmt_clock(ts: int) -> str:
    """时间戳 → `HH:MM`（当日时刻，用于开局/可观战时间）。"""
    try:
        return time.strftime("%H:%M", time.localtime(int(ts)))
    except Exception:  # noqa: BLE001
        return "-"


def _place_text(mode_id: int, room_id: int, mode: int = 0) -> str:
    """场次文案（`mode_id` 决定场次/麻种；`room_id>0` 追加房间号）。"""
    where = yaku_map.place_name(mode_id, room_id, mode)
    if room_id and int(room_id) > 0:
        where = f"{where}（房间 {room_id}）"
    return where


def format_playing_start(display: str, mode_id: int, room_id: int, mode: int = 0,
                         start_time: int = 0, approx: bool = False) -> str:
    """对局开始提示（★ 2026-09-21 重写：精确场次 + 开局时间 + 可观战时间）。

    旧版把 `playing.category` 当麻种用 → 实测该字段其实是「对局类别」（2=段位场），
    于是「玉之间·四人南」被写成「段位场·三麻」（用户 2026-09-21 反馈的线上 bug）。
    现在场次名完全由 `mode_id` 决定（`yaku_map.place_name`）；`start_time` = 该局开局时间戳。

    `approx=True` 表示时间来自**检测时刻兜底**（未能从 `fetchGameLiveList` 的 ~5 分钟窗口
    取到精确开局时间）→ 文案前缀「约」，避免给出假精确值（用户要求必须有时间，不能只剩
    「约 5 分钟后可观战」）。
    """
    where = _place_text(mode_id, room_id, mode)
    lines = ["🀄 雀魂 · 对局开始", f"群友「{display}」开始了 {where}"]
    if start_time:
        head = "约 " if approx else ""
        lines.append(f"开局 {head}{_fmt_clock(start_time)}"
                     f" · 约 {_fmt_clock(int(start_time) + SPECTATE_DELAY)} 后可观战")
    else:
        lines.append(f"（约 {SPECTATE_DELAY // 60} 分钟后可观战）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 纯函数（可单测）

def diff_events(prev: dict[str, dict], curr: list[FriendState], *,
                now: float, online_cooldown: int = ONLINE_COOLDOWN,
                online_enabled: bool = True) -> list[dict[str, Any]]:
    """对比前后快照 → 产出待推送事件（★纯函数，便于离线单测）。

    - `online`：`is_online` 发生变化，且距上次通知 ≥ `online_cooldown`
    - `playing`：出现**新的** `game_uuid`（按 `notified_playing` 去重）
    """
    events: list[dict[str, Any]] = []
    for st in curr:
        old = prev.get(str(st.account_id)) or {}
        if online_enabled:
            was = old.get("online")
            if was is not None and bool(was) != bool(st.online):
                if now - float(old.get("last_online_notify") or 0) >= online_cooldown:
                    events.append({"kind": "online", "state": st, "online": st.online})
        if st.playing_uuid and st.playing_uuid not in (old.get("notified_playing") or []):
            events.append({"kind": "playing", "state": st})
    return events


def to_playing_states(curr: list[FriendState], qq_of: dict[int, str],
                      groups_of: dict[int, list[str]]) -> list[room_battle.PlayingState]:
    """把「对局中」的好友快照转成 `room_battle.PlayingState`（复用同室判定）。"""
    out: list[room_battle.PlayingState] = []
    for st in curr:
        if not st.playing_uuid:
            continue
        out.append(room_battle.PlayingState(
            qq=qq_of.get(st.account_id, str(st.account_id)),
            account_id=st.account_id,
            nickname=st.nickname,
            group_ids=list(groups_of.get(st.account_id) or []),
            game_uuid=st.playing_uuid,
            category=st.category,
            mode_id=st.mode_id,
            room_id=st.room_id,
        ))
    return out


def _snapshot(prev: dict[str, dict], curr: list[FriendState]) -> None:
    """把当前快照写回存储（含已播报对局登记）。"""
    for st in curr:
        rec = prev.setdefault(str(st.account_id), {})
        rec["nickname"] = st.nickname
        rec["online"] = bool(st.online)
        rec["playing_uuid"] = st.playing_uuid
        rec["category"] = st.category
        rec["room_id"] = st.room_id
        rec["mode_id"] = st.mode_id
        if st.playing_uuid:
            uuids = list(rec.get("notified_playing") or [])
            if st.playing_uuid not in uuids:
                uuids = (uuids + [st.playing_uuid])[-PLAYING_KEEP:]
            rec["notified_playing"] = uuids


# ---------------------------------------------------------------- 主流程

async def tick(sender: Sender, *, account: str = "", password: str = "",
               online_enabled: bool = True,
               online_cooldown: int = ONLINE_COOLDOWN,
               rate_limit: int = 5,
               room_battle_enabled: bool = True,
               image_cards: bool = False, card_theme: str = "dark",
               name_resolver=None) -> dict[str, Any]:
    """执行一轮好友状态轮询：拉快照 → 比对 → 推送。

    `image_cards`/`card_theme`/`name_resolver` 由 main 注入（同 Phase 1）；
    ★ 上下线通知**始终纯文本**（一句话配图只会更刷屏，用户 2026-09-22 口径）。

    返回统计 `{checked, bound, online_events, playing_events, room_battles,
    pushed, skipped_rate, errors}`。
    """
    stat: dict[str, Any] = {"checked": 0, "bound": 0, "online_events": 0,
                            "playing_events": 0, "room_battles": 0,
                            "pushed": 0, "skipped_rate": 0, "errors": [],
                            "no_group": 0, "skipped_activity": 0}
    targets = bindings.targets()
    if not targets:
        return stat

    protocol.configure(account, password)
    try:
        friends = await protocol.fetch_friend_list()
    except ProviderError as e:
        stat["errors"].append(str(e))
        _log(f"好友状态查询失败：{e}")
        return stat

    stat["checked"] = len(friends)
    qq_of = {int(aid): qq for qq, aid, _g in targets}
    groups_of = {int(aid): bindings.groups_for_account(aid) for _q, aid, _g in targets}
    mine = [f for f in friends if f.account_id in qq_of]
    stat["bound"] = len(mine)
    if not mine:
        _log(f"小号好友 {len(friends)} 人，其中绑定群友 0 人"
             "（群友需先加小号好友才能被监控，见 /雀魂加好友）")
        return stat

    data = _load()
    prev = data["friends"]

    if not data.get("baseline"):
        # 首轮建基线：登记现状、不推送（避免刚开启就刷一屏历史状态）
        _snapshot(prev, mine)
        data["baseline"] = True
        _save(data)
        _log(f"建立实时基线：好友 {len(friends)} 人 / 绑定群友 {len(mine)} 人（首轮不推送）")
        return stat

    now = time.time()
    events = diff_events(prev, mine, now=now,
                         online_cooldown=online_cooldown,
                         online_enabled=online_enabled)

    # ★ 2026-09-26 用户口径「**所有活动局进行屏蔽，不做处理和汇报**」——实时快照只有
    #   `mode_id`（拿不到摘要的 `tag`），故按 `yaku_map.is_activity_playing` **保守判定**：
    #   「有 mode_id 但不在已知段位场表」即活动/其它场（实测修罗之战 `mode_id=40`）。
    #   被过滤的事件**仍由末尾 `_snapshot()` 登记进 `notified_playing`**（去重），
    #   所以下一轮不会重复判定、也不会重复刷日志。
    #   ★ 顺带好处：不再对活动局调用 `find_live_start_time`（活动场无 live 列表，
    #     实测 `filter_id=240` 返回 `code=8`，之前每局刷两条失败日志）。
    activity_events: list[dict[str, Any]] = []
    if events:
        kept: list[dict[str, Any]] = []
        for ev in events:
            st0: FriendState = ev["state"]
            if ev["kind"] == "playing" and yaku_map.is_activity_playing(st0.mode_id, st0.room_id):
                activity_events.append(ev)
                continue
            kept.append(ev)
        events = kept
    if activity_events:
        stat["skipped_activity"] = len(activity_events)
        detail = "、".join(f"{ev['state'].nickname or ev['state'].account_id}"
                           f"(mode_id={ev['state'].mode_id})" for ev in activity_events[:5])
        _log(f"跳过 {len(activity_events)} 个活动/其它场对局开始事件"
             f"（mode_id 不在已知段位场表；用户口径：活动局不汇报）：{detail}")

    # 实时同室对战：同群 + 同一自定义房（room_id>0）+ 同轮快照内同时对局中
    battles: list[room_battle.RoomBattle] = []
    if room_battle_enabled:
        battles = room_battle.detect_room_battle(to_playing_states(mine, qq_of, groups_of))
    in_battle = {m.account_id for b in battles for m in b.members}

    # ★ 限频与 Phase 1 共用同一计数器：同一群每分钟总上限，防两通道叠加刷屏
    p1 = phase1._load()

    # ★ 2026-09-21：开局时间只能从 `fetchGameLiveList` 取（「最近 ~5 分钟开局」滚动窗口）→
    #   只对本轮要播报的 playing 事件查，且每局最多 1 次调用（只读列表，风险低）。
    #   ⚠️ 该窗口只有 ~5 分钟：若检测到对局时已开局更久（或列表接口异常），会查不到 →
    #   **回退为本轮检测时刻**（误差 ≤ live_interval），保证文案一定有「开局/可观战时间」。
    start_times: dict[str, tuple[int, bool]] = {}      # uuid → (时间戳, 是否精确)
    for ev in events:
        if ev["kind"] != "playing":
            continue
        uid = ev["state"].playing_uuid
        if not uid or uid in start_times:
            continue
        exact = 0
        try:
            exact = await protocol.find_live_start_time(uid, ev["state"].mode_id)
        except Exception as e:  # noqa: BLE001 —— 拿不到精确时间不影响推送
            _log(f"查询开局时间失败（{uid}）：{type(e).__name__}: {e}")
        if exact:
            start_times[uid] = (int(exact), True)
        else:
            start_times[uid] = (int(now), False)
            _log(f"未从对局列表取到 {uid} 的精确开局时间（mode_id={ev['state'].mode_id}，"
                 f"该列表仅覆盖最近约 5 分钟开局的局）→ 文案回退为检测时刻")

    for b in battles:
        if not phase1._rate_allow(p1, b.group_id, rate_limit):
            stat["skipped_rate"] += 1
            _log(f"群 {b.group_id} 达到限频，跳过同室播报")
            continue
        who = "、".join(f"「{m.nickname or m.qq}」" for m in b.members)
        image = await phase1._render_card("room", {
            "room_id": b.room_id,
            "title": f"{who} 在房间 {b.room_id} 决一死战！",
            "players": [{"qq": m.qq, "name": m.nickname or m.qq} for m in b.members],
            "note": "（约 5 分钟后可在游戏内观战）",
        }, image_cards, card_theme)
        await phase1._safe_send(sender, b.group_id,
                                room_battle.format_room_battle_start(b), stat, image=image)
        stat["room_battles"] += 1
        _log(f"同室对战播报：房间 {b.room_id} / 群 {b.group_id} / {len(b.members)} 人")

    for ev in events:
        st: FriendState = ev["state"]
        if ev["kind"] == "playing" and st.account_id in in_battle:
            continue        # 已由同室播报覆盖，不再推普通「开始了对局」
        qq = str(qq_of.get(st.account_id, ""))
        display = ((bindings.get(qq) or {}).get("majsoul_nickname")
                   or st.nickname or qq)
        st_ts, st_exact = start_times.get(st.playing_uuid, (0, False))
        if ev["kind"] == "online":
            text = format_online(display, bool(ev["online"]))
        else:
            text = format_playing_start(display, st.mode_id, st.room_id,
                                        start_time=st_ts, approx=not st_exact)
        gids = list(groups_of.get(st.account_id) or [])
        if not gids:
            # ★ 2026-09-21：检测到事件却没有推送群 → 不推送也不留痕（线上静默根因之一）
            stat["no_group"] += 1
            _log(f"{display}（{st.account_id}）有"
                 f"{'上下线' if ev['kind'] == 'online' else '对局开始'}事件，"
                 f"但未加入任何推送群 → 未推送；请在目标群里发一次 "
                 f"/雀魂绑定 {st.account_id}", level="warn")
        for gid in gids:
            if not phase1._rate_allow(p1, gid, rate_limit):
                stat["skipped_rate"] += 1
                continue
            image = None
            if ev["kind"] == "playing":       # ★ 上下线保持纯文本
                nick = await phase1._display_name(qq, gid, name_resolver)
                image = await phase1._render_card("playing", {
                    "qq": qq,
                    "nickname": nick or display,
                    "place": _place_text(st.mode_id, st.room_id),
                    "start_time": st_ts,
                    "approx": not st_exact,
                }, image_cards, card_theme)
            await phase1._safe_send(sender, gid, text, stat, image=image)
        if ev["kind"] == "online":
            stat["online_events"] += 1
            prev.setdefault(str(st.account_id), {})["last_online_notify"] = int(now)
        else:
            stat["playing_events"] += 1

    phase1._save(p1)
    _snapshot(prev, mine)
    _save(data)
    return stat


def stats() -> dict[str, Any]:
    """运维视图（供 `/雀魂查看` 或排障）。"""
    data = _load()
    fs = data["friends"]
    return {
        "baseline": bool(data.get("baseline")),
        "friends": len(fs),
        "online": sum(1 for v in fs.values() if v.get("online")),
        "playing": sum(1 for v in fs.values() if v.get("playing_uuid")),
    }


async def reset_baseline() -> None:
    """清除实时基线（下次轮询重新建立，不推送）。"""
    data = _load()
    data["baseline"] = False
    _save(data)
