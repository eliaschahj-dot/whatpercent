# -*- coding: utf-8 -*-
"""로고 마크에서 favicon.ico / apple-touch-icon.png / og-image.png 생성."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

STATIC = Path(__file__).parent / "static"
BLUE, RED = (58, 107, 201), (232, 80, 58)
INK, SOFT, BG = (34, 37, 31), (92, 97, 86), (244, 245, 242)
FONT_BOLD = r"C:\Windows\Fonts\malgunbd.ttf"


def gradient(size):
    g = Image.new("RGB", (2, 2))
    g.putpixel((0, 0), BLUE)
    g.putpixel((1, 0), (145, 94, 130))
    g.putpixel((0, 1), (145, 94, 130))
    g.putpixel((1, 1), RED)
    return g.resize(size, Image.BICUBIC)


def draw_mark(size: int, corner_ratio=0.23) -> Image.Image:
    """정사각 로고 마크 (그라데이션 배경 + 흰 % 기호)."""
    s = size * 4  # 4x 슈퍼샘플링 후 축소 (안티앨리어싱)
    img = gradient((s, s)).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * corner_ratio), fill=255)
    img.putalpha(mask)

    d = ImageDraw.Draw(img)
    u = s / 64.0  # 64 기준 좌표계
    w = 5.5 * u

    def circle(cx, cy, r):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 255), width=int(w))

    circle(23 * u, 23 * u, 8 * u)
    circle(41 * u, 41 * u, 8 * u)
    d.line([44 * u, 17 * u, 20 * u, 47 * u], fill=(255, 255, 255, 255), width=int(6 * u))
    for cx, cy in [(44 * u, 17 * u), (20 * u, 47 * u)]:
        r = 3 * u
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


# 1) favicon.ico (16/32/48)
mark32 = draw_mark(48)
mark32.save(STATIC.parent / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])

# 2) apple-touch-icon (180x180, 불투명 배경 요구)
touch = Image.new("RGB", (180, 180), BG)
m = draw_mark(180)
touch.paste(m, (0, 0), m)
touch.save(STATIC / "apple-touch-icon.png")

# 3) og-image (1200x630) — 카톡/SNS 공유 카드
og = Image.new("RGB", (1200, 630), BG)
d = ImageDraw.Draw(og)
m = draw_mark(200)
og.paste(m, (100, 130), m)

f_big = ImageFont.truetype(FONT_BOLD, 118)
f_sub = ImageFont.truetype(FONT_BOLD, 46)
f_small = ImageFont.truetype(FONT_BOLD, 30)

d.text((340, 150), "몇프로", font=f_big, fill=INK)
w_title = d.textlength("몇프로", font=f_big)
d.text((340 + w_title + 12, 150), "%", font=f_big, fill=RED)
d.text((104, 400), "내 생각, 대중은 몇 %가 공감할까?", font=f_sub, fill=SOFT)
d.text((104, 540), "whatpercent.co.kr · AI 대중 공감 온도계", font=f_small, fill=(139, 144, 131))
og.save(STATIC / "og-image.png")

print("generated:", [p.name for p in [STATIC.parent / 'favicon.ico', STATIC / 'apple-touch-icon.png', STATIC / 'og-image.png']])
