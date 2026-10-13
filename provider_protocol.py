# -*- coding: utf-8 -*-
"""雀魂官方协议数据源（Phase 1 主数据源）。

链路（M1 实测冻结，见 `docs/specs/epic-majsoul-tracker/协议探测结果.md` §9.7）：

    Route.requestConnection → Lobby.login → Lobby.fetchAccountInfoExtra / fetchGameRecord

设计要点：
1. **连接为进程级单例**——插件热重载会重建本模块，若各建连接会与旧连接互踢
   （雀魂实测 `code=1004`），故连接状态挂在 `sys.modules` 的哨兵模块上，
   新模块加载时复用存活连接（哨兵名不在 `plugins.<插件名>` 前缀内，热重载不会清理）。
2. **websockets>=15 会读 Windows 系统代理** → 显式 `proxy=None` 直连；
   可用环境变量 `MAJSOUL_PROXY` 覆盖（如 `http://127.0.0.1:7890`）。
3. 网络调用全部带 timeout + 异常捕获，失败抛 `ProviderError`（带上下文），不抛裸栈。
4. 本模块**不依赖 nonebot**（纯逻辑层），凭据由调用方 `configure()` 注入。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import sys
import time
import types
import urllib.request
import uuid as uuid_mod
from dataclasses import dataclass, field
from typing import Any, Iterable

import websockets

from .ms import protocol_pb2 as pb
from .ms.base import MSRPCChannel

# ---- 协议常量（M1 实测）----
ENDPOINT = "wss://route-2.maj-soul.com/gateway"
# ★ 2026-09-23：雀魂客户端升级 4.0.47，版本串格式由 `WebGL_2022-{ver}` 变为 `WebGL-release-{ver}`。
#   硬编码旧串会让 `Lobby.login` 返回 **code=151**（表现为「小号登录失败」，但账号密码完全正常）。
#   现值仅作**兜底默认**，正常路径走 `version_str()` 动态发现（见下）。
VERSION_STR = "WebGL-release-4.0.47"
ORIGIN = "https://www.maj-soul.com"
CATEGORIES = (1, 2)   # 1=四麻 2=三麻（★ category=0 恒返回空）
TYPES = (1, 2, 3)     # 1=段位场 2=友人局 3=其他（大会类）
CALL_TIMEOUT = 15.0
QUERY_GAP = 0.15      # 参数矩阵逐次查询之间的轻微间隔

# ---- 客户端版本串动态发现（★ 2026-09-23 新增，防「客户端更新即登录失败」）----
# 原理：客户端 index.html 里引用 Unity WebGL 构建名，形如
#   chs_t-WebGL-release-4.0.47(47).loader.js  → 版本串 = `WebGL-release-4.0.47`
#   chs_t-WebGL_2022-0.16.275(275).loader.js  → 版本串 = `WebGL_2022-0.16.275`
# 即「构建名去掉 `(数字)` 后缀」。
VERSION_INDEX_URLS = (
    "https://game.maj-soul.com/1/index.html",
    "https://mahjongsoul.game.yo-star.com/1/index.html",
)
_VERSION_RE = re.compile(r"(WebGL[-_][A-Za-z0-9._-]+)\(\d+\)\.loader\.js")
VERSION_FETCH_TIMEOUT = 10.0   # 拉取 index.html 的超时
VERSION_TTL = 1800.0           # 发现结果缓存时长（秒）；登录遇 151 会强制刷新

# 进程级哨兵模块名（热重载存活）
_SINGLETON_KEY = "__majsoul_tracker_runtime__"


class ProviderError(Exception):
    """协议调用失败（含雀魂返回的 error code 或网络异常）。"""

    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


class _VersionRejected(Exception):
    """`Lobby.login` 返回 `code=151`（几乎总是客户端版本串过期）——内部信号，触发刷新重试。"""


# ---------------------------------------------------------------- 版本串发现

_version_cache: tuple[float, str] | None = None   # (发现时间, 版本串)


def _fetch_version_str_sync() -> str:
    """同步抓客户端 index.html 解析版本串（由 `asyncio.to_thread` 调用）；失败返回空串。"""
    for url in VERSION_INDEX_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=VERSION_FETCH_TIMEOUT) as resp:
                html = resp.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 —— 网络/证书问题一律视为「发现失败」
            continue
        m = _VERSION_RE.search(html)
        if m:
            return m.group(1)
    return ""


async def version_str(force_refresh: bool = False) -> str:
    """取当前客户端版本串：动态发现（带 TTL 缓存）→ 失败回退 `VERSION_STR` 默认值。

    ★ 2026-09-23：雀魂每次大版本更新都会改版本串，硬编码必然失效（`login` code=151）。
    本函数让插件自愈；发现失败也不影响功能（回退到已验证的默认值）。
    """
    global _version_cache
    now = time.time()
    if not force_refresh and _version_cache and now - _version_cache[0] < VERSION_TTL:
        return _version_cache[1]
    found = await asyncio.to_thread(_fetch_version_str_sync)
    if found:
        if _version_cache is None or _version_cache[1] != found:
            print(f"[majsoul_tracker] 客户端版本串：{found}（兜底默认 {VERSION_STR}）")
        _version_cache = (now, found)
        return found
    _version_cache = (now, VERSION_STR)
    return VERSION_STR


def version_cache_info() -> tuple[str, float]:
    """返回 (当前版本串, 距上次发现秒数)——供日志与排查用。"""
    if _version_cache is None:
        return VERSION_STR, -1.0
    return _version_cache[1], max(0.0, time.time() - _version_cache[0])


# ---------------------------------------------------------------- 数据模型

@dataclass
class PlayerResult:
    """一条对局摘要中的单个玩家结果。"""

    account_id: int
    nickname: str
    seat: int
    rank: int
    final_point: int
    grading_score: int   # = PT
    level_id: int
    # ★ 2026-09-20（Feature-04）：段位分与三麻段位。level=四麻、level3=三麻为字段名约定，
    #   但取用时一律按 id 前缀判定麻种（`rank_level.pick_level`），避免约定与实际相反。
    level_id3: int = 0
    level_score: int = 0
    level_score3: int = 0


@dataclass
class GameBrief:
    """`fetchAccountInfoExtra` 返回的一条对局摘要（对局已结束）。"""

    uuid: str
    start_time: int
    end_time: int
    tag: int           # 1=段位场 2=友人局 3=其他（与 TYPES 对应）
    sub_tag: int       # ★ 实测 == 牌谱 `meta.mode_id`（场次细分，见 `yaku_map.MODE_ID_NAMES`）
    category: int      # ★ 查询参数麻种：1=四麻 2=三麻（≠ `GameConfig.category`，后者是对局类别）
    rank: int          # 被查询玩家的名次
    final_point: int   # 被查询玩家的得点
    players: list[PlayerResult] = field(default_factory=list)


@dataclass
class GameRecord:
    """`fetchGameRecord` 返回的对局详情（head + 牌谱二进制）。"""

    uuid: str
    start_time: int
    end_time: int
    category: int      # ⚠️ `GameConfig.category` = **对局类别**（1=友人局/2=段位场），不是麻种！
    mode: int          # `GameMode.mode`：1/2=四麻东/半庄、11/12=三麻东/半庄（这个才是麻种依据）
    mode_id: int       # `GameMetaData.mode_id` = 场次细分（见 `yaku_map.MODE_ID_NAMES`）
    room_id: int       # 0=匹配场（段位场）；>0=自定义房（友人局）
    accounts: list[dict[str, Any]]
    result_players: list[dict[str, Any]]
    data: bytes
    head: Any = None


# ---------------------------------------------------------------- 单例容器

def _runtime() -> types.ModuleType:
    """取（或创建）进程级哨兵模块，用于跨热重载保留连接与凭据。"""
    mod = sys.modules.get(_SINGLETON_KEY)
    if mod is None:
        mod = types.ModuleType(_SINGLETON_KEY)
        mod.conn = None          # type: ignore[attr-defined]
        mod.lock = None          # type: ignore[attr-defined]
        mod.account = ""         # type: ignore[attr-defined]
        mod.password = ""        # type: ignore[attr-defined]
        sys.modules[_SINGLETON_KEY] = mod
    return mod


def configure(account: str, password: str) -> None:
    """注入雀魂小号凭据（由 main/monitor 从插件 Secret 读取后调用）。"""
    rt = _runtime()
    if rt.account != account or rt.password != password:  # type: ignore[attr-defined]
        rt.account = account          # type: ignore[attr-defined]
        rt.password = password        # type: ignore[attr-defined]
        # 凭据变更 → 旧连接作废（下次调用重新登录）
        rt.conn = None                # type: ignore[attr-defined]


class _Conn:
    """一条已登录的协议连接。"""

    def __init__(self, channel: MSRPCChannel, account_id: int, nickname: str):
        self.channel = channel
        self.account_id = account_id
        self.nickname = nickname

    @property
    def alive(self) -> bool:
        ws = getattr(self.channel, "_ws", None)
        if ws is None:
            return False
        state = getattr(ws, "state", None)
        if state is not None:                      # websockets>=14 新 API
            return getattr(state, "name", "") == "OPEN"
        return not getattr(ws, "closed", True)     # 旧 API 兜底

    async def close_silent(self) -> None:
        try:
            await self.channel.close()
        except Exception:  # noqa: BLE001 —— 关闭失败不影响后续重建
            pass


# ---------------------------------------------------------------- 低层调用

def _ok(res) -> bool:
    return res is not None and not (res.HasField("error") and res.error.code)


def _err(res) -> str:
    if res is None:
        return "无响应"
    if res.HasField("error") and res.error.code:
        e = res.error
        parts = [f"code={e.code}"]
        if e.str_params:
            parts.append(f"str_params={list(e.str_params)}")
        if e.json_param:
            parts.append(f"json_param={e.json_param}")
        return " | ".join(parts)
    return "OK"


async def _call(channel: MSRPCChannel, service: str, method: str, req_bytes: bytes,
                res_cls, timeout: float = CALL_TIMEOUT):
    """发送一次 RPC 并等待响应（带超时与事件清理）。"""
    name = f".lq.{service}.{method}"
    idx = channel._new_req_idx
    channel._new_req_idx = (channel._new_req_idx + 1) % 60007
    pkt = b"\x02" + idx.to_bytes(2, "little") + channel.wrap(name, req_bytes)
    evt = asyncio.Event()
    channel._req_events[idx] = evt
    try:
        await channel._ws.send(pkt)
    except Exception as e:  # noqa: BLE001 —— 连接已断
        channel._req_events.pop(idx, None)
        raise ProviderError(f"{service}.{method} 发送失败：{type(e).__name__}: {e}") from e
    try:
        await asyncio.wait_for(evt.wait(), timeout=timeout)
    except asyncio.TimeoutError as e:
        channel._req_events.pop(idx, None)
        raise ProviderError(f"{service}.{method} 超时（{timeout:.0f}s）") from e
    raw = channel._res.pop(idx, None)
    channel._req_events.pop(idx, None)
    if raw is None:
        raise ProviderError(f"{service}.{method} 无响应体")
    res = res_cls()
    res.ParseFromString(channel.unwrap(raw[3:]).data)
    return res


async def _open_and_login(account: str, password: str, ver: str) -> _Conn:
    """建连 + requestConnection + login（M1 实证链路），使用给定版本串。"""
    channel = MSRPCChannel(ENDPOINT)
    proxy = os.environ.get("MAJSOUL_PROXY") or None
    try:
        channel._ws = await websockets.connect(ENDPOINT, origin=ORIGIN, proxy=proxy)
    except Exception as e:  # noqa: BLE001
        raise ProviderError(f"连接雀魂网关失败：{type(e).__name__}: {e}") from e
    channel._msg_dispatcher = asyncio.create_task(channel.dispatch_msg())

    # 前置：requestConnection（缺失会导致全接口 code=151）
    r = pb.ReqRequestConnection()
    r.type = 1
    r.route_id = "en-2"
    r.timestamp = int(time.time())
    raw = bytearray(r.SerializeToString()) + b"\x32\x03Web"
    try:
        rc = await _call(channel, "Route", "requestConnection", bytes(raw), pb.ResRequestConnection)
        if not _ok(rc):
            raise ProviderError(f"requestConnection 失败：{_err(rc)}")

        rq = pb.ReqLogin()
        rq.account = account
        rq.password = hmac.new(b"lailai", password.encode(), hashlib.sha256).hexdigest()
        rq.device.platform = "pc"
        rq.device.is_browser = True
        rq.random_key = str(uuid_mod.uuid1())
        rq.gen_access_token = True
        rq.client_version_string = ver
        rq.currency_platforms.append(2)
        res = await _call(channel, "Lobby", "login", rq.SerializeToString(), pb.ResLogin)
    except Exception:
        await _Conn(channel, 0, "").close_silent()
        raise

    if not _ok(res):
        await _Conn(channel, 0, "").close_silent()
        # ★ 151 = 客户端版本串过期（雀魂更新）→ 交外层刷新版本串重试，不要误报「账号密码错」
        if res.HasField("error") and int(res.error.code) == 151:
            raise _VersionRejected(_err(res))
        raise ProviderError(f"雀魂小号登录失败：{_err(res)}（请检查账号密码）")

    nickname = res.account.nickname if res.HasField("account") else ""
    return _Conn(channel, int(res.account_id), nickname)


async def _connect_and_login(account: str, password: str) -> _Conn:
    """建连 + 登录；遇 `code=151`（版本串过期）自动刷新版本串重试一次。"""
    if not account or not password:
        raise ProviderError("雀魂小号凭据未配置（请在插件设置中填写邮箱与密码）")

    ver = await version_str()
    last = ""
    for attempt in range(2):
        try:
            return await _open_and_login(account, password, ver)
        except _VersionRejected as e:
            last = str(e)
            if attempt == 0:
                newer = await version_str(force_refresh=True)
                if newer and newer != ver:
                    ver = newer
                    continue
            break
    raise ProviderError(
        f"雀魂小号登录失败：{last}（code=151 通常是雀魂客户端更新导致版本串过期；"
        f"已用版本串 {ver} 刷新重试仍失败，请检查账号密码）"
    )


async def _get_conn(force_new: bool = False) -> _Conn:
    """取存活连接；不存在/已断/强制重建时重新登录（单例 + 锁）。"""
    rt = _runtime()
    if rt.lock is None:                      # type: ignore[attr-defined]
        rt.lock = asyncio.Lock()             # type: ignore[attr-defined]
    async with rt.lock:                      # type: ignore[attr-defined]
        conn: _Conn | None = rt.conn         # type: ignore[attr-defined]
        if not force_new and conn is not None and conn.alive:
            return conn
        if conn is not None:
            await conn.close_silent()
        conn = await _connect_and_login(rt.account, rt.password)  # type: ignore[attr-defined]
        rt.conn = conn                       # type: ignore[attr-defined]
        return conn


# ---------------------------------------------------------------- 对外 API

async def close() -> None:
    """关闭连接（插件停用/测试收尾用）。"""
    rt = _runtime()
    conn: _Conn | None = rt.conn  # type: ignore[attr-defined]
    rt.conn = None                # type: ignore[attr-defined]
    if conn is not None:
        await conn.close_silent()


async def whoami() -> tuple[int, str]:
    """返回小号 (account_id, nickname)；失败抛 ProviderError。"""
    conn = await _get_conn()
    return conn.account_id, conn.nickname


# ---------------------------------------------------------------- Phase 2：好友状态

@dataclass
class FriendState:
    """一个小号好友的实时状态快照（Phase 2）。

    ★ 只有**小号的好友**才有状态可查——群友需先加小号好友。
    """

    account_id: int
    nickname: str
    online: bool = False
    playing_uuid: str = ""
    category: int = 0      # ⚠️ `AccountPlayingGame.category` = **对局类别**（2=段位场），**不是麻种**！
    room_id: int = 0       # 0=段位场匹配；>0=自定义房
    mode_id: int = 0       # ★ 场次细分（唯一的麻种/房间依据，见 `yaku_map.MODE_ID_NAMES`）

    @property
    def playing(self) -> bool:
        return bool(self.playing_uuid)


async def fetch_friend_list() -> list[FriendState]:
    """查小号好友列表与实时状态（`fetchFriendList`）。

    掉线/互踢（`code=1004`）时重建连接重试一次。
    """
    conn = await _get_conn()
    last = ""
    for attempt in range(2):
        rq = pb.ReqCommon()
        try:
            res = await _call(conn.channel, "Lobby", "fetchFriendList",
                              rq.SerializeToString(), pb.ResFriendList)
        except ProviderError as e:
            last = str(e)
            if attempt == 0:
                conn = await _get_conn(force_new=True)
                continue
            raise ProviderError(f"查询好友列表失败：{last}") from e
        if not _ok(res):
            last = _err(res)
            if attempt == 0:
                conn = await _get_conn(force_new=True)
                continue
            raise ProviderError(f"查询好友列表失败：{last}")

        out: list[FriendState] = []
        for f in res.friends:
            st = f.state
            p = st.playing if st.HasField("playing") else None
            out.append(FriendState(
                account_id=int(f.base.account_id),
                nickname=f.base.nickname,
                online=bool(st.is_online),
                playing_uuid=(p.game_uuid if p is not None else ""),
                category=(int(p.category) if p is not None else 0),
                room_id=(int(p.meta.room_id) if p is not None else 0),
                mode_id=(int(p.meta.mode_id) if p is not None else 0),
            ))
        return out
    return []


async def find_live_start_time(game_uuid: str, mode_id: int) -> int:
    """查某局的开局时间戳（`fetchGameLiveList`，`filter_id = 200 + mode_id`）。

    ★ 2026-09-21 新增：`fetchFriendList` 的 `state.playing` **只有 uuid/category/meta，没有开局时间**，
    故「对局开始」文案的精确开局时间只能从这里拿。
    ⚠️ 该列表是「最近约 5 分钟开局的对局」滚动窗口（§9.10.6）→ 只有开局 ≤5 分钟内能查到；
    查不到返回 **0**（调用方回退到「约 5 分钟后可观战」）。
    ★ 只读列表查询，风险远低于 `fetchGameRecord`（牌谱屋以 1 次/秒持续跑该接口）。
    """
    if not game_uuid:
        return 0
    conn = await _get_conn()
    rq = pb.ReqGameLiveList()
    rq.filter_id = 200 + int(mode_id or 0)
    try:
        res = await _call(conn.channel, "Lobby", "fetchGameLiveList",
                          rq.SerializeToString(), pb.ResGameLiveList)
    except ProviderError as e:
        # ★ 2026-09-21：不再静默吞掉 —— 抛给调用方记日志，便于区分「接口失败」与「窗口外未命中」
        raise ProviderError(f"查开局时间：调用失败（{e}）") from e
    if not _ok(res):
        raise ProviderError(f"查开局时间：{_err(res)}（filter_id={rq.filter_id}）")
    for g in res.live_list:
        if g.uuid == game_uuid:
            return int(g.start_time)
    return 0      # 未命中 = 该局已超出列表的「最近约 5 分钟开局」窗口（正常情况）


async def apply_friend(account_id: int) -> None:
    """小号主动向指定账号发起好友申请（`applyFriend`，2026-09-20 路线 2）。

    雀魂机制：加好友需对方同意（用户确认）；调用成功仅代表**申请已发出**，
    是否成为好友取决于对方在游戏内点同意。失败抛 `ProviderError`（调用方降级提示）。
    """
    conn = await _get_conn()
    rq = pb.ReqApplyFriend()
    rq.target_id = int(account_id)
    res = await _call(conn.channel, "Lobby", "applyFriend",
                      rq.SerializeToString(), pb.ResCommon)
    if not _ok(res):
        raise ProviderError(f"发起好友申请失败：{_err(res)}")


async def fetch_recent_games(account_id: int, *,
                             categories: Iterable[int] = CATEGORIES,
                             types: Iterable[int] = TYPES,
                             limit: int | None = None) -> list[GameBrief]:
    """查任意账号（含非好友）最近对局，按结束时间降序。

    遍历 `category × type` 矩阵并按 uuid 去重（单组合上限 50 条）；
    单个组合失败不致命（该类型可能无数据），全部失败才抛错。
    """
    conn = await _get_conn()
    seen: dict[str, GameBrief] = {}
    errors: list[str] = []
    for category in categories:
        for typ in types:
            rq = pb.ReqFetchAccountInfoExtra()
            rq.account_id = int(account_id)
            rq.category = category
            rq.type = typ
            try:
                res = await _call(conn.channel, "Lobby", "fetchAccountInfoExtra",
                                  rq.SerializeToString(), pb.ResFetchAccountInfoExtra)
            except ProviderError as e:
                # 连接层失败 → 重建一次再试（掉线自愈）
                errors.append(f"c{category}t{typ}: {e}")
                try:
                    conn = await _get_conn(force_new=True)
                except ProviderError:
                    pass
                continue
            if not _ok(res):
                errors.append(f"c{category}t{typ}: {_err(res)}")
                continue
            for g in res.recent_games:
                if g.uuid in seen:
                    continue
                seen[g.uuid] = _to_brief(g, category)
            await asyncio.sleep(QUERY_GAP)
    if not seen and errors:
        raise ProviderError("查询最近对局失败：" + "；".join(errors[:4]))
    out = sorted(seen.values(), key=lambda x: x.end_time, reverse=True)
    return out[:limit] if limit else out


BUSY_CODES = (540,)   # ★ 实测：短时间连续拉牌谱会被雀魂限流，返回 code=540


async def fetch_game_record_raw(game_uuid: str, *, retries: int = 2) -> bytes:
    """拉对局详情并返回**原始响应字节**（`ResGameRecord` 序列化），供落盘缓存与离线重建。

    ★ 2026-09-28 新增（Feature-07 牌谱链接查询）：拉回来的原始响应**可无损还原**成
    `GameRecord`（`record_from_raw`），于是「同一条牌谱查第二次」可以零网络请求 ——
    这是牌谱查询命令最重要的降风控手段（`code=540` 是账号级风控，见铁律 4）。
    """
    conn = await _get_conn()
    ver = await version_str()          # ★ 与登录同源：客户端更新后自动跟随
    last_err, last_code = "", 0
    for attempt in range(retries + 1):
        rq = pb.ReqGameRecord()
        rq.game_uuid = game_uuid
        rq.client_version_string = ver
        try:
            res = await _call(conn.channel, "Lobby", "fetchGameRecord",
                              rq.SerializeToString(), pb.ResGameRecord)
        except ProviderError as e:
            last_err = str(e)
            conn = await _get_conn(force_new=True)   # 掉线自愈后重试
            continue
        if _ok(res):
            return res.SerializeToString()
        last_err = _err(res)
        last_code = int(res.error.code) if res.HasField("error") else 0
        if last_code in BUSY_CODES and attempt < retries:
            await asyncio.sleep(1.0 * (attempt + 1))
            continue
        break
    raise ProviderError(f"拉取牌谱失败（{game_uuid}）：{last_err}", code=last_code)


def record_from_raw(raw: bytes, game_uuid: str = "") -> GameRecord:
    """把 `fetch_game_record_raw()` 的字节（或本地缓存文件）重建为 `GameRecord`。

    ★ 纯本地操作、零网络请求（铁律 22 合规：读缓存不该占用协议额度）。
    """
    res = pb.ResGameRecord()
    res.ParseFromString(raw)
    return _to_record(res, game_uuid or str(getattr(res.head, "uuid", "") or ""))


async def fetch_game_record(game_uuid: str, *, retries: int = 2) -> GameRecord:
    """拉对局详情（对局结果 + 牌谱二进制）。

    ★ 雀魂对 `fetchGameRecord` 有频率限制：短时间内连续拉取会返回 `code=540`，
    故内置退避重试（1s → 2s）。
    """
    return record_from_raw(await fetch_game_record_raw(game_uuid, retries=retries), game_uuid)


# ---------------------------------------------------------------- 转换

def focus_player(brief: GameBrief, account_id: int) -> PlayerResult | None:
    """从对局摘要里取出关注玩家的单条结果（Feature-04 取段位分用）。"""
    for p in brief.players or []:
        if int(p.account_id) == int(account_id or 0):
            return p
    return None


def _to_brief(g, category: int) -> GameBrief:
    players = []
    for r in g.results:
        players.append(PlayerResult(
            account_id=int(r.account_id),
            nickname=r.nickname,
            seat=int(getattr(r, "seat", 0)),
            rank=int(r.rank),
            final_point=int(r.final_point),
            grading_score=int(r.grading_score),
            level_id=int(r.level.id) if r.HasField("level") else 0,
            level_id3=int(r.level3.id) if r.HasField("level3") else 0,
            level_score=int(r.level.score) if r.HasField("level") else 0,
            level_score3=int(r.level3.score) if r.HasField("level3") else 0,
        ))
    return GameBrief(
        uuid=g.uuid,
        start_time=int(g.start_time),
        end_time=int(g.end_time),
        tag=int(g.tag),
        sub_tag=int(g.sub_tag),
        category=category,
        rank=int(g.rank),
        final_point=int(g.final_point),
        players=players,
    )


def _to_record(res, game_uuid: str) -> GameRecord:
    head = res.head
    cfg = head.config
    meta = cfg.meta
    accounts = []
    for a in head.accounts:
        accounts.append({
            "account_id": int(a.account_id),
            "seat": int(a.seat),
            "nickname": a.nickname,
            # 牌谱 head.accounts 带该玩家的段位快照（level=四麻 / level3=三麻）
            "level_id": int(a.level.id) if a.HasField("level") else 0,
            "level_score": int(a.level.score) if a.HasField("level") else 0,
            "level_id3": int(a.level3.id) if a.HasField("level3") else 0,
            "level_score3": int(a.level3.score) if a.HasField("level3") else 0,
        })
    result_players = []
    for p in head.result.players:
        result_players.append({
            "seat": int(p.seat),
            "account_id": int(p.account_id) if hasattr(p, "account_id") else 0,
            "nickname": p.nickname if hasattr(p, "nickname") else "",
            "total_point": int(getattr(p, "total_point", 0)),
            "part_point_1": int(getattr(p, "part_point_1", 0)),
            "grading_score": int(getattr(p, "grading_score", 0)),
            "gold": int(getattr(p, "gold", 0)),
        })
    return GameRecord(
        uuid=head.uuid or game_uuid,
        start_time=int(head.start_time),
        end_time=int(head.end_time),
        category=int(cfg.category),
        mode=int(cfg.mode.mode),
        # ★ proto3 标量字段无 presence，不能用 HasField（会 ValueError），0 即未设置
        mode_id=int(meta.mode_id),
        room_id=int(meta.room_id),
        accounts=accounts,
        result_players=result_players,
        data=bytes(res.data),
        head=head,
    )
