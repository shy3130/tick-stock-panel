"""生成应用图标 — 白色 squircle 背景 + 紫色 Logo。

设计：
  背景: 白色 squircle (macOS Big Sur+ 要求不透明 squircle 背景, 用白色填充)
  内容: 紫色 #5B21B6 方括号 + K线 (上影短/下影长, bullish 站稳)

蜡烛几何 (32x32 viewBox):
  wick: y=7 ~ y=25
  body: y=9 ~ y=19 (偏上)
  → 上影 = 2 (短), 下影 = 6 (长)

每尺寸独立绘制: 线条占比全尺寸统一 (~9.4%), 小尺寸微调补偿, 不随尺寸递减。
运行: python packaging/generate_icon.py
产物:
  packaging/icon.ico   — Windows (16/32/48/64/128/256)
  packaging/icon.icns  — macOS   (16/32/64/128/256/512, 含 @2x)
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT_ICO = Path(__file__).parent / "icon.ico"
OUT_ICNS = Path(__file__).parent / "icon.icns"

# 背景: 白色 squircle (不透明, macOS 规范)
BG = (255, 255, 255, 255)
# logo 线条色: 紫色 #5B21B6 (与 Logo.tsx / favicon.svg 一致)
LOGO = (91, 33, 182, 255)       # #5B21B6
# wick 影线: 同色不透明 (半透明在小尺寸会糊掉)
LOGO_WICK = (91, 33, 182, 255)


def _draw(size: int, sw_b: float, sw_w: float, body_w: float) -> Image.Image:
    """绘制单尺寸图标 (像素直接映射 32-viewBox)。"""
    factor = max(8, 2048 // size)
    s = size * factor
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 品牌色 squircle 背景 (圆角方块, macOS Big Sur+ 规范形状, 不透明)
    # 纯色填充, 不用渐变 — 渐变在 16px 小尺寸下看不出, 反而易引入 alpha 合成 bug
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=BG)

    def p(v):
        return v * s / 32

    swb = max(1, int(round(sw_b * factor)))
    sww = max(1, int(round(sw_w * factor)))

    # 方括号 [ ]
    for pts in [[(10, 4), (4, 4), (4, 28), (10, 28)], [(22, 4), (28, 4), (28, 28), (22, 28)]]:
        scaled = [(p(x), p(y)) for x, y in pts]
        for i in range(len(scaled) - 1):
            d.line([scaled[i], scaled[i + 1]], fill=LOGO, width=swb, joint="curve")

    # wick 影线 (上短下长)
    d.line([(p(16), p(7)), (p(16), p(25))], fill=LOGO_WICK, width=sww)
    wcap = sww // 2 + 1
    for cy in [7, 25]:
        d.ellipse([p(16) - wcap, p(cy) - wcap, p(16) + wcap, p(cy) + wcap], fill=LOGO_WICK)

    # body 实体 (偏上 → 上影短下影长)
    d.rounded_rectangle(
        [p(16 - body_w / 2), p(9), p(16 + body_w / 2), p(19)],
        radius=p(0.5), fill=LOGO,
    )

    return img.resize((size, size), Image.LANCZOS)


def draw_logo(size: int) -> Image.Image:
    """按尺寸选参数, 保证各尺寸视觉粗细一致。

    核心原则: 所有尺寸线条占图标的视觉比例统一, 不能出现"大尺寸反而显细"。
    基准定为 sw=4.0/32 (12.5%) — 经多轮对比选定, Dock 放大时线条仍清晰有力。
    小尺寸在基准上略加粗, 抵消像素化糊掉; 大尺寸统一用基准, 不递减。
    """
    if size <= 16:
        return _draw(size, sw_b=4.5, sw_w=3.8, body_w=9)
    elif size <= 32:
        return _draw(size, sw_b=4.2, sw_w=3.6, body_w=8)
    else:
        # ≥48px 统一用基准 4.0, 不随尺寸递减
        return _draw(size, sw_b=4.0, sw_w=3.4, body_w=8)


def _save_ico(images_by_size: dict[int, Image.Image]) -> None:
    """Windows .ico (16/32/48/64/128/256)。主图 256 优先, 资源管理器大图清晰。"""
    sizes = [16, 32, 48, 64, 128, 256]
    images = [images_by_size[s] for s in sizes]
    images[-1].save(
        OUT_ICO, format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[:-1],
    )
    print(f"生成: {OUT_ICO} (尺寸 {sizes})")


def _save_icns(images_by_size: dict[int, Image.Image]) -> None:
    """macOS .icns (16/32/64/128/256/512)。

    Apple 要求 .icns 至少含一张大图 (512 或 1024) 才合法, 否则 Finder/Dock
    不显示。这里用 512 作主图 (Pillow 12 写 1024 需额外编码, 512 已覆盖
    Retina @2x)。@2x 高清由系统从大图自动缩放, 无需单独提供。
    """
    sizes = [16, 32, 64, 128, 256, 512]
    images = [images_by_size[s] for s in sizes]
    images[-1].save(
        OUT_ICNS, format="ICNS",
        append_images=images[:-1],
    )
    print(f"生成: {OUT_ICNS} (尺寸 {sizes})")


def main() -> None:
    # 一次绘制所有用到的尺寸 (ico 和 icns 取并集), 避免重复绘制
    all_sizes = {16, 32, 48, 64, 128, 256, 512}
    images_by_size = {sz: draw_logo(sz) for sz in all_sizes}
    _save_ico(images_by_size)
    _save_icns(images_by_size)


if __name__ == "__main__":
    main()
