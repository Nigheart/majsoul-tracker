# -*- coding: utf-8 -*-
"""雀魂账号状态监控插件——命令入口 + 定时轮询 + 推送出口。

命令：
- `/雀魂绑定 <雀魂ID|昵称>`：绑定（在群里发即把本群加入推送范围）
- `/雀魂解绑`：解除绑定
- `/雀魂查看`：查看绑定与监控状态
- `/雀魂代绑定 <QQ或@群友> <雀魂ID|昵称>`：**管理员**替群友绑定（超管 / 群主 / 群管理员）
- `/雀魂代解绑 <QQ或@群友>`：**管理员**解除群友绑定

配置：全部在 **网页端插件管理** 中设置（`plugin.json` 的 `settings`），
密码走 `secret`（`plugin_data/majsoul_tracker/secrets.json`，不回显）。
"""
from __future__ import annotations

import asyncio
import time

from nonebot import get_bots, on_command
from nonebot.adapters.onebot.v11 import (Bot, GroupMessageEvent, Message,
                                         MessageEvent, MessageSegment)
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from . import (ai_service, bindings, card, friend_applies, friend_monitor, majsoul_id, monitor,
               paipu_parser, permissions, provider_thirdparty, rank_level)
from . import provider_protocol as protocol
from .provider_protocol import ProviderError

PLUGIN_NAME = "majsoul_tracker"
JOB_ID = "plugins.majsoul_tracker.tick"
LIVE_JOB_ID = "plugins.majsoul_tracker.live_tick"

# 两套 ID 的中文说明（回显用）：★ 客户端显示的是 friend_id，协议/牌谱屋用的是 account_id
_KIND_CN = {"friend_id": "客户端加好友 ID", "account_id": "雀魂账号 ID"}

DEFAULTS: dict = {
    "poll_interval": 180,
    "push_rate_limit": 5,
    "provider": "protocol",
    "first_bind_backfill": False,
    "live_enabled": True,
    "live_interval": 120,
    "live_online_enabled": True,
    "live_online_cooldown": 300,
    "room_battle_enabled": True,
    "long_conn_enabled": False,
    # ★ 2026-09-21：日志心跳（平静期也写状态日志，便于网页端确认插件存活）
    "heartbeat_interval": 300,
    # Feature-04：对局结果增强评论（段位分变化 / 升降段临界 / 连续顺位 / 击飞）
    "commentary_enabled": True,
    "commentary_gap_levels": "200,100,50",
    "commentary_streak_repeat": 3,
    "commentary_streak_absent": 5,
    # ★ 2026-09-22 图片卡：对局结果 / 同室战报 / 对局开始 附 PNG 卡片（失败自动回退纯文本）
    "image_cards": True,
    "card_theme": "dark",
    # ★ 2026-10-28 新增（用户要求）：信息内是否附带牌谱链接；关闭 = 插件发出的**任何**消息
    #   都不带牌谱链接（推送 + /雀魂牌谱 回复），查询与 AI 复盘功能本身不受影响。
    "paipu_links_enabled": True,
}

# ★ 2026-09-28（Feature-07）牌谱查询用法提示（解析不出 uuid 时回给用户）
_PAIPU_USAGE = (
    "用法：/雀魂牌谱 <牌谱链接>\n"
    "牌谱链接＝雀魂里「对局详情 → 分享牌谱」复制出来的那条，形如：\n"
    "https://game.maj-soul.com/1/?paipu=<uuid>_a<match_id>\n"
    "（也可以直接发链接里 `paipu=` 后面那段牌谱 ID）"
)

# ★ 2026-09-29（Feature-08）AI 复盘用法提示 + 「同一局正在分析」去重表
_AI_USAGE = (
    "用法：/雀魂分析 <牌谱链接> [详细]\n"
    "会用自建的麻将 AI（akochan）逐手复盘这局：一致率 + 关键失误手（实际打什么 vs AI 建议打什么）+ 危险牌。\n"
    "牌谱链接同 /雀魂牌谱；分析较慢（单局约 1~2 分钟、整场可能十几分钟），"
    "提交后我会在跑完后把结果发过来。\n"
    "加「详细」二字（如 `/雀魂分析 <链接> 详细`）＝列出全部失误与危险牌，图片会很长。\n"
    "（同一局第二次分析会直接命中本地缓存，不再重跑；概览与详细共用同一份缓存）"
)
# ★ 2026-09-29（用户要求）：插件帮助命令 —— 与上级插件同风格（`深渊帮助` / `挖宝帮助` / `探险帮助`）
#   口径：一条文本讲清「能干什么 + 怎么用」，≤500 字（推送合规）；新增命令时**必须同步这里**
#   （回归 `_probe/test_help.py` 会扫源码比对，漏更新就报错）。
_HELP_TEXT = (
    "🀄 雀魂插件命令（绑定后自动推送对局结果）\n"
    "▍绑定\n"
    "/雀魂绑定 <雀魂ID 或 昵称> — 绑定你的雀魂账号（客户端里显示的 ID 即可）\n"
    "/雀魂解绑 — 解除绑定\n"
    "/雀魂查看 — 查看自己的绑定信息与最近战绩\n"
    "▍查询\n"
    "/雀魂战绩 [@群友] [四麻|三麻|全部] — 战绩卡（顺位走势 / 段位分 / 统计）\n"
    "/雀魂牌谱 <牌谱链接> — 这一局的明细（最大一手 / 最大放铳 / 全玩家统计）\n"
    "/雀魂分析 <牌谱链接> [详细] — 麻将 AI 逐手复盘（一致率 / 失误 / 危险牌）；"
    "加「详细」＝列出全部失误与危险牌，图片会很长\n"
    "▍实时状态（需先成为小号好友）\n"
    "/雀魂加好友 — 回显小号 ID，去游戏里加它，即可解锁「对局开始 / 上下线 / 同室对战」提示\n"
    "/雀魂重新申请好友 — 没收到好友申请时重发一次\n"
    "▍管理员\n"
    "/雀魂代绑定 <QQ或@群友> <雀魂ID|昵称>\n"
    "/雀魂代解绑 <QQ或@群友>\n"
    "提示：牌谱链接＝雀魂「对局详情 → 分享牌谱」复制的那条；AI 分析较慢，跑完自动发结果。"
)
_DETAIL_LIMIT = 400             # 详细模式的列出上限（与 ai_service._DETAIL_LIMIT 对齐）
_DETAIL_TEXT_MAX = 1200         # 详细模式「无图兜底」时每条文本的字数上限（超长分段）
_AI_RUNNING: set[str] = set()          # 正在分析的牌谱 uuid（防重复提交）


# ---------------------------------------------------------------- 配置读取

def _settings() -> dict:
    """读取插件设置（网页端配置，含默认值兜底）。"""
    data: dict = {}
    try:
        from web_admin.plugin_runtime import plugin_settings
        data = plugin_settings(PLUGIN_NAME) or {}
    except Exception:  # noqa: BLE001
        data = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in data.items() if v is not None})
    return out


def _credentials() -> tuple[str, str]:
    """读取雀魂小号凭据：账号来自普通设置，密码来自 Secret。"""
    account = str(_settings().get("majsoul_account") or "").strip()
    password = ""
    try:
        from web_admin.plugin_runtime import get_plugin_secret
        password = get_plugin_secret(PLUGIN_NAME, "majsoul_password", "")
    except Exception:  # noqa: BLE001
        password = ""
    return account, password


def _log(msg: str, *, level: str = "info", event: str = "plugin") -> None:
    """委托 `monitor._log`（统一出口 → stdout + 网页端运行日志）。"""
    monitor._log(msg, level=level, event=event)


# ---------------------------------------------------------------- 推送出口

async def send_group(group_id: str, text: str, image: bytes | None = None) -> None:
    """推送出口：发给指定群（多 bot 时取第一个在线实例）。

    `image` = 图片卡 PNG bytes（`nonebot` 会自动转 `base64://` 内联发送，不依赖文件系统）；
    **发图失败自动回退纯文本**——图片只是增强，绝不能因此丢消息。
    """
    bots = get_bots()
    bot = next(iter(bots.values()), None)
    if bot is None:
        raise RuntimeError("机器人未连接，无法推送")
    if image:
        try:
            await bot.send_group_msg(
                group_id=int(group_id),
                message=Message([MessageSegment.image(file=image),
                                 MessageSegment.text(text)]))
            return
        except Exception as e:  # noqa: BLE001 —— 发图失败不影响文本
            _log(f"发送图片卡失败，回退纯文本：{type(e).__name__}: {e}")
    await bot.send_group_msg(group_id=int(group_id), message=text)


async def send_to_chat(bot: Bot, chat_type: str, chat_id: str, text: str,
                       image: bytes | None = None) -> None:
    """**后台任务专用**出口：把结果发回「收到命令的那个会话」（群或私聊）。

    与 `send_group` 的区别：① 支持私聊；② 用**指定 bot 实例**（后台任务拿不到 matcher，
       多 bot 场景下必须用它自己那个号发，否则会发错号）；③ 发图失败自动回退纯文本。
    """
    async def _do(m: Message) -> None:
        if chat_type == "group":
            await bot.send_group_msg(group_id=int(chat_id), message=m)
        else:
            await bot.send_private_msg(user_id=int(chat_id), message=m)

    if image:
        try:
            await _do(Message([MessageSegment.image(file=image), MessageSegment.text(text)]))
            return
        except Exception as e:  # noqa: BLE001 —— 发图失败不影响文本
            _log(f"发送卡片失败，回退纯文本：{type(e).__name__}: {e}")
    await _do(Message(text))


def _split_text(text: str, limit: int) -> list[str]:
    """把长文本按行切成每段 ≤ `limit` 字（单行超长时硬切）——详细模式无图兜底用。"""
    out: list[str] = []
    cur = ""
    for line in (text or "").splitlines():
        while len(line) > limit:                     # 单行超长：硬切
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) <= limit:
            cur += "\n" + line
        else:
            out.append(cur)
            cur = line
    if cur:
        out.append(cur)
    return out or [""]


# ---------------------------------------------------------------- QQ 昵称

_NICK_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_NICK_TTL = 24 * 3600          # QQ 昵称/群名片缓存 24 小时，避免频繁调接口


async def resolve_qq_names(qqs, group_id: str = "") -> dict[str, str]:
    """取群友显示名：**群名片 → 群昵称 → 好友昵称**（带 24h 缓存）。

    失败或无可用 bot 时返回空 dict，调用方回退用雀魂昵称（推送绝不因取昵称失败而中断）。
    """
    out: dict[str, str] = {}
    bots = get_bots()
    bot = next(iter(bots.values()), None)
    if bot is None:
        return out
    now = time.time()
    for qq in (qqs or []):
        qq = str(qq)
        key = (str(group_id), qq)
        hit = _NICK_CACHE.get(key)
        if hit and now - hit[1] < _NICK_TTL:
            if hit[0]:
                out[qq] = hit[0]
            continue
        name = ""
        try:
            if group_id:
                info = await bot.get_group_member_info(group_id=int(group_id),
                                                       user_id=int(qq), no_cache=False)
                name = str((info or {}).get("card") or (info or {}).get("nickname") or "")
            if not name:
                info = await bot.get_stranger_info(user_id=int(qq), no_cache=False)
                name = str((info or {}).get("nickname") or "")
        except Exception as e:  # noqa: BLE001
            _log(f"取 QQ {qq} 昵称失败：{type(e).__name__}: {e}")
        _NICK_CACHE[key] = (name, now)
        if name:
            out[qq] = name
    return out


# ---------------------------------------------------------------- 命令

bind_cmd = on_command("雀魂绑定", priority=5, block=True)
unbind_cmd = on_command("雀魂解绑", priority=5, block=True)
me_cmd = on_command("雀魂查看", priority=5, block=True)
# ★ 2026-09-22（P2）：战绩查询（自己 / @群友 / QQ，可指定麻种）
record_cmd = on_command("雀魂战绩", priority=5, block=True)
# ★ 2026-09-28（Feature-07）：牌谱链接 → 对局明细（命令式；用户拍板「只做命令」）
paipu_cmd = on_command("雀魂牌谱", priority=5, block=True)
ai_cmd = on_command("雀魂分析", priority=5, block=True)          # ★ Feature-08：AI 复盘
help_cmd = on_command("雀魂帮助", priority=5, block=True)        # ★ 2026-09-29：插件帮助
addfriend_cmd = on_command("雀魂加好友", priority=5, block=True)
refriend_cmd = on_command("雀魂重新申请好友", priority=5, block=True)
# ★ 2026-09-21（用户要求）：管理员代绑定/代解绑（超管 或 群主/群管理员）
bindfor_cmd = on_command("雀魂代绑定", priority=5, block=True)
unbindfor_cmd = on_command("雀魂代解绑", priority=5, block=True)


async def _resolve_bind_input(matcher: Matcher, text: str) -> dict | None:
    """解析 + 核对绑定输入（**自助绑定与管理员代绑定共用**）。

    成功返回 `{value, btype, account_id, nickname, cand_note, picked_kind}`；
    校验/查询失败时已通过 `matcher.finish()` 回复，返回 `None`。
    """
    value, btype, err = bindings.parse_input(text)
    if err:
        await matcher.finish(err)

    account_id, nickname = 0, ""
    cand_note: list[str] = []
    picked_kind = ""
    if btype in (bindings.BIND_TYPE_ACCOUNT_ID, bindings.BIND_TYPE_FRIEND_ID):
        account, password = _credentials()
        if not account or not password:
            await matcher.finish("机器人尚未配置雀魂小号（请在网页端插件设置里填写邮箱与密码）")
        protocol.configure(account, password)  # ★ 绑定核对同样要注入凭据，否则登录层报「未配置」

        raw_id = int(value)
        if btype == bindings.BIND_TYPE_FRIEND_ID:
            conv = majsoul_id.fri2account(raw_id)
            if conv <= 0:
                await matcher.finish(
                    f"「{raw_id}」不像雀魂加好友 ID（应 ≥ {majsoul_id.MIN_FRIEND_ID}）。\n"
                    "请在雀魂客户端「个人资料」里复制 ID，或改用 account:<账号ID>")
            cands = [(conv, bindings.BIND_TYPE_FRIEND_ID)]
        else:
            # ★ 纯数字可能是两套 ID（客户端 friend_id / 协议 account_id），且两者可能撞车，
            #   故展开候选后按「能否查到对局」裁决；friend_id 解释优先（用户最常复制客户端 ID）
            cands = majsoul_id.candidates(raw_id)
        if not cands:
            await matcher.finish("ID 格式不对，请填纯数字雀魂 ID")

        await matcher.send(f"正在核对雀魂 ID {raw_id} …")
        picked: tuple[int, str, str] | None = None
        for acc, kind in cands:
            try:
                games = await protocol.fetch_recent_games(acc, limit=1)
            except ProviderError as e:
                cand_note.append(f"· {acc}（{_KIND_CN[kind]}）：查询失败（{e}）")
                continue
            nick = _nickname_from_games(games, acc) or ""
            cand_note.append(f"· {acc}（{_KIND_CN[kind]}）：{nick or '最近没有对局，取不到昵称'}")
            if nick and picked is None:
                picked = (acc, nick, kind)
        if picked is None:
            acc, kind = cands[0]
            picked = (acc, "", kind)
        account_id, nickname, picked_kind = picked
        _log(f"绑定核对 {raw_id}：候选 {cands} → 选中 {account_id}"
             f"（{_KIND_CN.get(picked_kind, picked_kind)}，昵称「{nickname or '未取到'}」）")
    else:  # 昵称 → 用免登录牌谱屋解析成 ID
        await matcher.send(f"正在查询昵称「{value}」…")
        try:
            cands = await provider_thirdparty.search_player(value)
        except ProviderError as e:
            await matcher.finish(f"昵称查询失败：{e}\n可改用雀魂 ID 绑定，例如：/雀魂绑定 12345678")
        exact = [c for c in cands if c["nickname"] == value]
        pick = exact or cands
        if not pick:
            await matcher.finish(f"没找到昵称「{value}」对应的雀魂账号。\n"
                                 "请确认昵称拼写，或改用雀魂 ID 绑定（更可靠）")
        if len(pick) > 1:
            top = pick[:5]
            lines = [f"昵称「{value}」匹配到多个账号，请改用雀魂 ID 绑定："]
            for c in top:
                lines.append(f"· {c['nickname']}（ID {c['account_id']}）")
            await matcher.finish("\n".join(lines))
        account_id = int(pick[0]["account_id"])
        nickname = pick[0]["nickname"]

    return {"value": value, "btype": btype, "account_id": account_id,
            "nickname": nickname, "cand_note": cand_note, "picked_kind": picked_kind}


async def _maybe_apply_friend(account_id: int, nickname: str) -> str:
    """首次绑定成功后自动发起雀魂好友申请（路线 2，2026-09-20 用户拍板）→ 回执文案。

    - 仅在**首次建立绑定**时调用（重复绑定/加入新群不重复发）；
    - 以 account_id 持久化「已申请」，等待对方同意期间不会重复发（2026-09-20 第二轮）；
    - 失败降级为可读提示，不阻断绑定。
    """
    friend_note = ""
    try:
        account, password = _credentials()
        if not account or not password:
            return "（小号未配置，跳过自动加好友；可发 /雀魂加好友 手动加）"
        protocol.configure(account, password)
        friends = await protocol.fetch_friend_list()
        friend_ids = {f.account_id for f in friends}
        friend_applies.prune(friend_ids)          # 已成为好友的，清掉申请记录
        is_friend = account_id in friend_ids
        action, note = friend_applies.plan(
            is_friend, friend_applies.applied_at(account_id),
            nickname=nickname, account_id=account_id)
        if action == "send":
            await protocol.apply_friend(account_id)
            friend_applies.record(account_id)
        elif action == "already":
            friend_applies.clear(account_id)
        friend_note = note
    except ProviderError as e:
        _log(f"自动加好友失败（{account_id}）：{e}")
        friend_note = f"自动加好友未成功（{e}）；可发 /雀魂加好友 手动添加"
    return friend_note


def _at_qq(event: MessageEvent) -> str:
    """取消息里第一个 @ 的 QQ（无则空串；@全体 忽略）。"""
    try:
        for seg in event.get_message():
            if seg.type == "at":
                qq = str(seg.data.get("qq") or "")
                if qq and qq != "all":
                    return qq
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _finish_card(matcher: Matcher, text: str, images) -> None:
    """回复「图片卡（可多张）+ 文本」；无图或发送失败时回退纯文本（图片只是增强）。"""
    if isinstance(images, (bytes, bytearray)):
        imgs = [bytes(images)]
    else:
        imgs = [b for b in (images or []) if b]
    if imgs:
        try:
            segs = [MessageSegment.image(file=b) for b in imgs]
            segs.append(MessageSegment.text(text))
            await matcher.finish(Message(segs))
            return
        except FinishedException:
            raise                      # finish 成功后的正常终止
        except Exception as e:  # noqa: BLE001
            _log(f"发送图片卡失败，回退纯文本：{type(e).__name__}: {e}")
    await matcher.finish(text)


# ★ A+D：参数解析在 `bindings.parse_record_args`（纯逻辑，可离线单测）


def _cat_cn(cat: int) -> str:
    """麻种中文名。"""
    return {1: "四麻", 2: "三麻"}.get(int(cat or 0), "对局")


def _record_summary_lines(h: dict, *, multi: bool = False) -> list[str]:
    """战绩文本行；`multi=True`（全部麻种）时每段带麻种前缀且压成 2 行，便于上下并读。"""
    st = h["summary"]
    cn = _cat_cn(h["category"])
    out: list[str] = []
    lid = int(h["level_id"] or 0)
    cur = int(h["level_score"] or 0)
    up = rank_level.upgrade_score(lid) if lid else None
    if h["level_name"]:
        seg = f"{cn}：" if multi else "段位："
        seg += f"{h['level_name']} {cur}"
        if up:
            seg += f" / {up}（距升段 {max(0, up - cur)} 分）"
        out.append(seg)
    head = f"{cn}（{st['games']} 局）：" if multi else f"最近 {st['games']} 局："
    stat_line = (f"{head}平均顺位 {st['avg_rank']:.2f}"
                 f" · 一位率 {st['win_rate'] * 100:.0f}%"
                 f" · 连对率 {st['top2_rate'] * 100:.0f}%")
    tail = f"最长连一位 {st['best_streak']}"
    if st.get("has_levels"):
        tail += f" · 段位分变化 {st['level_delta']:+d}"
    if multi:
        out.append(f"{stat_line} · {tail}")
    else:
        out.append(stat_line)
        out.append(tail)
    return out


@bind_cmd.handle()
async def _(bot: Bot, event: MessageEvent, matcher: Matcher, args: Message = CommandArg()):
    r = await _resolve_bind_input(matcher, args.extract_plain_text().strip())
    if r is None:
        return
    qq = str(event.user_id)
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    value, account_id, nickname = r["value"], r["account_id"], r["nickname"]
    cand_note, picked_kind = r["cand_note"], r["picked_kind"]

    ok, msg = bindings.bind(qq, value, r["btype"], group_id,
                            account_id=account_id, nickname=nickname)
    if not ok:
        await matcher.finish(msg)

    friend_note = await _maybe_apply_friend(account_id, nickname) if msg == "绑定成功" and account_id else ""

    lines = [f"✅ {msg}",
             f"雀魂账号：{nickname or '(未获取)'}（ID {account_id}）",
             f"推送范围：{'本群 ' + group_id if group_id else '未设置（请在群里再发一次本命令）'}",
             "对局结束后会自动把结果推送到群里。"]
    if friend_note:
        lines.append(friend_note)
    if len(cand_note) > 1:
        lines.append(f"（你输入的 {value} 有两种解释，已按「{_KIND_CN.get(picked_kind, picked_kind)}」绑定）")
        lines.extend(cand_note)
        lines.append("若选错了，请用 /雀魂绑定 account:<账号ID> 或 /雀魂绑定 friend:<加好友ID> 明确指定。")
    lines.append("⚠️ 请核对上面的雀魂昵称是否为你要监控的账号；若不对，请先 /雀魂解绑 再重新绑定。")
    await matcher.finish("\n".join(lines))


@bindfor_cmd.handle()
async def _(bot: Bot, event: MessageEvent, matcher: Matcher, args: Message = CommandArg()):
    """/雀魂代绑定 <QQ或@群友> <雀魂ID|昵称>：管理员替群友绑定（超管 / 群主 / 群管理员）。"""
    if not permissions.is_admin(user_id=str(event.user_id), role=permissions.role_of(event)):
        await matcher.finish(permissions.deny_text())

    target, value_text, err = bindings.parse_admin_args(args.extract_plain_text(), _at_qq(event))
    if err:
        await matcher.finish(err)

    r = await _resolve_bind_input(matcher, value_text)
    if r is None:
        return

    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    account_id, nickname = r["account_id"], r["nickname"]
    ok, msg = bindings.bind(target, r["value"], r["btype"], group_id,
                            account_id=account_id, nickname=nickname)
    if not ok:
        cur = bindings.get(target) or {}
        await matcher.finish(
            f"❌ {msg}\n"
            f"QQ {target} 当前绑定：{cur.get('majsoul_nickname') or '(未获取)'}"
            f"（ID {cur.get('account_id') or '未解析'}）\n"
            f"如需更换账号，请先 /雀魂代解绑 {target}")
    _log(f"管理员 {event.user_id} 代绑定：QQ {target} → {account_id}"
         f"（{nickname or '未取到昵称'}）")

    friend_note = await _maybe_apply_friend(account_id, nickname) if msg == "绑定成功" and account_id else ""

    lines = [f"✅ 已为 QQ {target} 绑定",
             f"雀魂账号：{nickname or '(未获取)'}（ID {account_id}）",
             f"推送范围：{'本群 ' + group_id if group_id else '未设置（请在本群再执行一次本命令以加入推送）'}",
             "对局结束后会自动把结果推送到群里。"]
    if friend_note:
        lines.append(friend_note)
    if len(r["cand_note"]) > 1:
        lines.append(f"（输入的 {r['value']} 有两种解释，已按「{_KIND_CN.get(r['picked_kind'], r['picked_kind'])}」绑定）")
        lines.extend(r["cand_note"])
        lines.append("若选错了，请改用 account:<账号ID> 或 friend:<加好友ID> 明确指定。")
    lines.append(f"⚠️ 请让 QQ {target} 核对雀魂昵称是否正确；不对可 /雀魂代解绑 {target} 后重绑。")
    await matcher.finish("\n".join(lines))


@unbindfor_cmd.handle()
async def _(bot: Bot, event: MessageEvent, matcher: Matcher, args: Message = CommandArg()):
    """/雀魂代解绑 <QQ或@群友>：管理员解除群友绑定（超管 / 群主 / 群管理员）。"""
    if not permissions.is_admin(user_id=str(event.user_id), role=permissions.role_of(event)):
        await matcher.finish(permissions.deny_text())

    target, err = bindings.parse_admin_target(args.extract_plain_text(), _at_qq(event))
    if err:
        await matcher.finish(err)

    cur = bindings.get(target)
    if not cur:
        await matcher.finish(f"QQ {target} 没有绑定记录。")
    ok, msg = bindings.unbind(target)
    _log(f"管理员 {event.user_id} 代解绑：QQ {target}（原账号 {cur.get('account_id') or '未解析'}）")
    await matcher.finish(
        f"{'✅' if ok else '❌'} QQ {target}：{msg}\n"
        f"（原绑定：{cur.get('majsoul_nickname') or '(未获取)'} / ID {cur.get('account_id') or '未解析'}）")


@refriend_cmd.handle()
async def _(bot: Bot, event: MessageEvent, matcher: Matcher):
    """/雀魂重新申请好友：对本人绑定账号强制重发好友申请（非好友时生效）。"""
    qq = str(event.user_id)
    b = bindings.get(qq)
    if not b:
        await matcher.finish("你还没有绑定雀魂账号。\n用法：/雀魂绑定 <雀魂ID 或 昵称>")
    account_id = int(b.get("account_id") or 0)
    if not account_id:
        await matcher.finish("绑定记录缺少雀魂账号 ID，请先 /雀魂解绑 再重新绑定")
    nickname = b.get("majsoul_nickname") or ""
    account, password = _credentials()
    if not account or not password:
        await matcher.finish("机器人尚未配置雀魂小号（请在网页端插件设置里填写邮箱与密码）")
    protocol.configure(account, password)
    await matcher.send("正在检查好友状态…")
    try:
        friends = await protocol.fetch_friend_list()
        if any(f.account_id == account_id for f in friends):
            friend_applies.clear(account_id)
            await matcher.finish("✅ 该账号已经是小号的好友，无需申请。")
        await protocol.apply_friend(account_id)
        friend_applies.record(account_id)
        await matcher.finish(
            f"✅ 已向「{nickname or account_id}」强制重新发出雀魂好友申请。\n"
            "请对方到游戏内同意；成为好友后实时监控生效。")
    except ProviderError as e:
        _log(f"强制加好友失败（{account_id}）：{e}")
        await matcher.finish(f"强制加好友失败：{e}")


@help_cmd.handle()
async def _(matcher: Matcher):
    """/雀魂帮助：列出本插件全部命令与用法（与 `深渊帮助`/`挖宝帮助` 同风格）。"""
    await matcher.finish(_HELP_TEXT)


@unbind_cmd.handle()
async def _(bot: Bot, event: MessageEvent, matcher: Matcher):
    ok, msg = bindings.unbind(str(event.user_id))
    await matcher.finish(msg)


@me_cmd.handle()
async def _(bot: Bot, event: MessageEvent, matcher: Matcher):
    qq = str(event.user_id)
    b = bindings.get(qq)
    if not b:
        await matcher.finish("你还没有绑定雀魂账号。\n用法：/雀魂绑定 <雀魂ID 或 昵称>")
    groups = b.get("group_ids") or []
    st = monitor.stats()
    last = paipu_parser.fmt_time(st["last_pushed"]) if st["last_pushed"] else "尚无"
    lines = [
        "🀄 你的雀魂绑定",
        f"雀魂账号：{b.get('majsoul_nickname') or '(未获取)'}（ID {b.get('account_id') or '未解析'}）",
        f"推送群：{'、'.join(groups) if groups else '未设置（请在群里发一次 /雀魂绑定 以加入）'}",
        f"绑定时间：{paipu_parser.fmt_time(b.get('bound_at') or 0)}",
        f"最近一次推送：{last}",
    ]
    await matcher.finish("\n".join(lines))


@record_cmd.handle()
async def _(bot: Bot, event: MessageEvent, matcher: Matcher, args: Message = CommandArg()):
    """/雀魂战绩 [@群友|QQ号] [四麻|4麻|三麻|3麻|全部]：查看绑定账号的战绩（图片卡 + 文本摘要）。

    默认查自己、自动挑**数据最多**的麻种；群里可查其他已绑定的群友；
    加 `全部` 则两个麻种各出一张卡（不发 `全部` 时若另一个麻种也有数据，会给出提示）。
    """
    text = args.extract_plain_text().strip()
    at_qq = _at_qq(event)
    target, cat, want_all = bindings.parse_record_args(text, at_qq)
    me = str(event.user_id)
    target = target or me

    # ★ 2026-10-04 诊断：把**真实消息段结构**落日志（用户反馈「@群友 查的却是自己」）。
    #   用于判断 @ 段是否存在、`data` 字段形态（QQ 客户端 / 适配器差异），定位后可精简。
    _segs = " | ".join(f"{s.type}={str(s.data)[:60]}" for s in event.get_message())
    _log(f"战绩查询：目标={target} 麻种={cat or '自动'} 全部={want_all} "
         f"at={at_qq or '(空)'} text={text[:40]!r} segs=[{_segs}]")

    b = bindings.get(target)
    if not b:
        await matcher.finish("你还没有绑定雀魂账号。\n用法：/雀魂绑定 <雀魂ID 或 昵称>"
                             if target == me
                             else f"QQ {target} 还没有绑定雀魂账号。")
    account_id = int(b.get("account_id") or 0)
    if not account_id:
        await matcher.finish("绑定记录缺少雀魂账号 ID，请先 /雀魂解绑 再重新绑定")
    nick = b.get("majsoul_nickname") or target

    base = monitor.player_history(account_id, cat)
    if not base["items"]:
        await matcher.finish(f"「{nick}」暂无战绩数据（打完一局后就能看到啦）")

    gid = str(event.group_id) if isinstance(event, GroupMessageEvent) else ""
    names = await resolve_qq_names([target], gid)
    display = names.get(target) or nick

    # ★ A+D：「全部」→ 两个麻种各出一张卡；否则单麻种（无参数时自动挑数据最多的）
    cats = base.get("categories") or {}
    have = [k for k in (1, 2) if cats.get(k)]
    keys = have if (want_all and len(have) > 1) else [base["category"]]
    multi = len(keys) > 1
    theme = str(_settings().get("card_theme") or "dark")
    with_image = bool(_settings().get("image_cards", True)) and card.available()

    lines = [f"🀄 雀魂战绩 · {display}"
             f"（{'全部麻种' if multi else _cat_cn(keys[0])}）"]
    images: list[bytes] = []
    for k in keys:
        h = base if k == base["category"] else monitor.player_history(account_id, k)
        if not h["items"]:
            continue
        lines.extend(_record_summary_lines(h, multi=multi))
        if not with_image:
            continue
        lid = int(h["level_id"] or 0)
        try:
            images.append(await asyncio.to_thread(card.render_record, {
                "qq": target,
                "nickname": display,
                "scope": f"{_cat_cn(h['category'])} · 最近 {h['summary']['games']} 局",
                "level_name": h["level_name"],
                "level_id": lid,
                "level_init": rank_level.init_score(lid) if lid else 0,
                "level_score": int(h["level_score"] or 0),
                "level_next": (rank_level.upgrade_score(lid) if lid else 0) or 0,
                "seq": h["ranks"],
                "level_scores": h["level_scores"],
                "four": h["category"] == 1,
                "summary": h["summary"],
                "theme": theme,
            }))
        except Exception as e:  # noqa: BLE001 —— 渲染失败回退文本
            _log(f"渲染战绩卡失败，回退纯文本：{type(e).__name__}: {e}")

    if not multi:                     # ★ 提示另一个麻种，避免「以为没数据」
        others = [(k, v) for k, v in cats.items() if k != base["category"] and v]
        if others:
            k, v = others[0]
            lines.append(f"（另有{_cat_cn(k)} {v} 局，发 /雀魂战绩 {_cat_cn(k)} 单独看，"
                         f"或 /雀魂战绩 全部 一起看）")
    await _finish_card(matcher, "\n".join(lines), images)


@paipu_cmd.handle()
async def _(bot: Bot, event: MessageEvent, matcher: Matcher, args: Message = CommandArg()):
    """/雀魂牌谱 <牌谱链接或牌谱 ID>：查看该局完整明细（图片卡 + 文本明细）。

    ★ 命令式触发（用户 2026-09-28 拍板：「只做命令 /雀魂牌谱 <链接>」）——不做群内自动
      识别链接，避免被刷链接打爆牌谱接口（`code=540` 是**账号级**风控，见铁律 4）。
    ★ 降风控三道：① 同一条牌谱走**本地缓存**（第二次零网络请求）；② 真实拉取共享轮询的
      每小时预算 `MAX_FETCH_PER_HOUR` 与 540 冷却；③ 同一「群+人」`PAIPU_QUERY_GAP` 秒一次。
    ★ 主角（明细以谁为主）：优先**链接尾号反推的分享者**；尾号缺失/不在本局时回退到
      **查询者自己绑定的账号**（若在本局里）；再没有就不设主角，只出全玩家明细。
    """
    text = args.extract_plain_text().strip()
    info = paipu_parser.parse_paipu_link(text)
    if not info:
        await matcher.finish(_PAIPU_USAGE)

    qq = str(event.user_id)
    gid = str(event.group_id) if isinstance(event, GroupMessageEvent) else ""
    allowed, wait = monitor.paipu_rate_allow(f"{gid or 'private'}:{qq}")
    if not allowed:
        await matcher.finish(f"牌谱查询太频繁啦，{wait} 秒后再试～")

    account, password = _credentials()
    if not account or not password:
        await matcher.finish("机器人尚未配置雀魂小号（请在网页端插件设置里填写邮箱与密码）")

    await matcher.send("正在解析牌谱…")
    b = bindings.get(qq) or {}
    res = await monitor.query_paipu(
        info["uuid"], account=account, password=password,
        prefer_account_id=int(info.get("sharer_account_id") or 0),
        fallback_account_id=int(b.get("account_id") or 0),
        match_id=int(info.get("match_id") or 0))
    if not res.get("ok"):
        await matcher.finish(f"❌ 牌谱查询失败：{res.get('error')}")

    detail = res["detail"]
    theme = str(_settings().get("card_theme") or "dark")
    image = None
    if bool(_settings().get("image_cards", True)) and card.available():
        try:
            image = await asyncio.to_thread(card.render_paipu_detail,
                                            {**detail, "theme": theme})
        except Exception as e:  # noqa: BLE001 —— 渲染失败回退完整文本（铁律 14）
            _log(f"渲染牌谱明细卡失败，回退纯文本：{type(e).__name__}: {e}")
    out = paipu_parser.format_paipu_detail(
        detail, compact=image is not None,
        # ★ 2026-10-28：与推送同一开关——关闭后连查询回复也不带牌谱链接
        with_paipu=bool(_settings().get("paipu_links_enabled", True)))
    if res.get("from_cache"):
        out += "\n（本地缓存命中，未重复拉取牌谱）"
    await _finish_card(matcher, out, [image] if image else [])


@ai_cmd.handle()
async def _(bot: Bot, event: MessageEvent, matcher: Matcher, args: Message = CommandArg()):
    """/雀魂分析 <牌谱链接或牌谱 ID>：用**自建 akochan** 逐手复盘这局（Feature-08，方案 B）。

    ★ 为什么后台异步（`asyncio.create_task`）：单局东场 1 局实测 **1 分 23 秒**（4 核全满），
      整场半庄十几分钟——**阻塞 handler 会把命令卡死**。所以立即回执、跑完再用
      `send_to_chat` 把结果发回原会话。
    ★ 三层防重复/防滥用：① 同一 uuid 正在分析 → 直接拒绝（`_AI_RUNNING`）；
      ② `paipu_rate_allow`（群+人 20 秒）限频；③ 分析结果**落盘缓存**（同一局第二次零耗时）。
    ★ 分析对象（视角）：链接尾号反推的分享者 → 查询者自己绑定的账号 → 都没有就报错
      （**绝不换个人替分析**；主角口径与 `/雀魂牌谱` 一致，见铁律 23）。
    """
    text = args.extract_plain_text().strip()
    detailed = any(w in text for w in ("详细", "全部", "全列"))
    if detailed:                                     # 关键词不参与链接解析，先剥掉
        for w in ("详细", "全部", "全列"):
            text = text.replace(w, "")
        text = text.strip()
    info = paipu_parser.parse_paipu_link(text)
    if not info:
        await matcher.finish(_AI_USAGE)

    ok, why = ai_service.engine_available()
    if not ok:
        await matcher.finish(f"❌ {why}")

    qq = str(event.user_id)
    gid = str(event.group_id) if isinstance(event, GroupMessageEvent) else ""
    key = str(info["uuid"])
    if key in _AI_RUNNING:
        await matcher.finish("这局已经在分析中啦，跑完我会把结果发出来～")
    allowed, wait = monitor.paipu_rate_allow(f"ai:{gid or 'private'}:{qq}")
    if not allowed:
        await matcher.finish(f"AI 分析请求太频繁啦，{wait} 秒后再试～")

    account, password = _credentials()
    if not account or not password:
        await matcher.finish("机器人尚未配置雀魂小号（请在网页端插件设置里填写邮箱与密码）")

    await matcher.send("正在读取牌谱…")
    b = bindings.get(qq) or {}
    res = await monitor.query_paipu(
        info["uuid"], account=account, password=password,
        prefer_account_id=int(info.get("sharer_account_id") or 0),
        fallback_account_id=int(b.get("account_id") or 0),
        match_id=int(info.get("match_id") or 0))
    if not res.get("ok"):
        await matcher.finish(f"❌ 牌谱查询失败：{res.get('error')}")

    rec = res.get("record")
    detail = res["detail"]
    sid = int(detail.get("sharer_account_id") or 0)
    seat = ai_service.seat_of(rec, sid) if rec is not None else -1
    if seat < 0:
        await matcher.finish("❌ 没能在牌谱里定位到要复盘的玩家（分享者/你本人都不是本局玩家）"
                             "——换个链接，或先 /雀魂绑定 自己的雀魂 ID")

    name = str((detail.get("sharer") or {}).get("name") or "")
    meta = ai_service.meta_of(rec)
    _AI_RUNNING.add(key)
    asyncio.create_task(_run_ai_analysis(bot, event, rec, seat, key, meta, name, detailed))
    await matcher.finish(
        "🎯 已开始 AI 复盘（akochan 引擎）"
        + ("·详细模式" if detailed else "") + "\n"
        f"{meta}｜视角：{name or '分享者'}\n"
        + ("会列出全部失误与危险牌，图片会比较长～\n" if detailed else "")
        + "单局约 1~2 分钟、整场可能十几分钟，跑完我会把结果发过来～")


async def _run_ai_analysis(bot: Bot, event: MessageEvent, rec, seat: int,
                           uuid: str, meta: str, name: str,
                           detailed: bool = False) -> None:
    """后台跑 AI 复盘并把结果发回原会话（handler 已 finish，不能再用 matcher）。

    `detailed=True`＝**详细模式**：列出全部失误与危险牌（图会长很多）；若出图失败，
    文本兜底也会很长 → 按 `_DETAIL_TEXT_MAX` 分段发送（否则超长单条可能发不出去）。
    """
    chat_type = "group" if isinstance(event, GroupMessageEvent) else "private"
    chat_id = str(event.group_id) if chat_type == "group" else str(event.user_id)
    try:
        result = await ai_service.analyze(rec, seat, uuid=uuid, detailed=detailed)
        summary = result.get("summary") or {}
        theme = str(_settings().get("card_theme") or "dark")
        image = None
        if bool(_settings().get("image_cards", True)) and card.available():
            try:
                image = await asyncio.to_thread(card.render_ai_review, {
                    "theme": theme, "meta": meta, "name": name,
                    "summary": summary,
                    "max_rows": _DETAIL_LIMIT if detailed else 6,
                    "max_dangers": _DETAIL_LIMIT if detailed else 3})
            except Exception as e:  # noqa: BLE001 —— 渲染失败回退完整文本（铁律 14）
                _log(f"渲染 AI 复盘卡失败，回退纯文本：{type(e).__name__}: {e}")
        out = ai_service.format_summary_text(result, meta=meta, compact=image is not None,
                                             detailed=detailed)
        if image is None and len(out) > _DETAIL_TEXT_MAX:
            chunks = _split_text(out, _DETAIL_TEXT_MAX)
            for i, c in enumerate(chunks, 1):
                head = f"（{i}/{len(chunks)}）\n" if len(chunks) > 1 else ""
                await send_to_chat(bot, chat_type, chat_id, head + c)
        else:
            await send_to_chat(bot, chat_type, chat_id, out, image)
        cost = "缓存" if result.get("from_cache") else f"{int(result.get('elapsed') or 0)} 秒"
        _log(f"AI 复盘完成：{uuid}｜一致率 {float(summary.get('rating') or 0) * 100:.1f}%"
             f"｜偏差 {summary.get('deviations')} 手"
             f"｜{'详细模式 ' if detailed else ''}耗时 {cost}", event="ai")
    except Exception as e:  # noqa: BLE001 —— 后台任务绝不能把异常抛出去
        _log(f"AI 复盘失败：{uuid}：{type(e).__name__}: {e}", level="warn", event="ai")
        try:
            await send_to_chat(bot, chat_type, chat_id, f"❌ AI 复盘失败：{e}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        _AI_RUNNING.discard(uuid)


@addfriend_cmd.handle()
async def _(bot: Bot, event: MessageEvent, matcher: Matcher):
    """回显小号的**客户端显示 ID**，引导群友加好友（Phase 2 准入门槛）。

    ★ 实时状态（`fetchFriendList`）只能看到小号的好友，故必须引导群友主动加小号。
    """
    account, password = _credentials()
    if not account or not password:
        await matcher.finish("机器人尚未配置雀魂小号（请在网页端插件设置里填写邮箱与密码）")
    protocol.configure(account, password)
    await matcher.send("正在查询小号信息…")
    try:
        aid, nick = await protocol.whoami()
    except ProviderError as e:
        await matcher.finish(f"查询小号失败：{e}")
    fid = majsoul_id.friend_of(aid)
    await matcher.finish(
        "🀄 加小号好友（实时对局状态的前提）\n"
        f"小号昵称：{nick}\n"
        f"客户端显示 ID：{fid}\n"
        "在雀魂客户端「好友 → 添加好友」里搜这个 ID 加为好友，\n"
        "之后你一开始对局，群里就会自动提示（约 5 分钟后可观战）。")


def _nickname_from_games(games, account_id: int) -> str:
    for g in games:
        for p in g.players:
            if int(p.account_id) == int(account_id) and p.nickname:
                return p.nickname
    return ""


# ---------------------------------------------------------------- 定时轮询

_last_tick = 0.0
_tick_hb = 0.0              # ★ 日志心跳：上一次写「轮询」状态日志的时间
_live_hb = 0.0              # ★ 日志心跳：上一次写「实时」状态日志的时间
_tick_lock = asyncio.Lock()


def _heartbeat_interval(st: dict) -> float:
    """日志心跳的最小间隔（秒）；0 = 每轮都写。

    ★ 2026-09-21（线上实证）：tick 日志原为「仅 pushed/errors 才写」→ 平静期网页端
    与 journalctl 完全静默，看起来像插件死了。现改为「事件必写 + 平静期心跳」。
    """
    try:
        return max(0.0, float(st.get("heartbeat_interval", 300)))
    except (TypeError, ValueError):
        return 300.0


def _register_job() -> None:
    """注册轮询任务（固定 30 秒心跳，内部按 poll_interval 节流 → 改配置即时生效）。"""
    try:
        from nonebot_plugin_apscheduler import scheduler
    except Exception as e:  # noqa: BLE001
        _log(f"未安装 nonebot-plugin-apscheduler，定时轮询不可用：{e}")
        return

    try:  # 热重载时避免重复注册
        scheduler.remove_job(JOB_ID)
    except Exception:  # noqa: BLE001
        pass

    @scheduler.scheduled_job("interval", seconds=30, id=JOB_ID,
                             max_instances=1, coalesce=True)
    async def _tick_job() -> None:
        global _last_tick, _tick_hb
        st = _settings()
        try:
            interval = max(30, int(st.get("poll_interval") or 180))
        except (TypeError, ValueError):
            interval = 180
        now = time.time()
        if now - _last_tick < interval:
            return
        if _tick_lock.locked():
            return
        async with _tick_lock:
            _last_tick = now
            account, password = _credentials()
            if not account or not password:
                return
            try:
                stat = await monitor.tick(
                    send_group,
                    account=account,
                    password=password,
                    rate_limit=int(st.get("push_rate_limit") or 5),
                    room_battle_enabled=bool(st.get("room_battle_enabled", True)),
                    first_backfill=bool(st.get("first_bind_backfill", False)),
                    # ★ 2026-10-28：牌谱链接总开关（关闭 = 推送信息内不带任何牌谱链接）
                    with_paipu=bool(st.get("paipu_links_enabled", True)),
                    commentary_settings={
                        "commentary_enabled": bool(st.get("commentary_enabled", True)),
                        "commentary_gap_levels": st.get("commentary_gap_levels") or "200,100,50",
                        "commentary_streak_repeat": st.get("commentary_streak_repeat") or 3,
                        "commentary_streak_absent": st.get("commentary_streak_absent") or 5,
                    },
                    image_cards=bool(st.get("image_cards", True)),
                    card_theme=str(st.get("card_theme") or "dark"),
                    name_resolver=resolve_qq_names,
                )
                active = bool(stat["pushed"] or stat["errors"] or stat["new"]
                              or stat["no_group"] or stat.get("skipped_activity"))
                if active or (now - _tick_hb) >= _heartbeat_interval(st):
                    _tick_hb = now
                    _log(f"轮询：checked={stat['checked']} new={stat['new']} "
                         f"pushed={stat['pushed']} "
                         f"skipact={stat.get('skipped_activity', 0)} "
                         f"errors={stat['errors'][:3]}")
            except Exception as e:  # noqa: BLE001 —— 定时任务绝不抛出
                _log(f"轮询异常：{type(e).__name__}: {e}")


_register_job()


# ---------------------------------------------------------------- 实时状态轮询（Phase 2）

_live_last_tick = 0.0
_live_lock = asyncio.Lock()


def _register_live_job() -> None:
    """注册实时状态轮询任务（Phase 2；固定 30 秒心跳，内部按 live_interval 节流）。"""
    try:
        from nonebot_plugin_apscheduler import scheduler
    except Exception as e:  # noqa: BLE001
        _log(f"未安装 nonebot-plugin-apscheduler，实时状态轮询不可用：{e}")
        return

    try:  # 热重载时避免重复注册
        scheduler.remove_job(LIVE_JOB_ID)
    except Exception:  # noqa: BLE001
        pass

    @scheduler.scheduled_job("interval", seconds=30, id=LIVE_JOB_ID,
                             max_instances=1, coalesce=True)
    async def _live_job() -> None:
        global _live_last_tick, _live_hb
        st = _settings()
        if not bool(st.get("live_enabled", True)):
            return
        try:
            interval = max(30, int(st.get("live_interval") or 120))
        except (TypeError, ValueError):
            interval = 120
        now = time.time()
        if now - _live_last_tick < interval:
            return
        if _live_lock.locked():
            return
        async with _live_lock:
            _live_last_tick = now
            account, password = _credentials()
            if not account or not password:
                return
            try:
                stat = await friend_monitor.tick(
                    send_group,
                    account=account,
                    password=password,
                    online_enabled=bool(st.get("live_online_enabled", True)),
                    online_cooldown=int(st.get("live_online_cooldown") or 300),
                    rate_limit=int(st.get("push_rate_limit") or 5),
                    room_battle_enabled=bool(st.get("room_battle_enabled", True)),
                    image_cards=bool(st.get("image_cards", True)),
                    card_theme=str(st.get("card_theme") or "dark"),
                    name_resolver=resolve_qq_names,
                )
                active = bool(stat["pushed"] or stat["errors"] or stat["room_battles"]
                              or stat["online_events"] or stat["playing_events"]
                              or stat["no_group"] or stat.get("skipped_activity"))
                if active or (now - _live_hb) >= _heartbeat_interval(st):
                    _live_hb = now
                    _log(f"实时：checked={stat['checked']} bound={stat['bound']} "
                         f"online={stat['online_events']} playing={stat['playing_events']} "
                         f"rooms={stat['room_battles']} pushed={stat['pushed']} "
                         f"skipact={stat.get('skipped_activity', 0)} "
                         f"errors={stat['errors'][:3]}")
            except Exception as e:  # noqa: BLE001 —— 定时任务绝不抛出
                _log(f"实时轮询异常：{type(e).__name__}: {e}")


_register_live_job()
