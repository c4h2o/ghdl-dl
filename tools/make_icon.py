# SPDX-License-Identifier: MIT
"""生成 ghdl-dl 的图标（SVG / PNG / ICO）与 GitHub 社交预览图。

用法::

    python tools/make_icon.py

输出到 ``ghdl/assets/``：

* ``icon.svg``          矢量源文件（与 PNG 同一套几何参数）
* ``icon.png``          1024×1024 透明背景
* ``icon.ico``          Windows 多尺寸图标（16–256）
* ``social-preview.png`` 1280×640，GitHub 仓库 Social preview 用

只依赖 Pillow，不依赖网络。图标概念：深色圆角方块 + 三段式向下箭头 + 底部托盘，
“分段”对应多段并发下载，“向下箭头 + 托盘”对应下载落盘。
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "ghdl" / "assets"

SIZE = 1024
SS = 4  # 超采样倍数，先画 4096 再缩小，边缘更干净

# --- 几何参数（SVG 与 PNG 共用） -----------------------------------------
RADIUS = 224
BG_TOP = (11, 18, 32)
BG_BOTTOM = (22, 35, 59)

SHAFT_X, SHAFT_W, SHAFT_H, SHAFT_R = 434, 156, 82, 28
SHAFT_YS = (206, 302, 398)
HEAD = ((372, 480), (652, 480), (512, 682))
HEAD_R = 26
TRAY = ((352, 768), (672, 768))
TRAY_W = 40

ARROW_TOP = (103, 232, 249)  # #67E8F9
ARROW_BOTTOM = (59, 130, 246)  # #3B82F6
GLOW = (14, 165, 233)  # #0EA5E9


def lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


def vertical_gradient(width: int, height: int, top: tuple[int, int, int],
                      bottom: tuple[int, int, int]) -> Image.Image:
    column = Image.new("RGB", (1, height))
    pixels = column.load()
    for y in range(height):
        pixels[0, y] = lerp(top, bottom, y / max(1, height - 1))
    return column.resize((width, height), Image.Resampling.BILINEAR).convert("RGBA")


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def rounded_polygon_points(points, radius: float, steps: int = 8):
    """把多边形顶点替换成圆角，返回闭合多边形的点列。"""
    out: list[tuple[float, float]] = []
    n = len(points)
    for i, (vx, vy) in enumerate(points):
        ax, ay = points[(i - 1) % n]
        bx, by = points[(i + 1) % n]
        ux, uy = ax - vx, ay - vy
        lu = math.hypot(ux, uy) or 1.0
        ux, uy = ux / lu, uy / lu
        wx, wy = bx - vx, by - vy
        lw = math.hypot(wx, wy) or 1.0
        wx, wy = wx / lw, wy / lw
        theta = math.acos(max(-1.0, min(1.0, ux * wx + uy * wy)))
        if theta < 1e-3 or theta > math.pi - 1e-3:
            out.append((vx, vy))
            continue
        d = radius / math.sin(theta / 2)
        bx_, by_ = ux + wx, uy + wy
        lb = math.hypot(bx_, by_)
        if lb < 1e-9:
            out.append((vx, vy))
            continue
        cx, cy = vx + bx_ / lb * d, vy + by_ / lb * d
        tangent = radius / math.tan(theta / 2)
        a1 = math.atan2(vy + uy * tangent - cy, vx + ux * tangent - cx)
        a2 = math.atan2(vy + wy * tangent - cy, vx + wx * tangent - cx)
        diff = (a2 - a1 + math.pi) % (2 * math.pi) - math.pi
        for step in range(steps + 1):
            a = a1 + diff * step / steps
            out.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    return out


def arrow_mask(size: int) -> Image.Image:
    """三段箭杆 + 圆角箭头 + 底部托盘，合并成一张遮罩。"""
    scale = size / SIZE
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)

    for y in SHAFT_YS:
        draw.rounded_rectangle(
            [SHAFT_X * scale, y * scale, (SHAFT_X + SHAFT_W) * scale, (y + SHAFT_H) * scale],
            radius=SHAFT_R * scale,
            fill=255,
        )

    head = [(x * scale, y * scale) for x, y in rounded_polygon_points(HEAD, HEAD_R)]
    draw.polygon(head, fill=255)

    tray = [(x * scale, y * scale) for x, y in TRAY]
    tray_width = round(TRAY_W * scale)
    draw.line(tray, fill=255, width=tray_width)
    r = tray_width / 2
    for x, y in tray:
        draw.ellipse([x - r, y - r, x + r, y + r], fill=255)

    return mask


def render_icon() -> Image.Image:
    size = SIZE * SS
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # 1. 圆角底板 + 渐变
    background = vertical_gradient(size, size, BG_TOP, BG_BOTTOM)
    canvas.paste(background, (0, 0), rounded_mask(size, RADIUS * SS))

    # 2. 箭头后面的柔光（低分辨率画好再放大，省时间）
    glow = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(glow).ellipse([512 - 340, 430 - 340, 512 + 340, 430 + 340], fill=255)
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    glow = glow.point(lambda v: int(v * 0.32)).resize((size, size), Image.Resampling.BILINEAR)
    glow_layer = Image.new("RGBA", (size, size), GLOW + (0,))
    glow_layer.putalpha(glow)
    canvas.alpha_composite(glow_layer)

    # 3. 箭头投影
    mask = arrow_mask(size)
    shadow = mask.resize((SIZE, SIZE), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(16))
    shadow = shadow.point(lambda v: int(v * 0.55)).resize((size, size), Image.Resampling.BILINEAR)
    shadow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow_layer.paste((0, 0, 0, 255), (0, 0), shadow)
    canvas.alpha_composite(shadow_layer, (0, round(12 * SS)))

    # 4. 箭头渐变
    gradient = vertical_gradient(size, size, ARROW_TOP, ARROW_BOTTOM)
    canvas.paste(gradient, (0, 0), mask)

    # 5. 内侧细边框
    border = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(border).rounded_rectangle(
        [SS, SS, size - 1 - SS, size - 1 - SS],
        radius=RADIUS * SS,
        outline=(255, 255, 255, 22),
        width=2 * SS,
    )
    canvas.alpha_composite(border)

    return canvas.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def build_svg() -> str:
    shafts = "\n".join(
        f'    <rect x="{SHAFT_X}" y="{y}" width="{SHAFT_W}" height="{SHAFT_H}" rx="{SHAFT_R}"/>'
        for y in SHAFT_YS
    )
    (tx1, ty1), (tx2, ty2) = TRAY
    head_points = rounded_polygon_points(HEAD, HEAD_R)
    head_path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in head_points) + " Z"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024" role="img" aria-label="ghdl-dl icon">
  <title>ghdl-dl</title>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#0B1220"/>
      <stop offset="1" stop-color="#16233B"/>
    </linearGradient>
    <linearGradient id="arrow" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#67E8F9"/>
      <stop offset="1" stop-color="#3B82F6"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.42" r="0.34">
      <stop offset="0" stop-color="#0EA5E9" stop-opacity="0.32"/>
      <stop offset="1" stop-color="#0EA5E9" stop-opacity="0"/>
    </radialGradient>
  </defs>

  <rect width="1024" height="1024" rx="{RADIUS}" fill="url(#bg)"/>
  <rect width="1024" height="1024" rx="{RADIUS}" fill="url(#glow)"/>

  <g fill="url(#arrow)">
{shafts}
  </g>

  <path d="{head_path}" fill="url(#arrow)"/>

  <line x1="{tx1}" y1="{ty1}" x2="{tx2}" y2="{ty2}"
        stroke="#3B82F6" stroke-width="{TRAY_W}" stroke-linecap="round"/>

  <rect x="1" y="1" width="1022" height="1022" rx="{RADIUS - 1}"
        fill="none" stroke="#FFFFFF" stroke-opacity="0.08" stroke-width="2"/>
</svg>
"""


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ["segoeuib.ttf" if bold else "segoeui.ttf", "arialbd.ttf" if bold else "arial.ttf"]
    for name in names:
        path = Path("C:/Windows/Fonts") / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def render_social(icon: Image.Image) -> Image.Image:
    width, height = 1280, 640
    canvas = vertical_gradient(width, height, (9, 15, 28), (17, 28, 48)).convert("RGB")

    # 左侧柔光
    glow = Image.new("L", (width, height), 0)
    ImageDraw.Draw(glow).ellipse([-120, -60, 620, 700], fill=255)
    glow = glow.filter(ImageFilter.GaussianBlur(150)).point(lambda v: int(v * 0.18))
    tint = Image.new("RGB", (width, height), GLOW)
    canvas = Image.composite(tint, canvas, glow)

    tile = icon.resize((264, 264), Image.Resampling.LANCZOS)
    canvas.paste(tile, (84, 188), tile)

    draw = ImageDraw.Draw(canvas)
    draw.text((404, 138), "ghdl-dl", font=load_font(108, bold=True), fill="#F8FAFC")
    draw.text(
        (408, 322),
        "Resumable downloads for large GitHub release assets",
        font=load_font(30),
        fill="#CBD5E1",
    )
    draw.text(
        (408, 392),
        "Multi-connection  ·  Auto-resume  ·  SHA-256 verification",
        font=load_font(24),
        fill="#38BDF8",
    )
    draw.text(
        (408, 452),
        "github.com/c4h2o/ghdl-dl",
        font=load_font(20),
        fill="#64748B",
    )
    return canvas


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    svg_path = OUT / "icon.svg"
    svg_path.write_text(build_svg(), encoding="utf-8")

    icon = render_icon()
    icon.save(OUT / "icon.png")
    icon.save(
        OUT / "icon.ico",
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)],
    )

    social = render_social(icon)
    social.save(OUT / "social-preview.png", optimize=True)

    for name in ("icon.svg", "icon.png", "icon.ico", "social-preview.png"):
        path = OUT / name
        print(f"{name:22} {path.stat().st_size:>8} bytes")


if __name__ == "__main__":
    main()
