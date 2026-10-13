# -*- coding: utf-8 -*-
"""雀魂役种 / 场次 / 模式映射表（纯数据，无依赖）。

- 役种 id 顺序取自开源 [yf-dev/majsoul-record](https://github.com/yf-dev/majsoul-record)
  `src/consts.py` 的 64 项表（原表韩语，此处译为日麻通用中文术语）。
- 场次/模式取值为 2026-09-18 实测（见《协议探测结果.md》§9.7.3）。
- 详见 `docs/specs/epic-majsoul-tracker/役种与场次映射表.md`。
"""

# 役种 id → 中文名（1~50 常用役；51~64 古役/地方役，常规对局不出现）
YAKU_NAMES: dict[int, str] = {
    1: "门前清自摸和", 2: "立直", 3: "加槓", 4: "岭上开花", 5: "海底捞月",
    6: "河底捞鱼", 7: "役牌·白", 8: "役牌·发", 9: "役牌·中", 10: "自风牌",
    11: "场风牌", 12: "断幺九", 13: "一杯口", 14: "平和", 15: "混全带幺九",
    16: "一气通贯", 17: "三色同顺", 18: "两立直", 19: "三色同刻", 20: "三槓子",
    21: "对对和", 22: "三暗刻", 23: "小三元", 24: "混老头", 25: "七对子",
    26: "纯全带幺九", 27: "混一色", 28: "两杯口", 29: "清一色", 30: "一发",
    31: "宝牌", 32: "红宝牌", 33: "里宝牌", 34: "拔北宝牌", 35: "天和",
    36: "地和", 37: "大三元", 38: "四暗刻", 39: "字一色", 40: "绿一色",
    41: "清老头", 42: "国士无双", 43: "小四喜", 44: "四槓子", 45: "九莲宝灯",
    46: "八连庄", 47: "纯正九莲宝灯", 48: "四暗刻单骑", 49: "国士无双十三面", 50: "大四喜",
    51: "燕返", 52: "岭上开花（古役）", 53: "十二落抬", 54: "五门齐", 55: "三连刻",
    56: "一色三顺", 57: "一筒摸月", 58: "九筒捞鱼", 59: "人和", 60: "大车轮",
    61: "大竹林", 62: "大数邻", 63: "石上三年", 64: "大七星",
}

# 需要单独点名的"大牌"（推送时可强调）
BIG_YAKUS = {
    "天和", "地和", "大三元", "四暗刻", "字一色", "绿一色", "清老头", "国士无双",
    "小四喜", "四槓子", "九莲宝灯", "纯正九莲宝灯", "四暗刻单骑", "国士无双十三面",
    "大四喜", "清一色", "两杯口", "大车轮", "大七星",
}

# fetchAccountInfoExtra 的 type / 返回项 tag
TAG_NAMES: dict[int, str] = {1: "段位场", 2: "友人局", 3: "其他"}

# `GameConfig.category` / `AccountPlayingGame.category`（对局摘要里的 category）——
# ★★ 2026-09-21 实测推翻旧假设：**它不是一个"麻种"字段**！8 个不同段位场次（含四麻与三麻）
#    实测全部 `category=2`；牌谱屋 `validations.rs` 实证 `config.category=1` = Friendly Match。
#    → 1=友人局、2=段位场、3=其他。麻种请看 `mode.mode` 或 `meta.mode_id`（见下）。
GAME_CATEGORY_NAMES: dict[int, str] = {1: "友人局", 2: "段位场", 3: "其他"}

# `fetchAccountInfoExtra(account_id, category, type)` 的**查询参数** category —— 这个才是麻种。
QUERY_CATEGORY_NAMES: dict[int, str] = {1: "四麻", 2: "三麻"}

# 对局模式 `mode.mode`（1/2=四麻，11/12=三麻；2026-09-21 实测 100% 吻合）
MODE_NAMES: dict[int, str] = {
    1: "四麻东风战", 2: "四麻半庄战", 11: "三麻东风战", 12: "三麻半庄战",
}

# ★ 段位场场次表：`meta.mode_id`（= `state.playing.meta.mode_id` = `GameConfig.meta.mode_id`
#   = 对局摘要的 `sub_tag`，三者同源）。2026-09-21 实测重建，见《协议探测结果.md》§9.11：
#   · 牌谱屋前端 `src/data/types/gameMode.ts` 给出 8/9=金、11/12=玉、15/16=王座、21-26=三麻金/玉/王座；
#   · 本插件实测 live 列表（filter=200+mode_id）段位分布 + 一张低段位账号牌谱（mode_id=2 = 铜之间·四人东，
#     玩家 level_id=101xx/102xx）+ 对局摘要 sub_tag 采样（17-20 为三麻低段、5/6 为雀杰场次）交叉印证；
#   · 规律：奇数=东场、偶数=南场；四麻 铜(2,3)/银(5,6)/金(8,9)/玉(11,12)/王座(15,16)；
#     三麻 铜(17,18)/银(19,20)/金(21,22)/玉(23,24)/王座(25,26)。
#   ⚠️ 1/4/7/10/13/14 等编号未观测到对局（怀疑是活动/比赛场次），走 `place_name` 的兜底分支。
MODE_ID_NAMES: dict[int, str] = {
    2: "铜之间·四人东", 3: "铜之间·四人南",
    5: "银之间·四人东", 6: "银之间·四人南",
    8: "金之间·四人东", 9: "金之间·四人南",
    11: "玉之间·四人东", 12: "玉之间·四人南",
    15: "王座之间·四人东", 16: "王座之间·四人南",
    17: "铜之间·三人东", 18: "铜之间·三人南",
    19: "银之间·三人东", 20: "银之间·三人南",
    21: "金之间·三人东", 22: "金之间·三人南",
    23: "玉之间·三人东", 24: "玉之间·三人南",
    25: "王座之间·三人东", 26: "王座之间·三人南",
}

# 段位场房间名（mode_id 未识别时只敢报到这个粒度）
RANKED_FALLBACK = "段位场"
# 三麻场次的编号起点（mode_id ≥ 17 即三麻；实测 17-26 全部三麻、2-16 全部四麻）
THREE_PLAYER_MODE_MIN = 17

# ★★ 2026-09-26 活动 / 大会场（用户口径：「所有活动局进行屏蔽，不做处理和汇报」）
#   实测（2026-09-26 用户打「修罗之战」，对局 `260926-71656251-…`，**4 人场**）：
#   · 摘要 `tag=3`（其他/大会；段位场恒 1、友人局 2）、`sub_tag=40`（= `meta.mode_id` 40）、
#     `category=1`、**全员 `grading_score=0`**（活动局不产生段位分变化）；
#   · 牌谱走**血战规则**（`RecordHuleXueZhanMid` / `HuInfoXueZhanMid`，和牌信息里**没有
#     `point_sum`**）→ 曾让 `paipu_parser` 抛 `AttributeError`、tick 崩、`seen` 未落盘 →
#     每轮重试同一局，直到把牌谱接口打成 `code=540` 账号级风控（见 monitor 铁律）。
#   → 摘要层用 `tag` 判活动（权威）；实时层只有 `mode_id`，按「有编号但不在已知段位场表」
#     保守判定（宁可漏播，也不能把活动局当段位场汇报）。
ACTIVITY_TAG = 3


def yaku_name(yaku_id: int) -> str:
    """役种 id → 中文名（未知 id 返回 `役种#<yaku_id>`）。"""
    return YAKU_NAMES.get(int(yaku_id), f"役种#{yaku_id}")


def is_big_yaku(yaku_id: int) -> bool:
    return yaku_name(yaku_id) in BIG_YAKUS


def tag_name(tag: int) -> str:
    return TAG_NAMES.get(int(tag), f"类型#{tag}")


def game_category_name(category: int) -> str:
    """`GameConfig.category`（1=友人局 / 2=段位场 / 3=其他）——**不是麻种**。"""
    return GAME_CATEGORY_NAMES.get(int(category), f"类别#{category}")


def query_category_name(category: int) -> str:
    """`fetchAccountInfoExtra` 查询参数 category（1=四麻 / 2=三麻）。"""
    return QUERY_CATEGORY_NAMES.get(int(category), f"类别#{category}")


def mode_name(mode: int) -> str:
    return MODE_NAMES.get(int(mode), f"模式#{mode}")


# 满贯档次（按番数；符数只在 3/4 番的临界档起作用）——2026-09-21 用户要求「详细到番/符/倍满」
def hand_rank_name(count: int, fu: int = 0) -> str:
    """番数（+符数）→ 满贯档次：满贯 / 跳满 / 倍满 / 三倍满 / 役满（不足则返回空串）。"""
    c = int(count or 0)
    f = int(fu or 0)
    if c >= 13:
        return "役满"
    if c >= 11:
        return "三倍满"
    if c >= 8:
        return "倍满"
    if c >= 6:
        return "跳满"
    if c >= 5:
        return "满贯"
    if c == 4 and f >= 40:
        return "满贯"
    if c == 3 and f >= 70:
        return "满贯"
    return ""


def mode_id_name(mode_id: int) -> str:
    """`mode_id` → 场次中文名（未识别返回空串）。"""
    return MODE_ID_NAMES.get(int(mode_id), "")


def is_three_player_mode(mode_id: int, mode: int = 0) -> bool:
    """判麻种（三麻？）：优先用 `mode.mode`（1/2=四麻、11/12=三麻），缺失时用 `mode_id` 编号段。"""
    m = int(mode or 0)
    if m in MODE_NAMES:
        return m >= 10
    return int(mode_id or 0) >= THREE_PLAYER_MODE_MIN


# ---------------------------------------------------------------- 活动 / 大会场判定（2026-09-26）

def is_activity_tag(tag: int) -> bool:
    """对局摘要 `tag` 是否属于**活动 / 大会场**（1=段位场、2=友人局、3=其他）。

    ★ 用户 2026-09-26 口径：「所有活动局进行屏蔽，不做处理和汇报」——**免拉牌谱**即可判定，
    避免活动牌谱（血战规则，字段缺失）被解析、也避免浪费牌谱接口额度。
    """
    return int(tag or 0) == ACTIVITY_TAG


def is_known_ranked_mode(mode_id: int) -> bool:
    """`mode_id` 是否属于已知的常规段位场（见 `MODE_ID_NAMES`）。"""
    return int(mode_id or 0) in MODE_ID_NAMES


def is_activity_playing(mode_id: int, room_id: int = 0) -> bool:
    """实时「对局中」快照是否判为**活动 / 其它场**（保守判定）。

    实时快照（`fetchFriendList`）**没有摘要的 `tag`**，只有 `mode_id` / `room_id`，故：
    - `room_id > 0` → 自定义房（友人局），不是活动；
    - `mode_id == 0` → 无场次信息，**不判**活动（不误杀）；
    - 其余：不在 `MODE_ID_NAMES` 里 → 判活动（实测活动局 `mode_id=40` 不在表内，
      而常规段位场 2~26 全在表内）。

    ⚠️ 取舍：雀魂若新增段位场编号（如 1/4/7/10/13/14 等从未观测到的编号被启用为段位场），
    这里会漏播其「对局开始」——宁可漏播，也不把活动局当段位场汇报（用户口径）。
    """
    if int(room_id or 0) > 0:
        return False
    mid = int(mode_id or 0)
    if mid == 0:
        return False
    return mid not in MODE_ID_NAMES


def _players_word(mode_id: int, mode: int = 0) -> str:
    return "三人" if is_three_player_mode(mode_id, mode) else "四人"


def _wind_word(mode_id: int, mode: int = 0) -> str:
    """东/南场：`mode` 有值时 1/11=东、2/12=南；否则按 mode_id 奇偶（实测奇数=东）。"""
    m = int(mode or 0)
    if m:
        return "东" if m in (1, 11) else "南"
    return "东" if int(mode_id or 0) % 2 == 1 else "南"


def place_name(mode_id: int, room_id: int = 0, mode: int = 0) -> str:
    """场次展示名（★ 2026-09-21 重写：房间/麻种由 `mode_id`+`mode` 决定，**不再用 category**）。

    - 自定义房（`room_id>0`）→ 「友人局（房间 xxxxx）」由调用方拼装，这里给「友人局·四人」；
    - 段位场已知场次 → 「玉之间·四人南」；
    - 未识别编号 → 兜底「段位场·四人东」（麻种+东南，不猜房间）。

    ⚠️ 2026-09-26：未识别编号的兜底会把**活动场**写成「段位场·三人南」（用户实测 bug，
    `mode_id=40` 的修罗之战）。调用方**必须先用 `is_activity_tag` / `is_activity_playing`
    过滤活动局**（本插件现在直接屏蔽活动局，不再走到这里）。
    """
    if room_id and int(room_id) > 0:
        # 自定义房：`mode_id` 恒为 0（无场次信息），麻种只在拿到 `mode.mode` 时才敢说
        if int(mode or 0) in MODE_NAMES:
            return f"友人局·{_players_word(mode_id, mode)}{_wind_word(mode_id, mode)}"
        return "友人局"
    named = mode_id_name(mode_id)
    if named:
        return named
    if not int(mode_id or 0):
        return RANKED_FALLBACK            # 无场次信息（room_id 也为 0 的异常组合）
    return f"{RANKED_FALLBACK}·{_players_word(mode_id, mode)}{_wind_word(mode_id, mode)}"


def place_full(mode: int, mode_id: int, room_id: int) -> str:
    """完整场次描述（含模式/房间），如「玉之间·四人南（四麻半庄战）」。"""
    place = place_name(mode_id, room_id, mode)
    mode_txt = mode_name(mode)
    if not mode_txt.startswith("模式#"):
        place = f"{place}（{mode_txt}）"
    if room_id and int(room_id) > 0:
        place = f"{place}·房间 {room_id}"
    return place
