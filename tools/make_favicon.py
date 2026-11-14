#!/usr/bin/env python3
"""生成站点 favicon：圆角底 + 「学」字。

输出到 static/：
  favicon.ico          多尺寸 ICO（16/32/48）
  favicon-16x16.png / favicon-32x32.png
  apple-touch-icon.png 180x180（全出血方形，iOS 自行裁圆角）

想换图标时改 BG/FG/COLORS 或文字后重跑：
  .venv/bin/python tools/make_favicon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static"

SIZE = 512
RADIUS = 112          # 圆角半径（约 22%，接近系统图标观感）
BG = (9, 105, 218)    # #0969da，与站点链接色一致
FG = (255, 255, 255)
CHAR = "学"

FONT_CANDIDATES = [
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/System/Library/Fonts/STHeiti Light.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path, index in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size, index=index)
    raise FileNotFoundError("未找到可用的中文字体，请安装或手动指定 FONT_CANDIDATES")


def draw_tile(canvas: int, radius: int | None) -> Image.Image:
    """生成一帧图标。radius=None 表示全出血方形（apple-touch-icon 用）。"""
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if radius:
        d.rounded_rectangle([0, 0, canvas - 1, canvas - 1], radius=radius, fill=BG + (255,))
    else:
        d.rectangle([0, 0, canvas - 1, canvas - 1], fill=BG + (255,))
    # 字号约为画布的 62%，视觉居中（汉字字形偏上，向下微调 2%）
    font = load_font(int(canvas * 0.62))
    left, top, right, bottom = d.textbbox((0, 0), CHAR, font=font)
    x = (canvas - (right - left)) / 2 - left
    y = (canvas - (bottom - top)) / 2 - top + canvas * 0.02
    d.text((x, y), CHAR, font=font, fill=FG + (255,))
    return img


def main() -> None:
    OUT.mkdir(exist_ok=True)
    tile = draw_tile(SIZE, RADIUS)

    tile.resize((32, 32), Image.LANCZOS).save(OUT / "favicon-32x32.png")
    tile.resize((16, 16), Image.LANCZOS).save(OUT / "favicon-16x16.png")
    tile.resize((180, 180), Image.LANCZOS).save(OUT / "apple-touch-icon.png")

    ico_sizes = [(16, 16), (32, 32), (48, 48)]
    tile.save(OUT / "favicon.ico", format="ICO", sizes=ico_sizes)
    print("已生成:", ", ".join(p.name for p in sorted(OUT.glob("favicon*"))) +
          ", apple-touch-icon.png")


if __name__ == "__main__":
    main()
