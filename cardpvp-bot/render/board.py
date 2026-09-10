"""
Composites the live battle board PNG shown in the duel channel, using the
pre-generated assets in assets/board/ and assets/cards/ (see
scripts/generate_assets.py).

Deliberately has no discord import -- it takes plain data in
(BattleRenderState) and returns PNG bytes out, the same separation
principle as game/engine.py.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageChops

ROOT = Path(__file__).parent.parent
BACKGROUND_PATH = ROOT / "assets" / "board" / "hex_background.png"
CARDS_DIR = ROOT / "assets" / "cards"
BLANK_CARD_PATH = CARDS_DIR / "_blank.png"

BOARD_W, BOARD_H = 900, 600
CARD_SLOT_W, CARD_SLOT_H = 260, 340

PFP_ACCENTS = [(79, 209, 197), (246, 173, 85)]  # left player, right player
TEXT_COLOR = (255, 255, 255)
LABEL_COLOR = (20, 20, 20)
MUTED_COLOR = (225, 235, 238)
PANEL_BG = (20, 30, 40, 165)  # semi-transparent panel behind stat boxes


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _centered(draw: ImageDraw.ImageDraw, cx: float, y: float, text: str, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text((cx - w / 2, y), text, font=font, fill=fill)


def _outlined_text(draw: ImageDraw.ImageDraw, cx: float, cy: float, text: str, font,
                    fill=(255, 255, 255), outline=(15, 15, 15), width: int = 2):
    """Centered text with a solid outline, so it stays legible over a variable-colored icon
    background (unlike plain text, which can vanish against a similarly-colored fill)."""
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = cx - w / 2, cy - h / 2
    for dx in range(-width, width + 1):
        for dy in range(-width, width + 1):
            if dx or dy:
                draw.text((x + dx, y + dy), text, font=font, fill=outline)
    draw.text((x, y), text, font=font, fill=fill)


def _heart_points(w: float, h: float, margin: float = 6) -> list[tuple[float, float]]:
    """Samples a classic parametric heart curve and fits it into a w x h box."""
    n = 100
    raw = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        raw.append((x, y))
    xs, ys = [p[0] for p in raw], [p[1] for p in raw]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    scale = min((w - 2 * margin) / (maxx - minx), (h - 2 * margin) / (maxy - miny))
    return [
        (margin + (x - minx) * scale, margin + (maxy - y) * scale)  # flip y: image coords grow downward
        for x, y in raw
    ]


def _render_heart_icon(fraction: float, size: tuple[int, int]) -> Image.Image:
    """A glass/glossy heart, empty (grey-blue) at the top and filled with red liquid from the
    bottom up according to `fraction` (0..1) -- used for the HP indicator."""
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pts = _heart_points(w, h)

    draw.polygon(pts, fill=(150, 170, 195, 255))

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)

    ys = [p[1] for p in pts]
    top, bottom = min(ys), max(ys)
    fill_h = (bottom - top) * max(0.0, min(1.0, fraction))
    fill_top = bottom - fill_h

    liquid_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if fill_h > 0:
        ImageDraw.Draw(liquid_layer).rectangle([0, fill_top, w, h], fill=(222, 55, 65, 255))
    liquid_layer.putalpha(ImageChops.multiply(liquid_layer.split()[3], mask))
    img.alpha_composite(liquid_layer)

    draw.polygon(pts, outline=(255, 255, 255, 255), width=3)
    draw.ellipse([w * 0.20, h * 0.16, w * 0.42, h * 0.36], fill=(255, 255, 255, 110))
    draw.ellipse([w * 0.56, h * 0.14, w * 0.70, h * 0.30], fill=(255, 255, 255, 85))
    return img


def _render_potion_icon(fraction: float, size: tuple[int, int]) -> Image.Image:
    """A glass potion bottle (cork + neck + round bulb), filled with blue liquid from the
    bottom up according to `fraction` (0..1) -- used for the Mana indicator."""
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cork_w, cork_h = w * 0.27, h * 0.13
    neck_w, neck_h = w * 0.29, h * 0.15
    bulb_w, bulb_h = w * 0.78, h * 0.52

    cork_box = [(w - cork_w) / 2, 2, (w + cork_w) / 2, 2 + cork_h]
    neck_box = [(w - neck_w) / 2, cork_box[3] - 2, (w + neck_w) / 2, cork_box[3] - 2 + neck_h]
    bulb_box = [(w - bulb_w) / 2, neck_box[3] - 6, (w + bulb_w) / 2, neck_box[3] - 6 + bulb_h]

    glass_color = (160, 185, 205, 255)
    draw.rounded_rectangle(neck_box, radius=4, fill=glass_color)
    draw.ellipse(bulb_box, fill=glass_color)

    mask = Image.new("L", (w, h), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle(neck_box, radius=4, fill=255)
    mdraw.ellipse(bulb_box, fill=255)

    top_overall, bottom_overall = neck_box[1], bulb_box[3]
    fill_h = (bottom_overall - top_overall) * max(0.0, min(1.0, fraction))
    fill_top = bottom_overall - fill_h

    liquid_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if fill_h > 0:
        ImageDraw.Draw(liquid_layer).rectangle([0, fill_top, w, h], fill=(55, 165, 230, 255))
    liquid_layer.putalpha(ImageChops.multiply(liquid_layer.split()[3], mask))
    img.alpha_composite(liquid_layer)

    draw.rounded_rectangle(neck_box, radius=4, outline=(255, 255, 255, 255), width=2)
    draw.ellipse(bulb_box, outline=(255, 255, 255, 255), width=3)
    draw.rounded_rectangle(cork_box, radius=3, fill=(155, 115, 65, 255), outline=(95, 68, 38, 255), width=2)

    hl_box = [bulb_box[0] + bulb_w * 0.16, bulb_box[1] + bulb_h * 0.14,
              bulb_box[0] + bulb_w * 0.38, bulb_box[1] + bulb_h * 0.42]
    draw.ellipse(hl_box, fill=(255, 255, 255, 110))
    return img


def _load_background() -> Image.Image:
    return Image.open(BACKGROUND_PATH).convert("RGB").copy()


def _load_card_image(card_id: Optional[str]) -> Image.Image:
    path = CARDS_DIR / f"{card_id}.png" if card_id else BLANK_CARD_PATH
    if not path.exists():
        path = BLANK_CARD_PATH
    return Image.open(path).convert("RGB")


@dataclass
class PlayerRenderState:
    name: str
    hp: int
    max_hp: int
    mana: int
    max_mana: int
    hand_count: int
    deck_count: int
    discard_count: int
    avatar_bytes: Optional[bytes] = None  # raw image bytes (their Discord avatar); None -> placeholder circle


@dataclass
class BattleRenderState:
    turn_number: int
    left: PlayerRenderState
    right: PlayerRenderState
    attacker_name: str
    defender_name: str
    played_card_id: Optional[str] = None  # None -> shows the "no card played" placeholder
    played_card_caption: Optional[str] = None  # small subtitle, e.g. "Alice attacked with"
    winner_name: Optional[str] = None


def _paste_circular_avatar(img: Image.Image, avatar_bytes: bytes, cx: float, cy: float, r: float) -> bool:
    """Crops the given image bytes to a circle and pastes it centered at (cx, cy). Returns False
    (leaving img untouched) if the bytes can't be decoded as an image, so the caller can fall
    back to the placeholder circle."""
    try:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    except Exception:
        return False

    size = int(r * 2)
    avatar = avatar.resize((size, size))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
    avatar.putalpha(mask)

    img.paste(avatar, (int(cx - r), int(cy - r)), avatar)
    return True


def _draw_player_side(img: Image.Image, draw: ImageDraw.ImageDraw, player: PlayerRenderState,
                       side: str, accent):
    is_left = side == "left"
    pfp_cx = 90 if is_left else BOARD_W - 90
    pfp_cy = 85
    pfp_r = 52

    pasted = False
    if player.avatar_bytes:
        pasted = _paste_circular_avatar(img, player.avatar_bytes, pfp_cx, pfp_cy, pfp_r - 3)

    if pasted:
        draw.ellipse([pfp_cx - pfp_r, pfp_cy - pfp_r, pfp_cx + pfp_r, pfp_cy + pfp_r], outline=(255, 255, 255), width=3)
    else:
        draw.ellipse([pfp_cx - pfp_r, pfp_cy - pfp_r, pfp_cx + pfp_r, pfp_cy + pfp_r], fill=accent, outline=(255, 255, 255), width=3)
        initials = "".join(w[0] for w in player.name.split()[:2]).upper() or "?"
        _centered(draw, pfp_cx, pfp_cy - 16, initials, _font(26), LABEL_COLOR)

    _centered(draw, pfp_cx, pfp_cy + pfp_r + 8, player.name, _font(15), TEXT_COLOR)

    icon_x = pfp_cx + (95 if is_left else -95)

    heart_size = (68, 62)
    heart_cy = 56
    heart_frac = player.hp / player.max_hp if player.max_hp else 0
    heart_img = _render_heart_icon(heart_frac, heart_size)
    img.paste(heart_img, (int(icon_x - heart_size[0] / 2), int(heart_cy - heart_size[1] / 2)), heart_img)
    _outlined_text(draw, icon_x, heart_cy - 4, str(player.hp), _font(18))
    _centered(draw, icon_x, heart_cy + heart_size[1] / 2 + 4, "HP", _font(11), MUTED_COLOR)

    potion_size = (54, 76)
    potion_cy = 138
    mana_frac = player.mana / player.max_mana if player.max_mana else 0
    potion_img = _render_potion_icon(mana_frac, potion_size)
    img.paste(potion_img, (int(icon_x - potion_size[0] / 2), int(potion_cy - potion_size[1] / 2)), potion_img)
    _outlined_text(draw, icon_x, potion_cy + 18, str(player.mana), _font(16))
    _centered(draw, icon_x, potion_cy + potion_size[1] / 2 + 4, "Mana", _font(11), MUTED_COLOR)

    box_w, box_h = 150, 112
    box_x = 24 if is_left else BOARD_W - 24 - box_w
    box_y = 300
    overlay = Image.new("RGBA", (box_w, box_h), PANEL_BG)
    img.paste(overlay, (box_x, box_y), overlay)
    draw.rectangle([box_x, box_y, box_x + box_w, box_y + box_h], outline=(255, 255, 255), width=2)
    _centered(draw, box_x + box_w / 2, box_y + 8, "Discard", _font(15), TEXT_COLOR)
    _centered(draw, box_x + box_w / 2, box_y + 34, str(player.discard_count), _font(24), TEXT_COLOR)
    _centered(draw, box_x + box_w / 2, box_y + 68, f"Hand {player.hand_count}", _font(13), MUTED_COLOR)
    _centered(draw, box_x + box_w / 2, box_y + 88, f"Deck {player.deck_count}", _font(13), MUTED_COLOR)


def render_battle_image(state: BattleRenderState) -> bytes:
    img = _load_background()
    draw = ImageDraw.Draw(img)

    _centered(draw, BOARD_W / 2, 10, f"Turn {state.turn_number}", _font(16), MUTED_COLOR)

    _draw_player_side(img, draw, state.left, "left", PFP_ACCENTS[0])
    _draw_player_side(img, draw, state.right, "right", PFP_ACCENTS[1])

    attacker_font = _font(30)
    _centered(draw, BOARD_W / 2, 40, "ATTACKER:", attacker_font, (10, 10, 10))
    _centered(draw, BOARD_W / 2, 74, state.attacker_name, _font(20), TEXT_COLOR)

    if state.played_card_caption:
        _centered(draw, BOARD_W / 2, 100, state.played_card_caption, _font(13), MUTED_COLOR)

    card_img = _load_card_image(state.played_card_id).resize((CARD_SLOT_W, CARD_SLOT_H))
    card_x, card_y = (BOARD_W - CARD_SLOT_W) // 2, 130
    shadow = Image.new("RGBA", (CARD_SLOT_W + 12, CARD_SLOT_H + 12), (0, 0, 0, 90))
    img.paste(shadow, (card_x - 6, card_y - 6), shadow)
    img.paste(card_img, (card_x, card_y))
    draw.rectangle([card_x, card_y, card_x + CARD_SLOT_W, card_y + CARD_SLOT_H], outline=(255, 255, 255), width=3)

    defender_font = _font(30)
    _centered(draw, BOARD_W / 2, card_y + CARD_SLOT_H + 22, "DEFENDER:", defender_font, (10, 10, 10))
    _centered(draw, BOARD_W / 2, card_y + CARD_SLOT_H + 56, state.defender_name, _font(20), TEXT_COLOR)

    if state.winner_name:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 165))
        img = img.convert("RGBA")
        img.alpha_composite(overlay)
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        _centered(draw, BOARD_W / 2, BOARD_H / 2 - 24, f"{state.winner_name}", _font(44), (255, 215, 90))
        _centered(draw, BOARD_W / 2, BOARD_H / 2 + 28, "WINS!", _font(44), (255, 215, 90))

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.getvalue()