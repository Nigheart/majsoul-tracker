# -*- coding: utf-8 -*-
"""图片卡片渲染（Pillow，纯函数，不依赖 nonebot，不直接读写插件数据）。

职责：把结构化 payload 渲染成 **PNG bytes**，供推送出口发图。

- **素材**：`assets/tiles/*.png`（雀魂风格牌图 37 张，含 `0m/0p/0s` 赤牌）；
- **头像**：QQ 头像走 `qlogo` CDN，内存缓存（成功 6 小时 / 失败 10 分钟），失败画占位圆；
- **主题**：`dark`（默认）/ `light`；
- **契约**：本模块**不吞异常**——渲染失败一律抛出，由调用方回退纯文本推送；
  未安装 Pillow 时 `available()` 返回 False，调用方直接走文本。

卡片类型：`render_result`（对局结果）/ `render_room_battle`（同室战报）/
`render_playing_start`（对局开始）/ `render_record`（`/雀魂战绩`，P2）。
"""
from __future__ import annotations

import io
import math
import os
import re
import time
from pathlib import Path
from typing import Any

try:                                              # Pillow 是硬依赖；缺失时优雅降级
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except Exception:  # noqa: BLE001
    HAS_PIL = False

W = 640                                           # 画布宽（QQ 端观感最佳）
H_MAX = 1400                                      # 绘制用画布高（最后按内容裁切）
# ★ AI 复盘卡专用上限：**详细模式**要列出全部失误/危险牌，图可以很长
#   （实测 12 局 50 手失误 + 10 手危险牌 ≈ 6500px）；仍留一个硬顶防呆。
H_AI_MAX = 16000

ASSETS = Path(__file__).resolve().parent / "assets" / "tiles"
LEVEL_DIR = Path(__file__).resolve().parent / "assets" / "levels"   # ★ 官方段位图标（P3）

THEMES = {
    "dark": {
        "bg": (14, 24, 19), "panel": (24, 38, 30), "panel2": (32, 48, 38),
        "bar": (18, 32, 25), "bar_bg": (10, 18, 14), "border": (52, 82, 64),
        "text": (237, 230, 214),
        "sub": (150, 172, 156), "gold": (227, 195, 107), "gold_dim": (150, 126, 66),
        "red": (226, 96, 79), "green": (95, 191, 127), "blue": (91, 155, 224),
        "silver": (196, 203, 210), "orange": (214, 148, 78), "grid": (44, 64, 52),
        "ming_bg": (38, 44, 40),
    },
    "light": {
        "bg": (240, 242, 245), "panel": (255, 255, 255), "panel2": (246, 247, 250),
        "bar": (228, 233, 238), "bar_bg": (232, 236, 242), "border": (206, 213, 222),
        "text": (38, 46, 56),
        "sub": (110, 122, 136), "gold": (176, 132, 26), "gold_dim": (198, 168, 96),
        "red": (206, 70, 60), "green": (32, 138, 84), "blue": (46, 108, 210),
        "silver": (130, 140, 152), "orange": (200, 124, 46), "grid": (222, 228, 236),
        "ming_bg": (236, 239, 243),
    },
}

_FONTS: dict = {}
_TILES: dict = {}
_AVATARS: dict = {}
_AVATAR_TTL_OK = 6 * 3600
_AVATAR_TTL_FAIL = 600


def available() -> bool:
    """Pillow 是否可用（不可用则调用方走纯文本）。"""
    return HAS_PIL


# ---------------------------------------------------------------- 字体 / 主题

def _font(size: int, bold: bool = False):
    """中文字体探测链：Windows（微软雅黑/黑体）→ Linux（Noto CJK/文泉驿）→ fc-match。"""
    key = (size, bold)
    if key in _FONTS:
        return _FONTS[key]
    win = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
    cands = []
    for n in (["msyhbd.ttc", "msyh.ttc", "simhei.ttf"] if bold
              else ["msyh.ttc", "msyhbd.ttc", "simhei.ttf"]):
        p = os.path.join(win, n)
        if os.path.exists(p):
            cands.append(p)
    cands += [p for p in (
        "/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    ) if os.path.exists(p)]
    if not cands:
        try:
            out = os.popen("fc-match -f '%{file}' :lang=zh 2>/dev/null").read().strip()
            if out and os.path.exists(out):
                cands.append(out)
        except Exception:  # noqa: BLE001
            pass
    f = None
    for p in cands:
        try:
            f = ImageFont.truetype(p, size)
            break
        except Exception:  # noqa: BLE001
            continue
    _FONTS[key] = f or ImageFont.load_default()
    return _FONTS[key]


def _theme(name: str) -> dict:
    return THEMES.get(str(name or "dark").lower(), THEMES["dark"])


EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u2B00-\u2BFF]")


def strip_emoji(s: Any) -> str:
    """去掉 emoji：中文字体无彩色 emoji 字形，画出来是方框。"""
    return EMOJI_RE.sub("", str(s or "")).strip()


# ---------------------------------------------------------------- 基元

def _panel(d, x, y, w, h, th, fill=None, outline=None, r=10, width=1):
    d.rounded_rectangle([x, y, x + w, y + h], radius=r,
                        fill=fill or th["panel"], outline=outline or th["border"], width=width)


def _txt(d, xy, s, size, th, color=None, bold=False, anchor=None):
    d.text(xy, str(s), font=_font(size, bold), fill=color or th["text"], anchor=anchor)


def _progress(d, x, y, w, h, pct, th, color=None):
    """进度条：**槽用更暗的底色 + 细描边**（深色主题下也必须看得见轨道），填充按 pct 截断。

    ★ 2026-09-22 修复（用户反馈「进度条显示不正确」）：原来槽色用 `panel2`，与面板底 `panel`
    几乎同色 → 只看得见金色填充、看不见轨道；而「上一局」银线常落在填充之外，看上去像悬空错线。
    """
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2,
                        fill=th.get("bar_bg", th["panel2"]), outline=th["border"], width=1)
    pct = max(0.0, min(1.0, float(pct)))
    if pct > 0:
        fill_w = max(h, int(w * pct))          # 填充最短也画成圆头，避免小比例时看不见
        d.rounded_rectangle([x + 1, y + 1, min(x + w - 1, x + fill_w), y + h - 1],
                            radius=max(1, (h - 2) // 2), fill=color or th["gold"])


def _star(d, cx, cy, r, fill):
    """画一颗五角星（外接半径 r，内半径 0.45r）。"""
    pts = []
    for i in range(10):
        ang = math.radians(-90 + i * 36)
        rr = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    d.polygon(pts, fill=fill)


def _stars(d, cx, cy, n: int, r: float, color) -> None:
    """在 (cx, cy) 居中横排画 n 颗五角星（段位星数）。"""
    n = max(0, min(int(n or 0), 3))
    if n <= 0:
        return
    gap = r * 2.4
    x0 = cx - gap * (n - 1) / 2
    for i in range(n):
        _star(d, x0 + gap * i, cy, r, color)


STAR_ROW_H = 14          # 徽章**下方**星级占用的高度（默认版式，调用方需预留）
STAR_COL_W = 16          # ★ 2026-09-24：星级画在图标**右侧竖排**时占用的宽度（行高受限场景）


def _ellipsize(d, s, f, max_w):
    s = str(s or "")
    if d.textlength(s, font=f) <= max_w:
        return s
    while s and d.textlength(s + "…", font=f) > max_w:
        s = s[:-1]
    return s + "…"


def _header(d, title, th, right=""):
    d.rectangle([0, 0, W, 46], fill=th["bar"])
    d.rectangle([0, 45, W, 47], fill=th["gold_dim"])
    d.rectangle([20, 15, 24, 31], fill=th["gold"])
    _txt(d, (34, 23), title, 19, th, color=th["gold"], bold=True, anchor="lm")
    if right:
        _txt(d, (W - 20, 24), right, 13, th, color=th["sub"], anchor="rm")


def _stat_box(d, x, y, w, h, label, value, th, color=None, big=30):
    _panel(d, x, y, w, h, th, fill=th["panel2"])
    _txt(d, (x + w // 2, y + h // 2 - 10), value, big, th, color=color or th["text"],
         bold=True, anchor="mm")
    _txt(d, (x + w // 2, y + h - 18), label, 13, th, color=th["sub"], anchor="mm")


_PNG_SOFT_LIMIT = 800 * 1024      # 超过此体积才尝试调色板压缩（普通卡本来就 <300KB）


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    if len(data) <= _PNG_SOFT_LIMIT:
        return data
    # ★ 详细模式的长图（实测 640×6470 → 2.0MB）用调色板重编码可降到 ~0.7MB，
    #   深色 UI + 牌图用 256 色几乎无视觉差异；取最小者，**绝不变大**。
    best = data
    for colors in (256, 128):
        try:
            b2 = io.BytesIO()
            img.quantize(colors=colors, method=Image.MEDIANCUT).save(
                b2, "PNG", optimize=True)
        except Exception:  # noqa: BLE001 —— 压缩失败就用原图，绝不影响出图
            break
        alt = b2.getvalue()
        if len(alt) < len(best):
            best = alt
        if len(best) <= _PNG_SOFT_LIMIT:
            break
    return best


# ---------------------------------------------------------------- 牌图 / 头像

def _tile(code: str, h: int):
    """按牌编码取牌图（等比缩放到高 h）；素材缺失或 Pillow 不可用返回 None。"""
    if not HAS_PIL:
        return None
    key = (str(code), int(h))
    if key in _TILES:
        return _TILES[key]
    im = None
    fp = ASSETS / f"{code}.png"
    if fp.exists():
        try:
            src = Image.open(fp).convert("RGBA")
            im = src.resize((max(1, int(src.width * h / src.height)), int(h)), Image.LANCZOS)
        except Exception:  # noqa: BLE001
            im = None
    _TILES[key] = im
    return im


def _paste_tile(img, x, y, code, w, h, th):
    im = _tile(str(code), h)
    if im is None:
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([x, y, x + w, y + h], radius=4,
                            fill=th["panel2"], outline=th["border"])
        _txt(d, (x + w // 2, y + h // 2), str(code), 11, th, color=th["sub"], anchor="mm")
        return
    if im.width != w:
        im = im.resize((int(w), int(h)), Image.LANCZOS)
    img.paste(im, (int(x), int(y)), im)


def _avatar_raw(qq: str) -> bytes | None:
    """拉 QQ 头像（带两级 TTL 缓存）；失败缓存 10 分钟避免反复重试。"""
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return None
    now = time.time()
    hit = _AVATARS.get(qq)
    if hit is not None:
        data, ts, ok = hit
        if now - ts < (_AVATAR_TTL_OK if ok else _AVATAR_TTL_FAIL):
            return data
    data = None
    try:
        import httpx
        r = httpx.get(f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640", timeout=6,
                      follow_redirects=True)
        if r.status_code == 200 and len(r.content) > 200:
            data = r.content
    except Exception:  # noqa: BLE001 —— 头像拿不到不影响卡片
        data = None
    _AVATARS[qq] = (data, now, data is not None)
    return data


def _circle_avatar(qq: str, size: int, th):
    """圆形头像（带描边）；拿不到返回 None。"""
    raw = _avatar_raw(qq)
    if not raw:
        return None
    try:
        src = Image.open(io.BytesIO(raw)).convert("RGBA").resize((size, size), Image.LANCZOS)
    except Exception:  # noqa: BLE001
        return None
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(src, (0, 0), mask)
    ImageDraw.Draw(out).ellipse((0, 0, size - 1, size - 1), outline=th["gold_dim"], width=2)
    return out


def _paste_avatar(img, x, y, size, qq, th):
    av = _circle_avatar(str(qq or ""), size, th)
    if av is None:
        d = ImageDraw.Draw(img)
        d.ellipse([x, y, x + size, y + size], fill=th["panel2"], outline=th["border"])
        _txt(d, (x + size // 2, y + size // 2), "QQ", 14, th, color=th["sub"], anchor="mm")
        return
    img.paste(av, (int(x), int(y)), av)


# ---------------------------------------------------------------- 和牌牌型

SUIT_ORDER = {"m": 0, "p": 1, "s": 2, "z": 3}


def tile_key(t: str) -> tuple:
    """牌排序键：万→筒→索→字；赤 5（`0m/0p/0s`）按 5 排。"""
    t = str(t)
    if len(t) < 2:
        return (9, 0)
    num, suit = t[:-1], t[-1]
    try:
        n = 5 if num == "0" else int(num)
    except ValueError:
        n = 0
    return (SUIT_ORDER.get(suit, 9), n)


MING_RE = re.compile(r"(\w+)\(([^)]*)\)")


def parse_ming(ming_list) -> list:
    """`['angang(3p,3p,3p,3p)']` → `[{'kind':'angang','tiles':[...]}]`。"""
    out = []
    for s in (ming_list or []):
        m = MING_RE.match(str(s))
        if m:
            out.append({"kind": m.group(1),
                        "tiles": [t.strip() for t in m.group(2).split(",") if t.strip()]})
    return out


def _draw_hand(img, d, x, y, hand: dict, max_w: int, th,
               marks: dict[int, tuple] | None = None) -> int:
    """绘制和牌牌型（副露组灰底 + 暗牌 + 和牌张金框），自适应尺寸；返回占用高度。

    `marks`：`{牌序号: 框色}`——按**绘制顺序**（副露组 → 暗牌 → 和牌张）给指定牌加**原位彩色外框**，
    用于 AI 复盘卡标注「实际打出（红框）/ AI 建议（金框）」，**不改变牌的排列**（默认 `None` 时
    行为与老版本完全一致）。
    """
    mings = hand.get("mings") or []
    tiles = [t for g in mings for t in g["tiles"]] + list(hand.get("hand") or []) + \
            [hand.get("win_tile") or ""]
    tiles = [t for t in tiles if t]
    n = len(tiles)
    if n <= 0:
        return 0
    n_gap_grp = len(mings)
    gap_in, gap_grp = 2, 10
    avail = max_w - (n - 1) * gap_in - n_gap_grp * gap_grp
    w = int(min(46, max(16, avail / n)))
    h = int(w / 0.62)

    def _mark(cx0: int, i: int) -> None:
        if marks and i in marks:
            d.rounded_rectangle([cx0 - 2, y - 2, cx0 + w + 2, y + h + 2], radius=4,
                                outline=marks[i], width=2)

    cx = x
    idx = 0
    for g in mings:
        gw = len(g["tiles"]) * w + (len(g["tiles"]) - 1) * gap_in
        d.rounded_rectangle([cx - 3, y - 4, cx + gw + 3, y + h + 4], radius=4, fill=th["ming_bg"])
        for t in g["tiles"]:
            _paste_tile(img, cx, y, t, w, h, th)
            _mark(cx, idx)
            idx += 1
            cx += w + gap_in
        cx += gap_grp - gap_in
    for t in (hand.get("hand") or []):
        _paste_tile(img, cx, y, t, w, h, th)
        _mark(cx, idx)
        idx += 1
        cx += w + gap_in
    if hand.get("win_tile"):
        d.rounded_rectangle([cx - 2, y - 2, cx + w + 2, y + h + 2], radius=4,
                            outline=th["gold"], width=2)
        _paste_tile(img, cx, y, hand["win_tile"], w, h, th)
        _mark(cx, idx)
    return h


def _finish(img, y_end: int, bg=None):
    """按内容底部裁切画布；`y_end` 越界时用**背景色**补齐（★ 绝不出现纯黑带）。

    ⚠️ Pillow 的 `crop` 对超出画布的区域用**纯黑 (0,0,0)** 填充——2026-09-28
    牌谱明细卡因高度估算偏小，底部出现 74 行纯黑盖住内容（用户报的「图片下方有黑条」）。
    这里加一道兜底：越界即新建背景色画布再裁，最坏是背景留白，**不会再是黑条**。
    （根治手段在 `render_paipu_detail`：按实测高度重绘，内容绝不被裁。）
    """
    y_end = max(120, int(y_end))
    if y_end > img.height:
        if bg is None:
            px = img.getpixel((0, 0))
            bg = tuple(px)[:3] if isinstance(px, tuple) else px
        canvas = Image.new("RGB", (img.width, y_end), bg)
        canvas.paste(img, (0, 0))
        img = canvas
    return img.crop((0, 0, W, y_end))


# ---------------------------------------------------------------- 卡：对局结果

DIGITS = "①②③④⑤⑥⑦⑧⑨"


def _rank_color(rank: int, th) -> tuple:
    return {1: th["gold"], 2: th["silver"], 3: th["orange"], 4: th["red"]}.get(rank, th["text"])


# ---------------------------------------------------------------- 段位徽章（P3）

# 大段位配色（雀魂观感）：初心灰 / 雀士绿 / 雀杰蓝 / 雀豪紫 / 雀圣金 / 魂天红
MAJOR_COLORS = {
    1: (150, 158, 168), 2: (95, 191, 127), 3: (91, 155, 224),
    4: (176, 124, 226), 5: (227, 195, 107), 6: (226, 96, 79),
}
MAJOR_NAMES = {1: "初心", 2: "雀士", 3: "雀杰", 4: "雀豪", 5: "雀圣", 6: "魂天"}

# 官方段位图标文件名（`extendRes/level/`）；麻种 1=四麻用 `sima_`、2=三麻用 `sanma_`、其他用通用版
LEVEL_ICON_FILES = {1: "fish", 2: "queshi", 3: "quejie", 4: "quehao", 5: "quesheng",
                    6: "huntian"}
CAT_ICON_PREFIX = {1: "sima_", 2: "sanma_"}
_LEVEL_ICONS: dict = {}


def _level_icon(cat: int, major: int, size: int):
    """取官方段位图标（先试麻种专用版，再试通用版）；无素材返回 None。

    图标按最长边等比缩放到 `size×size` 透明画布居中（通用版是 262×201 横版，方形版 256×256）。
    """
    base = LEVEL_ICON_FILES.get(major)
    if not base:
        return None
    key = (int(cat or 0), int(major), int(size))
    if key in _LEVEL_ICONS:
        return _LEVEL_ICONS[key]
    out = None
    for name in (f"{CAT_ICON_PREFIX.get(cat, '')}{base}", base):
        fp = LEVEL_DIR / f"{name}.png"
        if not fp.exists():
            continue
        try:
            src = Image.open(fp).convert("RGBA")
            scale = size / max(src.width, src.height)
            w, h = max(1, int(src.width * scale)), max(1, int(src.height * scale))
            im = src.resize((w, h), Image.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.paste(im, ((size - w) // 2, (size - h) // 2), im)
            out = canvas
            break
        except Exception:  # noqa: BLE001
            continue
    _LEVEL_ICONS[key] = out
    return out


def _parse_level_id(level_id: Any) -> tuple[int, int]:
    """`level.id` → `(大段位 1-6, 星 1-3)`（card 自足解析，规则同 `rank_level`）。"""
    try:
        lid = int(level_id or 0)
    except (TypeError, ValueError):
        return 0, 0
    if lid <= 0:
        return 0, 0
    major, star = (lid // 100) % 100, lid % 100
    if not (1 <= major <= 6) or star < 1:
        return 0, 0
    return major, star


def _badge(img, d, x, y, size: int, th, level_id: Any, star_side: bool = False) -> None:
    """段位徽章：**优先贴雀魂官方图标**（`assets/levels/`），缺失时回退自绘六边形。

    ★ 2026-09-22（用户反馈「你画的太难看了」）：官方段位图标已找到——路径
    `extendRes/level/{fish,queshi,quejie,quehao,quesheng,huntian}.png`，另有四麻 `sima_*`、
    三麻 `sanma_*` 方形版（256×256）。本插件用方形版按麻种取图（`assets/levels/`，解密 PNG）。

    ★ 2026-09-24 `star_side`：星级改画在**图标右侧竖排**（用户原话「星级再段位标识右边竖着排列」）
    ——用于全玩家统计表这种**行高有限**的场景：图标下方放星会超出 48px 行高导致徽章被裁掉
    （用户反馈「新的示例段位标识又消失了」）。此时图标按 `size` 画、星占右侧 `STAR_COL_W`。
    """
    major, star = _parse_level_id(level_id)
    if not major:
        return
    try:
        cat = int(level_id or 0) // 10000
    except (TypeError, ValueError):
        cat = 0
    icon = _level_icon(cat, major, size)
    if icon is not None:
        img.paste(icon, (int(x), int(y)), icon)
        if star:
            r = max(4.0, size * 0.115)
            if star_side:                        # ★ 右侧竖排（行高受限场景）
                cx = x + size + STAR_COL_W / 2
                gap = r * 2.6
                y0 = y + size / 2 - gap * (star - 1) / 2
                for i in range(star):
                    _star(d, cx, y0 + gap * i, r, th["gold"])
            else:                                # 图标正下方（默认，见铁律 17）
                _stars(d, x + size / 2, y + size + STAR_ROW_H / 2, star, r, th["gold"])
        return
    # ---- 以下为「官方图标缺失」时的自绘回退 ----
    color = MAJOR_COLORS.get(major, th["gold"])
    cx, cy, r = x + size / 2, y + size / 2, size / 2
    hex_pts = [(cx + r * math.cos(math.radians(60 * i - 90)),
                cy + r * math.sin(math.radians(60 * i - 90))) for i in range(6)]
    d.polygon(hex_pts, fill=color)
    inner = [(cx + r * 0.82 * math.cos(math.radians(60 * i - 90)),
              cy + r * 0.82 * math.sin(math.radians(60 * i - 90))) for i in range(6)]
    d.polygon(inner, fill=th["panel"])
    name = MAJOR_NAMES.get(major, "")
    if name:
        _txt(d, (cx, cy - size * 0.06), name, max(10, int(size * 0.34)), th,
             color=color, bold=True, anchor="mm")
    if star:                                     # 星点：徽章右下角（自绘版不受行高影响）
        dot_r = max(2, int(size * 0.06))
        gap = dot_r * 2.6
        sx = cx + size * 0.30 - gap * (star - 1) / 2
        for i in range(min(star, 3)):
            px = sx + gap * i
            py = cy + size * 0.30
            d.ellipse([px - dot_r, py - dot_r, px + dot_r, py + dot_r], fill=color)


def _level_panel(img, d, x, y, w, th, info: dict) -> int:
    """段位分面板：**本段起始分 / 当前分 / 本段上限（升级分）** + 上一局位置与变化 + 段位徽章。

    `info` = `{level_id, level_name, init, cur, prev, nxt, pt}`：
    - `init` = 本段初始分（`rank_level.init_score`）
    - `nxt`  = 本段**最大分**（升级分，`rank_level.upgrade_score`）
    - `prev` = **上一局结束后的分数**（与 `cur` 之差即本局变化）
    进度条按 `[init, nxt]` 归一化，并在**上一局分数位置**画一条银线便于看出涨跌。返回占用高度。
    """
    h = 92
    _panel(d, x, y, w, h, th)
    _badge(img, d, x + 14, y + 14, 52, th, info.get("level_id"))   # 下方留出星级空间
    px0, pw = x + 80, w - 96
    name = info.get("level_name") or ""
    cur = int(info.get("cur") or 0)
    nxt = int(info.get("nxt") or 0)
    init = int(info.get("init") or 0)
    prev = int(info.get("prev") or 0)
    pt = int(info.get("pt") or 0)

    _txt(d, (px0, y + 24), name, 16, th, color=th["gold"], bold=True, anchor="lm")
    if prev:
        col = th["green"] if pt >= 0 else th["red"]
        _txt(d, (x + w - 16, y + 24), f"上局 {prev} → {cur}（{pt:+d}）", 14, th,
             color=col, anchor="rm")
    else:
        _txt(d, (x + w - 16, y + 24), f"当前 {cur}", 14, th, color=th["sub"], anchor="rm")

    by = y + 48
    if nxt > 0:                                # 有区间才画进度条（魂天/未知不画）
        # ★ 2026-09-22 用户纠正：「所有的段位分的进度条起始都应该是 0」——
        #   基准 = 0 → 升级分（本段最大分），而不是「本段起始分 → 升级分」。
        _progress(d, px0, by, pw, 12, cur / nxt, th)
        if prev:                               # 上一局分数位置（银线 + 顶部小三角，同一 0 基准）
            r = min(1.0, max(0.0, prev / nxt))
            mx = px0 + pw * r
            d.line([mx, by - 8, mx, by + 20], fill=th["silver"], width=3)
            d.polygon([(mx - 4, by - 11), (mx + 4, by - 11), (mx, by - 4)],
                      fill=th["silver"])
        _txt(d, (px0, y + 76), "0", 12, th, color=th["sub"], anchor="lm")
        _txt(d, (px0 + pw // 2, y + 76), f"当前 {cur}", 13, th, color=th["gold"], bold=True,
             anchor="mm")
        _txt(d, (px0 + pw, y + 76), f"升级 {nxt}", 12, th, color=th["sub"], anchor="rm")
    else:
        _txt(d, (px0, y + 76), f"当前 {cur}", 13, th, color=th["gold"], bold=True, anchor="lm")
    return h


# ---------------------------------------------------------------- 全玩家统计表（2026-09-24）

# 列定义：统计列（键, 表头）。顺序＝立直/荣和/自摸/放铳（用户截图口径）。
STAT_COLS = (("liqi", "立直"), ("rong", "荣和"), ("zimo", "自摸"), ("shokou", "放铳"))


def _players_table(img, d, x, y, w, th, players: list) -> int:
    """全玩家统计表（对局结果卡新增区块）。

    ★ 2026-09-24（用户需求，原话「包括对局玩家、事件、每个玩家的段位和点数变化，
      以及立直/荣和/自摸/放铳 的次数」）——每位玩家一行。

    ★ 版式（2026-09-24 用户第二轮反馈后定稿）：
      - **段位图标与 PT 放在一起**（原话「PT应该和段位标识放在一起」）→ 行首左侧
        一列：段位徽章 + 其右侧 PT 数值，形成「段位/PT」复合列；
      - **得点、变化列加宽**（原话「这些数字都比较大，现在叠在一起了」）→ 得点 88px、
        变化 78px，字号 14，右对齐留足位数（`77100`/`+42100` 不再重叠）；
      - 统计列各 38px，昵称列自适应剩余宽度。

    `players[]` 结构：`{rank, qq, name, point, delta, pt, level_id,
    agari, liqi, rong, zimo, shokou}`；缺字段一律按 0 处理（向后兼容）。
    返回占用高度（★ 动态计算，不硬编码画布高度）。
    """
    rows = sorted(players, key=lambda p: int(p.get("rank") or 99))
    if not rows:
        return 0
    head_h, row_h = 26, 44       # ★ 无头像后行高 48 → 44（徽章 34px 仍留 5px 余量）
    h = head_h + row_h * len(rows) + 8
    _panel(d, x, y, w, h, th)

    # ---- 列布局（从右往左排布，剩余给昵称）----
    stat_w = 38                                   # 四个统计列
    pt_w = 52                                     # PT 数值
    badge_sz = 34                                 # ★ 段位图标放大（用户要求，行高 48 放得下）
    badge_w = badge_sz + STAR_COL_W               # 图标 + 右侧竖排星级
    point_w = 88                                  # ★ 加宽：终局得点（最大 6 位如 77100）
    delta_w = 78                                  # ★ 加宽：点数变化（如 +42100）
    pad = 14
    right = x + w - pad
    dlt_x = right - delta_w
    pnt_x = dlt_x - point_w
    stat_x0 = pnt_x - stat_w * len(STAT_COLS)
    pt_x = stat_x0 - pt_w
    badge_x = pt_x - badge_w
    # ★ 2026-09-24 用户口径「对局细节就不要头像了」——**明细行不画 QQ 头像**，
    #   省下的横向空间给昵称（长昵称不再被截断），行高也从 48 收到 44。
    name_x = x + 34
    name_w = max(60, badge_x - 10 - name_x)

    # ---- 表头 ----
    hy = y + head_h // 2 + 3
    _txt(d, (x + 18, hy), "#", 12, th, color=th["sub"], anchor="lm")
    _txt(d, (name_x, hy), "玩家", 12, th, color=th["sub"], anchor="lm")
    _txt(d, (badge_x + badge_w // 2, hy), "段位/PT", 12, th,
         color=th["sub"], anchor="mm")
    for i, (_k, label) in enumerate(STAT_COLS):
        _txt(d, (stat_x0 + stat_w * i + stat_w // 2, hy), label, 12, th,
             color=th["sub"], anchor="mm")
    _txt(d, (pnt_x + point_w - 4, hy), "得点", 12, th, color=th["sub"], anchor="rm")
    _txt(d, (right, hy), "变化", 12, th, color=th["sub"], anchor="rm")
    d.line([x + 12, y + head_h, x + w - 12, y + head_h], fill=th["grid"], width=1)

    for i, pl in enumerate(rows):
        ry = y + head_h + row_h * i
        rank = int(pl.get("rank") or 0)
        if i % 2 == 0:
            d.rounded_rectangle([x + 6, ry + 2, x + w - 6, ry + row_h - 2], radius=6,
                                fill=th["panel2"])
        if rank == 1:
            d.rounded_rectangle([x + 6, ry + 2, x + 10, ry + row_h - 2], radius=2,
                                fill=th["gold"])
        cy = ry + row_h // 2
        _txt(d, (x + 20, cy), str(rank), 16, th, color=_rank_color(rank, th),
             bold=True, anchor="lm")
        # ★ 2026-09-24：明细行**不画头像**（用户口径），直接排昵称
        _txt(d, (name_x, cy), _ellipsize(d, pl.get("name", ""), _font(15), name_w),
             15, th, bold=(rank == 1), anchor="lm")
        # ★ 段位徽章 + PT（同列相邻）；徽章放大、星级在图右侧竖排（行高受限）
        if pl.get("level_id"):
            _badge(img, d, badge_x, cy - badge_sz // 2, badge_sz, th,
                   pl["level_id"], star_side=True)
        pt = int(pl.get("pt") or 0)
        _txt(d, (pt_x + pt_w - 4, cy), f"{pt:+d}", 14, th,
             color=th["green"] if pt > 0 else (th["red"] if pt < 0 else th["sub"]),
             bold=bool(pt), anchor="rm")
        # 四项统计
        for j, (key, _label) in enumerate(STAT_COLS):
            v = int(pl.get(key) or 0)
            col = th["sub"]
            if v:
                col = th["red"] if key == "shokou" else th["text"]
            _txt(d, (stat_x0 + stat_w * j + stat_w // 2, cy), str(v), 15, th,
                 color=col, bold=bool(v), anchor="mm")
        # 得点 / 变化（加宽后右对齐，大数字不再重叠）
        _txt(d, (pnt_x + point_w - 4, cy), str(int(pl.get("point") or 0)), 15, th,
             anchor="rm")
        dl = int(pl.get("delta") or 0)
        _txt(d, (right, cy), f"{dl:+d}", 14, th,
             color=th["green"] if dl >= 0 else th["red"], anchor="rm")
    return h + 14


# ---------------------------------------------------------------- 卡：对局结果

def render_result(p: dict) -> bytes:
    """对局结果卡。

    payload：`qq / nickname / majsoul_nick / time / place / rank / point / delta / pt /
    level_name / level_score / level_next / level_prev / hand / comments`。
    `hand` 结构见 `paipu_parser.extract_hules`（`hand`/`win_tile`/`mings`/`point`/`fans_txt`…）。
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow 不可用")
    th = _theme(p.get("theme"))
    comments = [strip_emoji(c) for c in (p.get("comments") or []) if strip_emoji(c)]
    img = Image.new("RGB", (W, H_MAX), th["bg"])
    d = ImageDraw.Draw(img)
    _header(d, "雀魂对局结果", th, p.get("place", ""))

    y = 60
    _paste_avatar(img, 24, y, 56, p.get("qq", ""), th)
    _txt(d, (96, y + 8), p.get("nickname", ""), 22, th, bold=True, anchor="lm")
    sub = ""
    nick2 = p.get("majsoul_nick") or ""
    if nick2 and nick2 != p.get("nickname"):
        sub = f"雀魂昵称：{nick2}"
    if p.get("time"):
        sub = (sub + " · " if sub else "") + str(p["time"])
    if sub:
        _txt(d, (96, y + 40), sub, 13, th, color=th["sub"], anchor="lm")

    y += 70
    bw = (W - 48 - 2 * 12) // 3
    rank = int(p.get("rank") or 0)
    rc = _rank_color(rank, th)
    _stat_box(d, 24, y, bw, 64, "名次", f"#{rank}", th, color=rc)
    delta = int(p.get("delta") or 0)
    _stat_box(d, 24 + bw + 12, y, bw, 64, "得点", f"{int(p.get('point') or 0)}", th,
              color=th["green"] if delta >= 0 else th["red"])
    pt = int(p.get("pt") or 0)
    _stat_box(d, 24 + 2 * (bw + 12), y, bw, 64, "段位分变动", f"{pt:+d}", th,
              color=th["green"] if pt >= 0 else th["red"])

    # 段位分面板（★ 本段起始 / 当前分 / 本段上限 + 上一局位置与变化 + 段位徽章）
    y += 78
    if p.get("level_name") or p.get("level_score"):
        y += _level_panel(img, d, 24, y, W - 48, th, {
            "level_id": p.get("level_id"), "level_name": p.get("level_name"),
            "init": p.get("level_init"), "cur": p.get("level_score"),
            "prev": p.get("level_prev"), "nxt": p.get("level_next"), "pt": p.get("pt"),
        }) + 10

    hand = p.get("hand")
    # ★ 2026-09-26：`players` 非空 ⇒ 本局拉到并解析了牌谱（降级摘要路径为 None）。
    #   用户口径：未和牌也要明确写出来——有图时文字被精简成一行，**这句话只能由图承担**。
    has_paipu = bool(p.get("players"))
    if hand and (hand.get("hand") or hand.get("win_tile")):
        how = "自摸" if hand.get("zimo") else "荣和"
        _txt(d, (24, y + 8), "最大一手", 13, th, color=th["sub"], anchor="lm")
        _txt(d, (110, y + 8), f"{how} {hand.get('win_tile','')}", 15, th,
             color=th["gold"], bold=True, anchor="lm")
        _txt(d, (W - 24, y + 8), f"+{int(hand.get('point') or 0)}", 21, th,
             color=th["gold"], bold=True, anchor="rm")
        y += 26
        y += _draw_hand(img, d, 30, y, hand, W - 66, th) + 12
        mid = " ".join(x for x in (f"{hand.get('count') or 0} 番", f"{hand.get('fu') or 0} 符",
                                   hand.get("rank_name") or "") if x)
        fans = hand.get("fans_txt", "")
        line = f"{mid}｜{fans}" if mid else fans
        if line:
            _txt(d, (24, y), _ellipsize(d, line, _font(13), W - 48), 13, th, anchor="lm")
            y += 24
    elif has_paipu:
        _txt(d, (24, y + 8), "最大一手", 13, th, color=th["sub"], anchor="lm")
        _txt(d, (110, y + 8), "本场未和牌", 15, th, color=th["sub"], anchor="lm")
        y += 30
    elif p.get("brief_note"):
        _txt(d, (24, y + 6), p["brief_note"], 13, th, color=th["sub"], anchor="lm")
        y += 26

    # ★ 2026-09-26 新增「最大放铳」：**与「最大一手」同详细度**（用户追加口径：放铳牌 +
    #   对方的和牌牌型全手牌/副露 + 番/符/役种）。`loss` 为 `delta_scores` 实际负值
    #   （含本场棒/供托，见铁律 19）；未放铳 → 写「本场未放铳」；降级摘要路径不出这一块。
    if has_paipu:
        deal = p.get("deal")
        _txt(d, (24, y + 8), "最大放铳", 13, th, color=th["sub"], anchor="lm")
        if deal:
            # ★ 2026-09-26 用户口径：**显示对手名**（「被 XXX 荣和 3p」）；昵称超长则截断
            who = str(deal.get("winner_name") or "")
            dhow = (f"被 {who} 荣和 {deal.get('tile') or '?'}" if who
                    else f"放铳 {deal.get('tile') or '?'}")
            _txt(d, (110, y + 8), _ellipsize(d, dhow, _font(15), W - 214), 15, th,
                 color=th["red"], bold=True, anchor="lm")
            _txt(d, (W - 24, y + 8), f"{int(deal.get('loss') or 0)}", 21, th,
                 color=th["red"], bold=True, anchor="rm")
            y += 26
            if deal.get("hand") or deal.get("win_tile"):
                y += _draw_hand(img, d, 30, y, deal, W - 66, th) + 12
            dmid = " ".join(x for x in (f"{deal.get('count') or 0} 番",
                                        f"{deal.get('fu') or 0} 符",
                                        deal.get("rank_name") or "") if x)
            dfans = deal.get("fans_txt", "")
            dline = f"{dmid}｜{dfans}" if dmid else dfans
            if dline:
                _txt(d, (24, y), _ellipsize(d, dline, _font(13), W - 48), 13, th, anchor="lm")
                y += 24
        else:
            _txt(d, (110, y + 8), "本场未放铳", 15, th, color=th["sub"], anchor="lm")
            y += 30

    # ★ 2026-09-24 新增：全玩家统计表（对局玩家 / 段位 / 点数变化 / 立直·荣和·自摸·放铳）
    if p.get("players"):
        y += _players_table(img, d, 24, y, W - 48, th, p["players"])

    for c in comments:
        color = th["blue"]
        if any(k in c for k in ("升段", "距升段", "一位")):
            color = th["gold"]
        if any(k in c for k in ("降段", "掉段", "垫底", "击飞")):
            color = th["red"]
        _panel(d, 24, y, W - 48, 26, th, r=6)
        d.rounded_rectangle([24, y, 29, y + 26], radius=2, fill=color)
        _txt(d, (40, y + 13), _ellipsize(d, c, _font(14), W - 74), 14, th, anchor="lm")
        y += 32
    return _png(_finish(img, y + 6))


# ---------------------------------------------------------------- 卡：牌谱明细（Feature-07，2026-09-28）

def _duration(sec: int) -> str:
    """秒 → 「20 分 44 秒」/「1 小时 3 分」。"""
    sec = max(0, int(sec or 0))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return f"{h} 小时 {m} 分"
    return f"{m} 分 {s} 秒" if m else f"{s} 秒"


def _hand_rows(hand: dict | None, *, loss: bool = False) -> list[str]:
    """一手牌 → 文字行（`[牌型行, 役种行, 点数]`）；空手牌返回 `[]`。

    ★ 与「最大一手」同详细度（用户口径）：牌型 + 番/符/档次 + 役种。
    ★ 2026-10-06 用户口径（原话：「荣和的得点跑到"自摸8p"后面去了。原来的位置在最右侧，
      且字体更大」）——**点数单独作为第 3 项返回**，由调用方画在**整行最右侧、字号更大**处
      （与对局结果卡 `render_result` 同款）；**不再拼进牌型行**（旧写法
      `"自摸 8p +16000"` 会让得点看起来像牌名的一部分）。
    """
    if not hand:
        return []
    if loss:
        who = str(hand.get("winner_name") or "")
        tile = hand.get("tile") or "?"
        title = f"被 {who} 荣和 {tile}" if who else f"放铳 {tile}"
        point = f"{int(hand.get('loss') or 0)}"
    else:
        how = "自摸" if hand.get("zimo") else "荣和"
        title = f"{how} {hand.get('win_tile') or '?'}"
        point = f"+{int(hand.get('point') or 0)}"
    mid = " ".join(x for x in (f"{hand.get('count') or 0} 番", f"{hand.get('fu') or 0} 符",
                               hand.get("rank_name") or "") if x)
    fans = hand.get("fans_txt") or ""
    second = f"{mid}｜{fans}" if mid and fans else (mid or fans)
    return [title, second, point]


# 牌谱明细卡内容高度上限：单个「最大一手 / 最大放铳」块的绘制上限。
# `_draw_hand` 最高 = int(46 / 0.62) = 74px（牌图），另有标题行 26 / 描述行 24 / 与牌图间距 12。
_PAIPU_HAND_BLOCK_H = 74 + 12 + 26 + 24                 # = 136
_PAIPU_HAND_BLOCK_PAD = 10                              # 安全余量（宁大勿小）


def _paipu_est_height(sharer: dict | None, players: list, others: list) -> int:
    """牌谱明细卡内容高度**上限**估算（★ 必须 ≥ 实际绘制高度，宁大勿小）。

    ★ 2026-09-28 bug 根因：旧估算把「最大一手 / 最大放铳」两块按 0 高度计入
    （`sharer` 恒 330）、`others` 每人只算 56px，而实际两块各自还要加**手牌牌图**
    （`_draw_hand` 最高 74px）与标题/描述行 → 实测 `139cf41a` 那局真实内容高 974px、
    估算仅 900px → `_finish` 的 crop 越界 → Pillow 用**纯黑**补齐 74 行（用户报的黑条）。
    """
    h = 140                                            # 顶栏 46 + 元信息行 42 + 余量
    if sharer:
        h += 26 + 72                                   # 主角名一行 + 三个统计框
        if sharer.get("level_id"):
            h += 102                                   # `_level_panel` 92 + 间距 10
        for key in ("best_hand", "max_deal_in"):
            h += (_PAIPU_HAND_BLOCK_H + _PAIPU_HAND_BLOCK_PAD) if sharer.get(key) else 30
    if players:
        h += 26 + 44 * len(players) + 8 + 14 + 10      # `_players_table`(h+14) + 间距
    if others:
        h += 26 + 56 * len(others)                     # 「其他玩家」标题 + 每人 50+6
    return h


def render_paipu_detail(p: dict) -> bytes:
    """牌谱明细卡（Feature-07）。

    用户需求（原话）：「玩家给出一个牌谱链接，回复这局类似于对局明细的信息。」
    口径（用户拍板）：**以分享者为主角 + 全体明细**、图片卡 + 文本明细、命令式触发。

    payload = `monitor.build_paipu_detail()` 的结构 + `theme`：
    `place / start_time / end_time / rounds / player_count / sharer{...} /
    players[]（全玩家统计行）/ hands[]（每人最大一手·最大放铳）`。
    `sharer=None`（链接尾号反推不出、查询者也不在本局）时**不设主角**，只出全体明细。

    ★ 先测量后绘制（2026-09-28 修「图片下方黑条」）：
      ① 画布按**内容高度上限**申请（`_paipu_est_height`，牌图/手牌块全算进去）；
      ② 画完按**实测 y** 裁切（`_finish`）—— 内容完整、底部不留大片空白；
      ③ 万一估算上限仍失效（实测超出画布），按实测高度**重绘一次**——内容绝不被裁。
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow 不可用")
    th = _theme(p.get("theme"))
    sharer = p.get("sharer") or None
    players = p.get("players") or []
    # 其他玩家按顺位排序（`hands` 按座位序给出，直接遍历会得到 #2 #4 #1 这种乱序）
    others = sorted((h for h in (p.get("hands") or []) if not h.get("is_sharer")),
                    key=lambda h: int(h.get("rank") or 99))

    est = min(6000, max(400, _paipu_est_height(sharer, players, others)))
    img = Image.new("RGB", (W, est), th["bg"])
    y = _paint_paipu_detail(img, ImageDraw.Draw(img), p, sharer, players, others, th)
    if y + 8 > img.height:                             # 估算失效 → 按实测高度重绘，绝不裁内容
        img = Image.new("RGB", (W, y + 8), th["bg"])
        y = _paint_paipu_detail(img, ImageDraw.Draw(img), p, sharer, players, others, th)
    return _png(_finish(img, y + 8, th["bg"]))


def _paint_paipu_detail(img, d, p: dict, sharer, players: list, others: list, th) -> int:
    """把牌谱明细卡画到 `img` 上，返回内容底部 y。

    ★ 铁律 14：本函数是牌谱明细卡的**唯一绘制实现**（`render_paipu_detail` 调用它；
    估算失效时同一条绘制路径重跑一次，**不是**第二套渲染）。
    """
    _header(d, "雀魂牌谱明细", th, p.get("place", ""))
    y = 58

    # ---- 元信息行：时间区间 · 时长 · 局数 · 人数 ----
    st, et = int(p.get("start_time") or 0), int(p.get("end_time") or 0)
    period = ""
    if st:
        period = time.strftime("%m-%d %H:%M", time.localtime(st))
        if et:
            period += " → " + time.strftime("%H:%M", time.localtime(et))
    meta = " · ".join(x for x in (
        period,
        _duration(et - st) if (st and et) else "",
        f"{int(p.get('rounds') or 0)} 局" if p.get("rounds") else "",
        f"{int(p.get('player_count') or 0)} 人" if p.get("player_count") else "",
    ) if x)
    if meta:
        _panel(d, 24, y, W - 48, 34, th, fill=th["panel2"])
        _txt(d, (38, y + 17), meta, 14, th, color=th["gold"], anchor="lm")
        y += 42

    # ---- 主角（分享者 / 本人）----
    if sharer:
        label = "分享者" if p.get("sharer_source") == "link" else "本人"
        _txt(d, (24, y + 10), label, 13, th, color=th["sub"], anchor="lm")
        _txt(d, (86, y + 10), _ellipsize(d, str(sharer.get("name") or "?"), _font(21), 300),
             21, th, bold=True, anchor="lm")
        lv = str(sharer.get("level_name") or "")
        if lv:
            _txt(d, (W - 24, y + 10), lv, 14, th, color=th["gold"], anchor="rm")
        y += 26

        bw = (W - 48 - 2 * 12) // 3
        rank = int(sharer.get("rank") or 0)
        _stat_box(d, 24, y, bw, 60, "名次", f"#{rank}", th, color=_rank_color(rank, th))
        delta = int(sharer.get("delta") or 0)
        _stat_box(d, 24 + bw + 12, y, bw, 60, "得点",
                  f"{int(sharer.get('point') or 0)}", th,
                  color=th["green"] if delta >= 0 else th["red"])
        pt = int(sharer.get("pt") or 0)
        _stat_box(d, 24 + 2 * (bw + 12), y, bw, 60, "段位分变动", f"{pt:+d}", th,
                  color=th["green"] if pt >= 0 else th["red"])
        y += 72

        if sharer.get("level_id"):
            y += _level_panel(img, d, 24, y, W - 48, th, {
                "level_id": sharer.get("level_id"), "level_name": sharer.get("level_name"),
                "init": sharer.get("level_init"), "cur": sharer.get("level_after"),
                "prev": sharer.get("level_before"), "nxt": sharer.get("level_next"),
                "pt": pt,
            }) + 10

        # 最大一手（无 = 本场未和牌）
        _txt(d, (24, y + 8), "最大一手", 13, th, color=th["sub"], anchor="lm")
        hb = _hand_rows(sharer.get("best_hand"))
        if hb:
            # ★ 2026-10-06 用户口径：牌型在左，**点数独占整行最右侧、字号 21（大字）**
            #   —— 与对局结果卡 `render_result` 同款；不再跟牌名挤在一行小字里
            #   ⚠️ 右侧预留 116px：21 号字最宽 6 字符（`+48000`）约 76px + 间距，
            #   否则 6 位数点数会与左侧牌型文字重叠（几何自检脚本抓到的边界）
            _txt(d, (110, y + 8), _ellipsize(d, hb[0], _font(15), W - 110 - 116), 15, th,
                 color=th["gold"], bold=True, anchor="lm")
            _txt(d, (W - 24, y + 8), hb[2], 21, th, color=th["gold"], bold=True, anchor="rm")
            y += 26
            y += _draw_hand(img, d, 30, y, sharer["best_hand"], W - 66, th) + 12
            if len(hb) > 1 and hb[1]:
                _txt(d, (24, y), _ellipsize(d, hb[1], _font(13), W - 48), 13, th, anchor="lm")
                y += 24
        else:
            _txt(d, (110, y + 8), "本场未和牌", 15, th, color=th["sub"], anchor="lm")
            y += 30

        # 最大放铳（无 = 本场未放铳）
        _txt(d, (24, y + 8), "最大放铳", 13, th, color=th["sub"], anchor="lm")
        db = _hand_rows(sharer.get("max_deal_in"), loss=True)
        if db:
            # ★ 2026-10-06：与「最大一手」同款——描述在左、**放铳点数在最右侧大字（21）**
            _txt(d, (110, y + 8), _ellipsize(d, db[0], _font(15), W - 110 - 116), 15, th,
                 color=th["red"], bold=True, anchor="lm")
            _txt(d, (W - 24, y + 8), db[2], 21, th, color=th["red"], bold=True, anchor="rm")
            y += 26
            deal = sharer["max_deal_in"]
            if deal.get("hand") or deal.get("win_tile"):
                y += _draw_hand(img, d, 30, y, deal, W - 66, th) + 12
            if len(db) > 1 and db[1]:
                _txt(d, (24, y), _ellipsize(d, db[1], _font(13), W - 48), 13, th, anchor="lm")
                y += 24
        else:
            _txt(d, (110, y + 8), "本场未放铳", 15, th, color=th["sub"], anchor="lm")
            y += 30

    # ---- 全玩家统计表 ----
    if players:
        y += _players_table(img, d, 24, y, W - 48, th, players)

    # ---- 其他玩家的最大一手 / 最大放铳（紧凑两行：牌型 + 役种）----
    if others:
        _txt(d, (24, y + 6), "其他玩家", 13, th, color=th["sub"], anchor="lm")
        y += 26
        for h in others:
            _panel(d, 24, y, W - 48, 50, th, fill=th["panel2"], r=8)
            rank = int(h.get("rank") or 0)
            _txt(d, (38, y + 16), f"#{rank}", 14, th, color=_rank_color(rank, th),
                 bold=True, anchor="lm")
            _txt(d, (72, y + 16), _ellipsize(d, str(h.get("name") or ""), _font(15), 130),
                 15, th, bold=True, anchor="lm")
            best = _hand_rows(h.get("best"))
            deal = _hand_rows(h.get("deal"), loss=True)
            if best:
                first, second, col, ptxt = "最大一手 " + best[0], best[1], th["gold"], best[2]
            elif deal:
                first, second, col, ptxt = "最大放铳 " + deal[0], deal[1], th["red"], deal[2]
            else:
                first, second, col, ptxt = "本场未和牌 · 未放铳", "", th["sub"], ""
            _txt(d, (210, y + 16), _ellipsize(d, first, _font(14), W - 210 - 116), 14, th,
                 color=col, anchor="lm")
            if ptxt:
                # ★ 2026-10-06：点数同样放**行尾右对齐**（不再跟牌名挤在一起）
                _txt(d, (W - 38, y + 16), ptxt, 14, th, color=col, bold=True, anchor="rm")
            if second:
                _txt(d, (72, y + 36), _ellipsize(d, second, _font(12), W - 96), 12, th,
                     color=th["sub"], anchor="lm")
            y += 56

    return y


# ---------------------------------------------------------------- 卡：同室战报

def render_room_battle(p: dict) -> bytes:
    """同室对战卡（两种模式）。

    - **赛后战报**：`players[]` 带 `rank/point/delta/pt` → 每人一行数据；
    - **开局播报**：`players[]` 只有 `qq/name`（无数据）→ 只列成员，底部显示 `note`
      （如「（约 5 分钟后可在游戏内观战）」）。

    payload：`room_id / title / players[{rank,qq,name,point,delta,pt}] / note / theme`。
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow 不可用")
    th = _theme(p.get("theme"))
    players = p.get("players") or []
    has_data = any(int(pl.get("rank") or 0) or int(pl.get("point") or 0) for pl in players)
    note = str(p.get("note") or "")
    row_h = 48 if has_data else 44

    img = Image.new("RGB", (W, H_MAX), th["bg"])
    d = ImageDraw.Draw(img)
    _header(d, "雀魂 · 同室对战", th, f"房间 {p.get('room_id')}")
    _txt(d, (24, 72), p.get("title", ""), 19, th, bold=True, anchor="lm")
    y = 100
    for i, pl in enumerate(players):
        _panel(d, 24, y, W - 48, row_h, th,
               fill=th["panel2"] if i % 2 == 0 else th["panel"], r=8)
        rank = int(pl.get("rank") or 0)
        if rank == 1:
            d.rounded_rectangle([24, y, 29, y + row_h], radius=2, fill=th["gold"])
        _paste_avatar(img, 40, y + (row_h - 36) // 2, 36, pl.get("qq", ""), th)
        cy = y + row_h // 2
        if has_data:
            _txt(d, (86, cy), f"#{rank}", 17, th, color=_rank_color(rank, th),
                 bold=True, anchor="lm")
            _txt(d, (118, cy), _ellipsize(d, pl.get("name", ""), _font(16), 150), 16, th,
                 anchor="lm")
            _txt(d, (W - 200, cy), f"{int(pl.get('point') or 0)}", 16, th, anchor="rm")
            dl = int(pl.get("delta") or 0)
            _txt(d, (W - 116, cy), f"{dl:+d}", 15, th,
                 color=th["green"] if dl >= 0 else th["red"], anchor="rm")
            _txt(d, (W - 40, cy), f"PT {int(pl.get('pt') or 0):+d}", 15, th,
                 color=th["gold"], anchor="rm")
        else:
            _txt(d, (86, cy), pl.get("name", ""), 18, th, bold=True, anchor="lm")
        y += row_h + 6
    if note:
        _txt(d, (24, y + 6), note, 14, th, color=th["green"], anchor="lm")
        y += 26
    return _png(_finish(img, y + 6))


# ---------------------------------------------------------------- 卡：对局开始

def render_playing_start(p: dict) -> bytes:
    """对局开始卡。payload：`qq / nickname / place / start_time / approx / spectate_delay`。"""
    if not HAS_PIL:
        raise RuntimeError("Pillow 不可用")
    th = _theme(p.get("theme"))
    img = Image.new("RGB", (W, H_MAX), th["bg"])
    d = ImageDraw.Draw(img)
    _header(d, "雀魂 · 对局开始", th)
    _paste_avatar(img, 24, 66, 60, p.get("qq", ""), th)
    _txt(d, (100, 84), p.get("nickname", ""), 24, th, bold=True, anchor="lm")
    _txt(d, (100, 112), p.get("place", ""), 17, th, color=th["gold"], anchor="lm")

    _panel(d, 24, 142, W - 48, 42, th, fill=th["panel2"])
    start = int(p.get("start_time") or 0)
    delay = int(p.get("spectate_delay") or 300)
    if start:
        head = "约 " if p.get("approx") else ""
        _txt(d, (40, 163), f"开局 {head}{_clock(start)}", 15, th, anchor="lm")
        _txt(d, (W - 40, 163), f"约 {_clock(start + delay)} 后可观战", 15, th,
             color=th["green"], anchor="rm")
    else:
        _txt(d, (40, 163), f"（约 {delay // 60} 分钟后可观战）", 15, th,
             color=th["green"], anchor="lm")
    return _png(_finish(img, 196))


def _clock(ts: int) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(int(ts)))
    except Exception:  # noqa: BLE001
        return "-"


# ---------------------------------------------------------------- 卡：战绩（P2）

def _level_curve(d, x, y, w, th, scores: list, label: str = "") -> int:
    """段位分走势曲线（P2）。`scores` = 逐局**局后**段位分（旧→新）；返回占用高度。

    y 轴按数据自适应（并对过小波动做下限放大，避免把 5 分的抖动画成断崖）。
    """
    h = 104
    _panel(d, x, y, w, h, th)
    _txt(d, (x + 16, y + 18), label or f"段位分走势（最近 {len(scores)} 局）", 13, th,
         color=th["sub"], anchor="lm")
    left, right = x + 62, x + w - 16
    top, bottom = y + 34, y + h - 16
    lo, hi = min(scores), max(scores)
    if hi - lo < 20:                       # 波动过小时放大 y 轴（保持居中）
        mid = (hi + lo) / 2
        lo, hi = mid - 10, mid + 10
    span = max(1, hi - lo)
    step = (right - left) / max(1, len(scores) - 1)
    pts = [(left + step * i, bottom - (v - lo) / span * (bottom - top))
           for i, v in enumerate(scores)]
    d.line([left, top, right, top], fill=th["grid"], width=1)
    d.line([left, bottom, right, bottom], fill=th["grid"], width=1)
    _txt(d, (left - 8, top), str(hi), 11, th, color=th["sub"], anchor="rm")
    _txt(d, (left - 8, bottom), str(lo), 11, th, color=th["sub"], anchor="rm")
    if len(pts) > 1:
        d.line(pts, fill=th["gold"], width=2)
    for i, (px, py) in enumerate(pts):
        last = (i == len(pts) - 1)
        r = 4 if last else 2
        d.ellipse([px - r, py - r, px + r, py + r],
                  fill=th["gold"] if last else th["gold_dim"])
    # 首末数值标注（一眼看出涨跌）
    _txt(d, (left, bottom + 4), str(scores[0]), 11, th, color=th["sub"], anchor="lm")
    _txt(d, (right, bottom + 4), str(scores[-1]), 11, th, color=th["gold"], anchor="rm")
    return h


def render_record(p: dict) -> bytes:
    """`/雀魂战绩` 战绩卡（P2）。

    payload：`qq / nickname / scope / level_name / level_init / level_score / level_next /
    seq([顺位...]) / level_scores([局后段位分...]) / four / summary{...}`。
    `level_scores` 与 `summary` 可缺（老数据/首次上线）→ 对应区块自动省略。
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow 不可用")
    th = _theme(p.get("theme"))
    seq = [int(x) for x in (p.get("seq") or [])][-50:]
    scores = [int(x) for x in (p.get("level_scores") or [])][-50:]
    summary = p.get("summary") or {}
    four = bool(p.get("four"))
    n_rank = 4 if four else 3
    img = Image.new("RGB", (W, H_MAX), th["bg"])
    d = ImageDraw.Draw(img)
    _header(d, "雀魂战绩", th, p.get("scope", ""))

    y = 58
    _paste_avatar(img, 24, y, 48, p.get("qq", ""), th)
    _txt(d, (86, y + 6), p.get("nickname", ""), 21, th, bold=True, anchor="lm")
    _txt(d, (86, y + 32), p.get("level_name", ""), 15, th, color=th["gold"], anchor="lm")
    _badge(img, d, W - 24 - 48, y, 48, th, p.get("level_id"))       # ★ P3 段位徽章
    cur, nxt = int(p.get("level_score") or 0), int(p.get("level_next") or 0)
    init = int(p.get("level_init") or 0)
    _txt(d, (W - 80, y + 6), f"{cur} / {nxt}" if nxt else f"{cur}", 15, th,
         color=th["sub"], anchor="rm")
    _txt(d, (W - 80, y + 32), f"距升段 {max(0, nxt - cur)} 分" if nxt else "", 14, th,
         color=th["gold"], anchor="rm")
    y += 74                                   # ★ 给徽章下方星级留空间
    if nxt:
        # ★ 基准 0 → 升级分（同结果卡口径，用户 2026-09-22 纠正）
        _progress(d, 24, y, W - 48, 10, cur / max(1, nxt), th)
        _txt(d, (24, y + 26), "0", 12, th, color=th["sub"], anchor="lm")
        _txt(d, (W // 2, y + 26), f"当前 {cur}", 13, th, color=th["gold"], bold=True,
             anchor="mm")
        _txt(d, (W - 24, y + 26), f"升级 {nxt}", 12, th, color=th["sub"], anchor="rm")
        y += 46
    else:
        _txt(d, (24, y + 16), f"当前 {cur}", 13, th, color=th["gold"], bold=True, anchor="lm")
        y += 30

    if len(scores) >= 2:                                   # ★ P2：段位分曲线
        y += _level_curve(d, 24, y, W - 48, th, scores) + 12

    if seq:
        _txt(d, (24, y), f"最近 {len(seq)} 局顺位走势", 14, th, color=th["sub"], anchor="lm")
        y += 26
        plot_h = 84
        _panel(d, 24, y, W - 48, plot_h + 28, th)
        left, right, top = 58, W - 46, y + 14
        for r in range(1, n_rank + 1):                     # 背景横线：四麻 4 条 / 三麻 3 条
            yy = top + (r - 1) * (plot_h / max(1, n_rank - 1))
            d.line([left, yy, right, yy], fill=th["grid"], width=1)
            _txt(d, (left - 12, yy), str(r), 12, th, color=th["sub"], anchor="rm")
        n = len(seq)
        step = (right - left) / max(1, n - 1)
        pts = []
        for i, r in enumerate(seq):
            r = min(max(r, 1), n_rank)
            pts.append((left + step * i, top + (r - 1) * (plot_h / max(1, n_rank - 1)), r))
        if len(pts) > 1:
            d.line([(x, yy) for x, yy, _ in pts], fill=th["grid"], width=2)
        pr = 4 if n <= 22 else (3 if n <= 36 else 2)
        for px, py, r in pts:
            d.ellipse([px - pr, py - pr, px + pr, py + pr], fill=_rank_color(r, th))
        y += plot_h + 40

        cnt = {k: seq.count(k) for k in (1, 2, 3, 4)}
        labels = [(1, "一位"), (2, "二位"), (3, "三位"), (4, "四位")] if four \
            else [(1, "一位"), (2, "二位"), (3, "三位")]
        gap = 12
        bw2 = (W - 48 - (len(labels) - 1) * gap) // len(labels)
        x = 24
        for k, label in labels:
            n_k = cnt.get(k, 0)
            pct = n_k / max(1, len(seq))
            _panel(d, x, y, bw2, 64, th, fill=th["panel2"])
            _txt(d, (x + 12, y + 20), label, 14, th, color=th["sub"], anchor="lm")
            _txt(d, (x + bw2 - 12, y + 20), f"{n_k} 次", 15, th, color=_rank_color(k, th),
                 bold=True, anchor="rm")
            _progress(d, x + 12, y + 38, bw2 - 24, 8, pct, th, color=_rank_color(k, th))
            _txt(d, (x + 12, y + 58), f"{pct*100:.0f}%", 12, th, color=th["sub"], anchor="lm")
            x += bw2 + gap
        y += 76

    if summary:                                            # ★ P2：统计行
        bits = [f"共 {int(summary.get('games') or 0)} 局"]
        if summary.get("avg_rank"):
            bits.append(f"平均顺位 {float(summary['avg_rank']):.2f}")
        if summary.get("win_rate") is not None:
            bits.append(f"一位率 {float(summary.get('win_rate') or 0)*100:.0f}%")
        if summary.get("top2_rate") is not None:
            bits.append(f"连对率 {float(summary.get('top2_rate') or 0)*100:.0f}%")
        if summary.get("best_streak"):
            bits.append(f"最长连一位 {int(summary['best_streak'])}")
        _panel(d, 24, y, W - 48, 34, th, fill=th["panel"])
        _txt(d, (38, y + 17), _ellipsize(d, " · ".join(bits), _font(13), W - 76), 13, th,
             anchor="lm")
        y += 42
    return _png(_finish(img, y + 6))


# ================================================================ AI 复盘卡（Feature-08，2026-09-29）
#
# 数据源：服务器自建 mjai-reviewer + akochan 的 `--json` 报告 → `ai_review.parse_review()`
# → 本函数渲染成与其它卡同风格的图片。
# ★ 视觉口径：**AI 建议打的那张牌用金框高亮**——复用 `_draw_hand` 的 `win_tile` 金框槽位
#   （语义是「建议张」而非和牌张，仅视觉复用，不新写绘制逻辑）。

_MJAI_Z = {"E": "1z", "S": "2z", "W": "3z", "N": "4z", "P": "5z", "F": "6z", "C": "7z"}
_AI_HAND_H = 76          # 手牌牌图行高（`_draw_hand` 实测 ≈74）
_AI_ROW_H = 26 + _AI_HAND_H + 16


def _mjai_tile(t: Any) -> str:
    """mjai 牌名 → 牌图 code（`5pr`→`0p` 赤 5；`E/S/W/N/P/F/C`→`1z..7z`；`?`→ 空串）。"""
    s = str(t or "").strip()
    if not s or s == "?":
        return ""
    if s in _MJAI_Z:
        return _MJAI_Z[s]
    if len(s) == 3 and s[0] == "5" and s[2] == "r":
        return "0" + s[1]
    if not (1 <= len(s) <= 2) or "{" in s or "'" in s:
        # ★ 脏数据（例如误把动作 dict 的 repr 当牌名传进来）→ 宁可不画，
        #   也不要让 `_paste_tile` 在牌面上糊一行文字（用户看到的是「乱码绿块」）。
        return ""
    return s


def _ai_mv_text(mv: Any) -> str:
    """动作 → 「打 S」/「立直」（复用 `ai_review.move_label` 保证同口径；导入失败时兜底）。"""
    try:
        from . import ai_review as _ar
        return _ar.move_label(mv)
    except Exception:  # noqa: BLE001
        return str((mv or {}).get("pai") if isinstance(mv, dict) else "") or "—"


def _paint_ai(img, d, p: dict, th) -> int:
    """绘制 AI 复盘卡主体，返回内容底部 y（唯一绘制路径，供「先测量后绘制」复用）。"""
    s = p.get("summary") or {}
    rows = (s.get("mistakes") or [])[: int(p.get("max_rows") or 6)]
    _header(d, "雀魂 AI 复盘", th, str(p.get("meta") or ""))
    y = 58

    bits = [x for x in (
        f"分享者「{p['name']}」" if p.get("name") else "",
        f"评估 {int(s.get('reviewed') or 0)} 手",
        f"引擎 {s.get('engine') or 'AI'}",
        str(s.get("review_time") or ""),
    ) if x]
    d.text((20, y), " · ".join(bits), font=_font(16), fill=th["sub"])
    y += 30

    # ---- 表现分块：大号「表现分」+ 等级，一致率与三档计数作副信息 ----
    ph = 86
    _panel(d, 14, y, W - 28, ph, th)
    score = float(s.get("score") or 0.0)
    grade = str(s.get("grade") or "?")
    grade_text = str(s.get("grade_text") or "")
    d.text((30, y + 10), "表现分（自建加权）", font=_font(15), fill=th["sub"])
    d.text((30, y + 26), f"{score:.1f}", font=_font(34, True), fill=th["gold"])
    d.text((122, y + 44), f"{grade} {grade_text}", font=_font(15), fill=th["gold"])
    d.text((30, y + 66), f"一致率 {float(s.get('rating') or 0) * 100:.1f}%",
           font=_font(13), fill=th["sub"])
    cx = 236
    for label, val, color in (("同意", int(s.get("agree") or 0), th["green"]),
                              ("可容忍", int(s.get("tolerable") or 0), th["orange"]),
                              ("分歧", int(s.get("disagree") or 0), th["red"])):
        d.text((cx, y + 16), label, font=_font(14), fill=th["sub"])
        d.text((cx, y + 32), str(val), font=_font(24, True), fill=color)
        cx += 92
    _progress(d, 236, y + 66, W - 236 - 40, 10, float(s.get("rating") or 0), th,
              color=th["gold"])
    y += ph + 16

    # ---- 关键失误（可量化者才画手牌；无则显式写「一致」） ----
    dev = int(s.get("deviations") or 0)
    unq = int(s.get("unquantified") or 0)
    if not rows:
        msg = ("本场与 AI 建议一致（无明显失误）" if not dev else
               f"本场 {dev} 手与 AI 建议不同，但引擎未给出可比较的期望值")
        _panel(d, 14, y, W - 28, 46, th)
        _txt(d, (W // 2, y + 23), msg, 16, th, color=th["sub"], anchor="mm")
        y += 58
    else:
        tail = f"（共 {dev} 手偏差" + (f"，另 {unq} 手无期望值可比" if unq else "") + "）"
        d.text((20, y), f"关键失误 · 可量化 {len(rows)} 手{tail}", font=_font(17, True),
               fill=th["text"])
        y += 28
        for i, m in enumerate(rows, 1):
            mark = "（可容忍）" if m.get("acceptance") == "tolerable" else ""
            line = (f"{DIGITS[i - 1] if i <= len(DIGITS) else str(i)} {m.get('label')}"
                    f" {int(m.get('junme') or 0)} 巡：实际 {_ai_mv_text(m.get('actual'))}"
                    f" → AI 建议 {_ai_mv_text(m.get('best'))}"
                    f"（期望差 {float(m.get('loss') or 0):.2f}）{mark}")
            d.text((20, y), _ellipsize(d, line, _font(16), W - 40), font=_font(16),
                   fill=th["text"])
            y += 26
            fuuros = [_mjai_tile(t) for t in (m.get("fuuros") or [])]
            fuuros = [t for t in fuuros if t]
            tiles = [_mjai_tile(t) for t in (m.get("tehai") or [])]
            tiles = [t for t in tiles if t]
            if tiles:
                # ★ 两张牌**都圈出来**（用户 2026-09-29 口径）：实际打出＝红框，AI 建议＝金框；
                #   两者相同（AI 也这么打）时只画金框，避免同一张牌套两个色框。
                best_pai = _mjai_tile((m.get("best") or {}).get("pai"))
                act_pai = _mjai_tile((m.get("actual") or {}).get("pai"))
                seq = fuuros + sorted(tiles, key=tile_key)
                marks: dict[int, tuple] = {}
                used: set[int] = set()
                pairs = ([(best_pai, th["gold"])] if (act_pai and act_pai == best_pai)
                         else [(act_pai, th["red"]), (best_pai, th["gold"])])
                for pai, color in pairs:
                    if not pai:
                        continue
                    i = next((k for k, t in enumerate(seq) if t == pai and k not in used), None)
                    if i is not None:
                        used.add(i)
                        marks[i] = color
                hand = {"mings": [{"tiles": fuuros}] if fuuros else [],
                        "hand": sorted(tiles, key=tile_key), "win_tile": ""}
                _draw_hand(img, d, 22, y, hand, W - 60, th, marks=marks)
                y += _AI_HAND_H
            y += 16

    # ---- 危险牌（实际打出的牌放铳概率高）★ AI 也建议打的不算失误，用灰字区分 ----
    dgs = (s.get("dangers") or [])[: int(p.get("max_dangers") or 3)]
    dth = float(s.get("danger_threshold") or 0.05)
    if s.get("has_probs") and dgs:
        cnt = int(s.get("danger_count") or len(dgs))
        d.text((20, y), f"危险牌 · 打出即放铳概率 ≥{dth * 100:.0f}%（共 {cnt} 手，列前 {len(dgs)}）",
               font=_font(17, True), fill=th["text"])
        y += 28
        for i, dg in enumerate(dgs, 1):
            bp = dg.get("best_prob")
            bp = None if bp is None else float(bp)
            if dg.get("ai_agrees"):
                tail, color = "AI 也建议打（无奈之举）", th["sub"]
            elif bp is None:
                tail, color = f"AI 建议 {_ai_mv_text(dg.get('best'))}", th["red"]
            elif bp < float(dg.get("prob") or 0):
                tail, color = (f"更安全的选择 {_ai_mv_text(dg.get('best'))}"
                               f"（{bp * 100:.1f}%）"), th["red"]
            else:
                # AI 也可能为了期望值打更危险的牌 → 必须写清，别让人以为 AI 建议的更安全
                tail, color = (f"AI 建议 {_ai_mv_text(dg.get('best'))}"
                               f"（{bp * 100:.1f}%，期望更高）"), th["red"]
            line = (f"{DIGITS[i - 1] if i <= len(DIGITS) else str(i)} {dg.get('label')}"
                    f" {int(dg.get('junme') or 0)} 巡  {_ai_mv_text(dg.get('actual'))}"
                    f" {float(dg.get('prob') or 0) * 100:.1f}%   {tail}")
            d.text((20, y), _ellipsize(d, line, _font(16), W - 40), font=_font(16), fill=color)
            y += 28
    elif s.get("has_probs"):
        # 「本场确实没有危险牌」也要写出来（缺省什么都不画 → 读图人会以为是没数据）
        d.text((20, y), f"危险牌：无（未打出放铳概率 ≥{dth * 100:.0f}% 的牌，相对安全）",
               font=_font(15), fill=th["sub"])
        y += 24
    else:
        # 缺失态显式：引擎没给放铳概率时不能「什么都不画」（读图的人会以为本场无危险牌）
        d.text((20, y), "危险牌：本报告未提供放铳概率数据（引擎未输出）",
               font=_font(15), fill=th["sub"])
        y += 24

    y += 6
    d.text((20, y), f"分析耗时 {s.get('review_time') or '—'}｜引擎 {s.get('engine') or 'AI'}"
                    "（akochan・仅供复盘参考）", font=_font(14), fill=th["sub"])
    return y + 24


def render_ai_review(p: dict) -> bytes:
    """**AI 复盘卡**（Feature-08）。`payload = {"theme","meta","name","summary","max_rows"}`。

    `summary` 来自 `ai_review.parse_review()`；数据不可用时抛异常（调用方回退纯文本，铁律 14）。
    高度用「先测量后绘制 + 按实测重绘」两遍法，**内容绝不被裁、底部不留大片空白**。

    `max_rows` / `max_dangers`：概览＝**6 行失误 + 3 条危险牌**（用户 2026-09-29 拍板）；
    **详细模式**传很大的值即可「全列」，此时图可以很长（上限 `H_AI_MAX`）。
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow 不可用")
    th = _theme(p.get("theme"))
    s = p.get("summary") or {}
    if not s.get("available"):
        raise RuntimeError("AI 复盘数据不可用")
    n = min(len(s.get("mistakes") or []), int(p.get("max_rows") or 6))
    dn = (min(len(s.get("dangers") or []), int(p.get("max_dangers") or 3))
          if s.get("has_probs") else 0)
    # 末尾 44 = 间隙 6 + 尾行 24 + `_finish` 边距 14 + 余量 2（保证首遍绘制即不越界）
    est = (60 + 30 + (86 + 16) + (28 + n * _AI_ROW_H if n else 58)
           + (28 + dn * 28 if dn else 24) + 44)
    img = Image.new("RGB", (W, min(H_AI_MAX, max(320, est))), th["bg"])
    d = ImageDraw.Draw(img)
    y = _paint_ai(img, d, p, th)
    if y + 14 > img.height:                      # 估算失效 → 按实测重绘
        img = Image.new("RGB", (W, min(H_AI_MAX, y + 16)), th["bg"])
        d = ImageDraw.Draw(img)
        y = _paint_ai(img, d, p, th)
    return _png(_finish(img, y + 14, bg=th["bg"]))
