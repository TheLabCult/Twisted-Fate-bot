"""
One-off asset generator -- creates placeholder card art (assets/cards/*.png),
a blank "no card played" placeholder, and the board background
(assets/board/hex_background.png).

Re-run this whenever data/cards.json changes (cards added/renamed/recolored):

    python scripts/generate_assets.py

These are pure placeholder graphics (flat color + shape + text), not real
illustrations -- swap in real card art later by just replacing the PNGs in
assets/cards/ with same-named files; the renderer doesn't care how they
were made.
"""

import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent.parent
CARDS_JSON = ROOT / "data" / "cards.json"
CARDS_DIR = ROOT / "assets" / "cards"
BOARD_DIR = ROOT / "assets" / "board"

CARD_W, CARD_H = 300, 420
BOARD_W, BOARD_H = 900, 600

COLOR_RGB = {
    "Red": (196, 62, 62),
    "Blue": (58, 116, 196),
    "Green": (68, 158, 98),
    "Yellow": (198, 164, 48),
    "Purple": (138, 78, 176),
}


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _centered(draw: ImageDraw.ImageDraw, cx: float, y: float, text: str, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text((cx - w / 2, y), text, font=font, fill=fill)


def _passive_summary(card: dict) -> str:
    effect = card.get("passive_effect", "block")
    if effect == "negate_color":
        return f"Passive {card['passive_cost']}: full block vs {card['passive_condition_color']}"
    if effect == "block_bonus_color":
        total = card["passive_block"] + card.get("passive_bonus_block", 0)
        return f"Passive {card['passive_cost']}: {card['passive_block']} ({total} vs {card['passive_condition_color']})"
    if effect == "reflect":
        return f"Passive {card['passive_cost']}: reflects damage"
    if effect == "reflect_color":
        return f"Passive {card['passive_cost']}: reflects vs {card['passive_condition_color']}"
    return f"Passive {card['passive_cost']}: blocks {card['passive_block']}"


def generate_card_image(card: dict) -> Image.Image:
    base = COLOR_RGB.get(card["color"], (90, 90, 100))
    light = tuple(min(255, c + 45) for c in base)
    dark = tuple(max(0, c - 45) for c in base)

    img = Image.new("RGB", (CARD_W, CARD_H), dark)
    draw = ImageDraw.Draw(img)

    for y in range(CARD_H):
        t = y / CARD_H
        row = tuple(int(light[i] * (1 - t) + dark[i] * t) for i in range(3))
        draw.line([(0, y), (CARD_W, y)], fill=row)

    draw.rectangle([4, 4, CARD_W - 5, CARD_H - 5], outline=(255, 255, 255), width=4)

    # abstract diamond -- purely decorative placeholder "art"
    cx, cy, size = CARD_W / 2, CARD_H / 2 - 25, 68
    diamond = [(cx, cy - size), (cx + size, cy), (cx, cy + size), (cx - size, cy)]
    draw.polygon(diamond, fill=tuple(min(255, c + 65) for c in base), outline=(255, 255, 255), width=3)

    name_font = _font(24)
    stat_font = _font(16)
    small_font = _font(14)

    draw.rectangle([0, 0, CARD_W, 44], fill=(0, 0, 0))
    _centered(draw, CARD_W / 2, 9, card["name"], name_font, (255, 255, 255))

    draw.rectangle([0, CARD_H - 66, CARD_W, CARD_H], fill=(0, 0, 0))
    _centered(draw, CARD_W / 2, CARD_H - 60,
              f"Active {card['active_cost']}: {card['active_damage']} dmg", stat_font, (255, 255, 255))
    _centered(draw, CARD_W / 2, CARD_H - 36, _passive_summary(card), small_font, (230, 230, 230))
    _centered(draw, CARD_W / 2, CARD_H - 16, card["color"], small_font, (200, 200, 210))

    return img


def generate_blank_card() -> Image.Image:
    img = Image.new("RGB", (CARD_W, CARD_H), (58, 60, 70))
    draw = ImageDraw.Draw(img)
    draw.rectangle([4, 4, CARD_W - 5, CARD_H - 5], outline=(140, 140, 150), width=4)
    font = _font(22)
    _centered(draw, CARD_W / 2, CARD_H / 2 - 16, "No card", font, (200, 200, 210))
    _centered(draw, CARD_W / 2, CARD_H / 2 + 12, "played yet", font, (200, 200, 210))
    return img


def generate_hex_background() -> Image.Image:
    top, bottom = (92, 200, 210), (28, 96, 128)
    img = Image.new("RGB", (BOARD_W, BOARD_H), bottom)
    draw = ImageDraw.Draw(img)
    for y in range(BOARD_H):
        t = y / BOARD_H
        row = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        draw.line([(0, y), (BOARD_W, y)], fill=row)

    hex_size = 34
    hex_w = math.sqrt(3) * hex_size
    hex_h = 2 * hex_size
    vert_spacing = hex_h * 0.75

    def hex_points(cx, cy, size):
        return [
            (cx + size * math.cos(math.radians(a)), cy + size * math.sin(math.radians(a)))
            for a in range(0, 360, 60)
        ]

    overlay = Image.new("RGBA", (BOARD_W, BOARD_H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    row, y = 0, -hex_h
    while y < BOARD_H + hex_h:
        x_offset = (hex_w / 2) if row % 2 else 0
        x = -hex_w
        while x < BOARD_W + hex_w:
            odraw.polygon(hex_points(x + x_offset, y, hex_size), outline=(255, 255, 255, 45))
            x += hex_w
        y += vert_spacing
        row += 1

    composited = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    return composited


def main():
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    BOARD_DIR.mkdir(parents=True, exist_ok=True)

    with open(CARDS_JSON) as f:
        cards = json.load(f)

    skipped = 0
    generated = 0
    for card in cards:
        if card.get("custom_art"):
            skipped += 1
            continue
        generate_card_image(card).save(CARDS_DIR / f"{card['id']}.png")
        generated += 1

    generate_blank_card().save(CARDS_DIR / "_blank.png")
    generate_hex_background().save(BOARD_DIR / "hex_background.png")

    print(f"Generated {generated} placeholder card images (skipped {skipped} with custom_art), "
          f"1 blank placeholder, and the board background in {CARDS_DIR} and {BOARD_DIR}.")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
