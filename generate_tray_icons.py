"""Pre-render crisp tray icon frames from icon.png.

Produces tray_icons/{dark,light}_<size>.png. "dark" frames are brightened for
dark taskbars/menu bars; "light" frames keep the original tones. Each frame is
downscaled individually and sharpened so thin emblem strokes survive at 16-32px.
"""
import os
import sys

from PIL import Image, ImageEnhance, ImageFilter

SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(ROOT, "icon.png")
OUT_DIR = os.path.join(ROOT, "tray_icons")


def square_crop(img):
    box = img.getchannel("A").getbbox()
    if box:
        img = img.crop(box)
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return canvas


def brighten(img):
    rgb, alpha = img.convert("RGB"), img.getchannel("A")
    rgb = ImageEnhance.Brightness(rgb).enhance(1.6)
    rgb = ImageEnhance.Contrast(rgb).enhance(1.25)
    rgb = ImageEnhance.Color(rgb).enhance(1.15)
    result = rgb.convert("RGBA")
    result.putalpha(alpha)
    return result


def render(img, size):
    frame = img.resize((size, size), Image.LANCZOS)
    if size > 64:
        return frame
    alpha = frame.getchannel("A").point(lambda v: min(255, int(v * 1.35)))
    rgb = frame.convert("RGB").filter(ImageFilter.UnsharpMask(radius=0.8, percent=120, threshold=0))
    frame = rgb.convert("RGBA")
    frame.putalpha(alpha)
    return frame


def main():
    if not os.path.isfile(SOURCE):
        print(f"icon.png not found at {SOURCE}", file=sys.stderr)
        return 1
    source = square_crop(Image.open(SOURCE).convert("RGBA"))
    variants = {"light": source, "dark": brighten(source)}
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, img in variants.items():
        for size in SIZES:
            render(img, size).save(os.path.join(OUT_DIR, f"{name}_{size}.png"), optimize=True)
    print(f"Generated {len(variants) * len(SIZES)} tray icon frames in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
