"""图标设计方案预览 — 生成多个精致方案供挑选。

每个方案以 1024x1024 超清绘制 (4x 超采样抗锯齿), 输出 PNG 预览。
选定后, 用 generate_icon.py 产出最终多尺寸 .ico。

品牌色系 (从 favicon #5B21B6 延伸):
  深紫 #2E1065  主紫 #5B21B6  亮紫 #7C3AED  浅紫 #A78BFA
  强调金 #FBBF24 (金融上涨感)
"""
from __future__ import annotations

from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).parent / "designs"
OUT.mkdir(exist_ok=True)

# 品牌色
DEEP = (46, 16, 101)        # #2E1065
MAIN = (91, 33, 182)        # #5B21B6
BRIGHT = (124, 58, 237)     # #7C3AED
LIGHT = (167, 139, 250)     # #A78BFA
GOLD = (251, 191, 36)       # #FBBF24
WHITE = (255, 255, 255)


def _supersample(size: int, factor: int = 4):
    """返回 (大画布, 缩放比, 最终尺寸)。绘制后缩放回 size, 抗锯齿。"""
    s = size * factor
    return Image.new("RGBA", (s, s), (0, 0, 0, 0)), factor, s


def _finish(img: Image.Image, size: int) -> Image.Image:
    """超采样缩小到目标尺寸。"""
    return img.resize((size, size), Image.LANCZOS)


def _v_gradient(size: int, c_top, c_bot, radius_ratio: float = 0.0):
    """竖直渐变填充 (可选圆角)。返回 RGBA Image。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    for y in range(size):
        t = y / max(1, size - 1)
        r = int(c_top[0] + (c_bot[0] - c_top[0]) * t)
        g = int(c_top[1] + (c_bot[1] - c_top[1]) * t)
        b = int(c_top[2] + (c_bot[2] - c_top[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b, 255)
    if radius_ratio > 0:
        mask = Image.new("L", (size, size), 0)
        md = ImageDraw.Draw(mask)
        r = int(size * radius_ratio)
        md.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
        img.putalpha(mask)
    return img


def _glow(size: int, draw_fn, color, blur_ratio: float = 0.04):
    """对绘制内容做柔和光晕: 先画到独立图层, 模糊后叠加。"""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    draw_fn(d)
    glow = layer.filter(ImageFilter.GaussianBlur(size * blur_ratio))
    return glow


# ── 方案 1: 渐变紫底 + 白色发光主体 (现代 App 风格) ──────────────
def design_1(size: int = 1024) -> Image.Image:
    img, f, s = _supersample(size)

    # 圆角渐变背景 (深紫 → 亮紫)
    bg = _v_gradient(s, BRIGHT, DEEP, radius_ratio=0.22)
    img.alpha_composite(bg)

    d = ImageDraw.Draw(img)
    u = s / 32  # 单位 (基于32基准)

    # 白色方括号 (圆头粗线)
    sw = 2.8 * u
    def draw_brackets(dd):
        # 左 [
        dd.line([(11, 6), (5, 6)], fill=WHITE, width=int(sw), joint="curve")
        dd.line([(5, 6), (5, 26)], fill=WHITE, width=int(sw))
        dd.line([(5, 26), (11, 26)], fill=WHITE, width=int(sw))
        # 右 ]
        dd.line([(21, 6), (27, 6)], fill=WHITE, width=int(sw))
        dd.line([(27, 6), (27, 26)], fill=WHITE, width=int(sw))
        dd.line([(27, 26), (21, 26)], fill=WHITE, width=int(sw))

    # 白色 K 线 wick
    def draw_wick(dd):
        dd.line([(16, 8), (16, 24)], fill=LIGHT, width=int(1.6 * u))
    # 白色 K 线 body
    def draw_body(dd):
        dd.rounded_rectangle(
            [12.5 * u, 12 * u, 19.5 * u, 21 * u],
            radius=1.2 * u, fill=WHITE,
        )

    # 光晕层 (放大模糊)
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    draw_body(gd)
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.025))
    # 光晕染色为亮紫
    tint = Image.new("RGBA", (s, s), BRIGHT + (0,))
    glow = Image.composite(tint, Image.new("RGBA", (s, s), (0, 0, 0, 0)),
                           glow.point(lambda a: min(255, int(a * 0.6))))
    img.alpha_composite(glow)

    draw_brackets(d)
    draw_wick(d)
    draw_body(d)

    return _finish(img, size)


# ── 方案 2: 透明底 + 渐变紫发光主体 (优雅线条) ──────────────────
def design_2(size: int = 1024) -> Image.Image:
    img, f, s = _supersample(size)
    d = ImageDraw.Draw(img)
    u = s / 32

    sw = 3.0 * u

    # 渐变括号: 用浅紫到亮紫。Pillow line 不支持渐变, 分段填色模拟。
    def gradient_brackets(dd):
        # 左括号三段, 从上(浅)到下(亮) — 简化为整体亮紫, 配光晕显层次
        col = BRIGHT
        # 左 [
        dd.line([(11, 5), (5, 5)], fill=col, width=int(sw))
        dd.line([(5, 5), (5, 27)], fill=col, width=int(sw))
        dd.line([(5, 27), (11, 27)], fill=col, width=int(sw))
        # 右 ]
        dd.line([(21, 5), (27, 5)], fill=col, width=int(sw))
        dd.line([(27, 5), (27, 27)], fill=col, width=int(sw))
        dd.line([(27, 27), (21, 27)], fill=col, width=int(sw))

    # 主体光晕 (亮紫大模糊)
    glow_layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gradient_brackets(gd)
    gd.rounded_rectangle([12.5 * u, 12 * u, 19.5 * u, 21 * u], radius=1.2 * u, fill=BRIGHT)
    glow = glow_layer.filter(ImageFilter.GaussianBlur(s * 0.05))
    img.alpha_composite(glow)

    # 主体 (亮紫)
    gradient_brackets(d)
    d.line([(16, 8), (16, 24)], fill=LIGHT, width=int(1.8 * u))
    d.rounded_rectangle([12.5 * u, 12 * u, 19.5 * u, 21 * u], radius=1.2 * u, fill=BRIGHT)

    # 高光: body 顶部一道浅紫
    d.rounded_rectangle([13.5 * u, 12.8 * u, 18.5 * u, 14.5 * u], radius=0.8 * u, fill=LIGHT)

    return _finish(img, size)


# ── 方案 3: 深色底 + 金紫上涨 K 线柱 (金融图表感) ────────────────
def design_3(size: int = 1024) -> Image.Image:
    img, f, s = _supersample(size)

    # 深紫黑圆角底
    bg = _v_gradient(s, (30, 27, 75), (15, 12, 41), radius_ratio=0.22)
    img.alpha_composite(bg)
    d = ImageDraw.Draw(img)
    u = s / 32

    # 三根上涨 K 线柱 (紫→亮紫→金), 从左到右升高
    bars = [
        # (x_center, body_top, body_bottom, wick_top, wick_bottom, color)
        (10, 17, 24, 14, 26, LIGHT),
        (16, 13, 20, 10, 22, BRIGHT),
        (22, 8, 15, 5, 17, GOLD),
    ]
    bw = 3.2 * u
    for cx, bt, bb, wt, wb, col in bars:
        # wick
        d.line([(cx * u, wt * u), (cx * u, wb * u)], fill=col, width=int(1.2 * u))
        # body (圆角)
        d.rounded_rectangle(
            [cx * u - bw / 2, bt * u, cx * u + bw / 2, bb * u],
            radius=0.8 * u, fill=col,
        )

    # 金色柱的光晕
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle([22 * u - bw / 2, 8 * u, 22 * u + bw / 2, 15 * u], radius=0.8 * u, fill=GOLD)
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.03))
    img.alpha_composite(glow)

    # 上涨趋势线 (淡, 连接柱顶)
    d.line([(10 * u, 17 * u), (16 * u, 13 * u), (22 * u, 8 * u)],
           fill=(255, 255, 255, 120), width=int(0.6 * u))

    return _finish(img, size)


# ── 方案 4: 渐变底 + K线被方括号"框选" (品牌强调) ────────────────
def design_4(size: int = 1024) -> Image.Image:
    img, f, s = _supersample(size)

    # 斜向渐变背景 (浅紫→主紫), 圆角
    bg = _v_gradient(s, (139, 92, 246), MAIN, radius_ratio=0.22)
    img.alpha_composite(bg)
    d = ImageDraw.Draw(img)
    u = s / 32

    # 白色粗括号 (圆角连接)
    sw = 2.2 * u
    def brackets(dd):
        dd.rounded_rectangle(
            [5 * u, 6 * u, 11 * u, 26 * u],
            radius=2.0 * u, width=int(sw), outline=WHITE,
        )  # 左括号外形
        dd.rounded_rectangle(
            [21 * u, 6 * u, 27 * u, 26 * u],
            radius=2.0 * u, width=int(sw), outline=WHITE,
        )  # 右括号外形
    # 上面的画法画出的是矩形框, 改回线条式括号但圆头
    def brackets2(dd):
        cap = int(sw / 2)
        # 左 [
        dd.line([(10.5, 6), (6, 6)], fill=WHITE, width=int(sw))
        dd.rounded_rectangle([6 * u - cap, 6 * u - cap, 6 * u + cap, 26 * u + cap],
                             radius=cap, fill=WHITE)  # 竖干
        dd.line([(6, 26), (10.5, 26)], fill=WHITE, width=int(sw))
        # 圆角补点
        for cx, cy in [(6, 6), (6, 26)]:
            dd.ellipse([cx * u - cap, cy * u - cap, cx * u + cap, cy * u + cap], fill=WHITE)
        # 右 ]
        dd.line([(21.5, 6), (26, 6)], fill=WHITE, width=int(sw))
        dd.rounded_rectangle([26 * u - cap, 6 * u - cap, 26 * u + cap, 26 * u + cap],
                             radius=cap, fill=WHITE)
        dd.line([(26, 26), (21.5, 26)], fill=WHITE, width=int(sw))
        for cx, cy in [(26, 6), (26, 26)]:
            dd.ellipse([cx * u - cap, cy * u - cap, cx * u + cap, cy * u + cap], fill=WHITE)

    # K 线光晕
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rounded_rectangle([12.8 * u, 11.5 * u, 19.2 * u, 21.5 * u], radius=1.4 * u, fill=WHITE)
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.02))
    img.alpha_composite(glow)

    brackets2(d)
    # wick
    d.line([(16, 8), (16, 24)], fill=(255, 255, 255, 200), width=int(1.5 * u))
    # body (实心白)
    d.rounded_rectangle([12.8 * u, 11.5 * u, 19.2 * u, 21.5 * u], radius=1.4 * u, fill=WHITE)

    return _finish(img, size)


def main() -> None:
    designs = [
        ("方案1_渐变紫底白色发光", design_1),
        ("方案2_透明底渐变紫线条", design_2),
        ("方案3_深色底金紫上涨K线", design_3),
        ("方案4_浅紫底框选K线", design_4),
    ]
    # 拼一张对比图 (2x2)
    cell = 512
    grid = Image.new("RGBA", (cell * 2 + 60, cell * 2 + 60), (245, 245, 247, 255))
    gd = ImageDraw.Draw(grid)
    positions = [(20, 20), (cell + 40, 20), (20, cell + 40), (cell + 40, cell + 40)]
    for (name, fn), pos in zip(designs, positions):
        img = fn(cell)
        # 棋盘格背景透显
        grid.alpha_composite(img, pos)
        # 单独存
        fn(1024).save(OUT / f"{name}.png")
        print(f"  生成: {name}.png")

    grid.save(OUT / "对比图_2x2.png")
    print(f"\n对比图: {OUT / '对比图_2x2.png'}")
    print(f"单图目录: {OUT}")


if __name__ == "__main__":
    main()
