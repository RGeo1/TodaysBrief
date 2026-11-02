"""Generate the cyberpunk app icon (PNG preview + multi-size ICO)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)

BG = (8, 8, 10, 255)
PANEL = (17, 17, 20, 255)
YELLOW = (252, 238, 10, 255)
CYAN = (0, 229, 255, 255)
WHITE = (242, 242, 242, 255)
CYAN_DIM = (0, 112, 125, 255)
MAGENTA_DIM = (125, 23, 52, 255)
SCAN = (20, 20, 24, 255)

SIZE = 256
CUT = 30


def _font(path: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _centered(draw: ImageDraw.ImageDraw, text: str, font, y: int, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    x = (SIZE - width) / 2 - bbox[0]
    draw.text((x, y), text, font=font, fill=fill)


def main() -> None:
    image = Image.new("RGBA", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(image)

    # CRT scanlines
    for y in range(0, SIZE, 6):
        draw.line([(0, y), (SIZE, y)], fill=SCAN, width=1)

    # Chamfered HUD frame
    border = [
        (CUT, 10),
        (SIZE - CUT, 10),
        (SIZE - 10, CUT),
        (SIZE - 10, SIZE - CUT),
        (SIZE - CUT, SIZE - 10),
        (CUT, SIZE - 10),
        (10, SIZE - CUT),
        (10, CUT),
        (CUT, 10),
    ]
    draw.line(border, fill=YELLOW, width=5, joint="curve")
    draw.line([(CUT + 8, 20), (SIZE - CUT - 8, 20)], fill=CYAN, width=2)
    draw.rectangle([10, 46, 19, SIZE - 46], fill=YELLOW)

    title_font = _font("C:/Windows/Fonts/msyhbd.ttc", 142)
    sub_font = _font("C:/Windows/Fonts/bahnschrift.ttf", 24)
    mark_font = _font("C:/Windows/Fonts/bahnschrift.ttf", 18)

    # Glitch title: dim offsets, white on top
    bbox = draw.textbbox((0, 0), "幕", font=title_font)
    width = bbox[2] - bbox[0]
    x = (SIZE - width) / 2 - bbox[0]
    draw.text((x - 3, 61), "幕", font=title_font, fill=CYAN_DIM)
    draw.text((x + 3, 63), "幕", font=title_font, fill=MAGENTA_DIM)
    draw.text((x, 62), "幕", font=title_font, fill=WHITE)

    _centered(draw, "// DAILY BRIEF", sub_font, 208, YELLOW)
    draw.text((SIZE - 78, 26), "//", font=mark_font, fill=CYAN)
    draw.rectangle([SIZE - 54, 230, SIZE - 46, 238], fill=CYAN)

    png_path = ASSETS / "today_brief.png"
    ico_path = ASSETS / "today_brief.ico"
    image.save(png_path)
    image.save(
        ico_path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"WROTE {png_path}")
    print(f"WROTE {ico_path}")


if __name__ == "__main__":
    main()
