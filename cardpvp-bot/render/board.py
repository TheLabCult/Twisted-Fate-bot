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

from PIL import Image, ImageDraw, ImageFont, ImageChops, ImageFilter

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


SS = 4  # supersample factor for smooth anti-aliased edges on the small HP/Mana icons


def _render_heart_icon(fraction: float, size: tuple[int, int]) -> Image.Image:
    """A chubby, glossy heart (two circular lobes + a kite-shaped bottom), grey-blue when
    empty and filling with red liquid from the bottom up according to `fraction` (0..1) --
    matches the reference "glass heart" game-icon style. Used for the HP indicator."""
    w, h = size
    W, H = w * SS, h * SS
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    r = W * 0.30
    cy_lobe = H * 0.34
    cx1, cx2 = W * 0.32, W * 0.68

    def heart_shape(d, fill):
        d.ellipse([cx1 - r, cy_lobe - r, cx1 + r, cy_lobe + r], fill=fill)
        d.ellipse([cx2 - r, cy_lobe - r, cx2 + r, cy_lobe + r], fill=fill)
        d.polygon([
            (W * 0.5, H * 0.24),   # top point tucks into the valley between the lobes
            (W * 0.97, H * 0.40),  # right point reaches the right lobe's outer edge
            (W * 0.5, H * 0.94),   # bottom tip
            (W * 0.03, H * 0.40),  # left point reaches the left lobe's outer edge
        ], fill=fill)

    # unified silhouette mask -- single source of truth, so there are no seams
    # where the two circles and the wedge meet (drawing separate outlines per
    # shape instead causes visible line artifacts at the overlaps).
    mask = Image.new("L", (W, H), 0)
    heart_shape(ImageDraw.Draw(mask), 255)

    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow_layer.paste((0, 0, 0, 150), (0, int(H * 0.06)), mask)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(SS * 2.2))
    img.alpha_composite(shadow_layer)

    base_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    base_layer.paste((150, 168, 190, 255), (0, 0), mask)
    img.alpha_composite(base_layer)

    top_y, bottom_y = cy_lobe - r, H * 0.94
    fill_h = (bottom_y - top_y) * max(0.0, min(1.0, fraction))
    fill_top = bottom_y - fill_h
    liquid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if fill_h > 0:
        ld = ImageDraw.Draw(liquid)
        ld.rectangle([0, fill_top, W, H], fill=(210, 35, 48, 255))
        ld.rectangle([0, fill_top, W, fill_top + SS * 1.5], fill=(150, 20, 34, 255))  # seam shading
    liquid.putalpha(ImageChops.multiply(liquid.split()[3], mask))
    img.alpha_composite(liquid)

    # outline derived by eroding the SAME mask -- guarantees a clean, seamless ring
    eroded = mask.filter(ImageFilter.MinFilter(int(SS * 3.2) * 2 + 1))
    ring = ImageChops.subtract(mask, eroded)
    img.paste((255, 255, 255, 255), (0, 0), ring)

    hl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    hd.ellipse([cx1 - r * 0.55, cy_lobe - r * 0.75, cx1 + r * 0.15, cy_lobe - r * 0.05], fill=(255, 255, 255, 200))
    hd.ellipse([cx2 - r * 0.55, cy_lobe - r * 0.75, cx2 + r * 0.15, cy_lobe - r * 0.05], fill=(255, 255, 255, 160))
    img.alpha_composite(hl)

    return img.resize((w, h), Image.LANCZOS)


def _render_potion_icon(fraction: float, size: tuple[int, int]) -> Image.Image:
    """A glossy glass potion bottle -- round bulb, narrow neck with a rim highlight, and a
    textured cork -- filling with blue liquid from the bottom up according to `fraction`
    (0..1). Matches the reference game-icon style. Used for the Mana indicator."""
    w, h = size
    W, H = w * SS, h * SS
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    bulb_r = W * 0.40
    bulb_cx, bulb_cy = W * 0.5, H * 0.62
    neck_w, neck_h = W * 0.30, H * 0.16
    neck_top = bulb_cy - bulb_r - neck_h * 0.55
    cork_w, cork_h = W * 0.36, H * 0.16
    cork_top = neck_top - cork_h * 0.65

    def bottle_shape(d, fill):
        d.rounded_rectangle(
            [bulb_cx - neck_w / 2, neck_top, bulb_cx + neck_w / 2, neck_top + neck_h + bulb_r * 0.3],
            radius=neck_w * 0.15, fill=fill,
        )
        d.ellipse([bulb_cx - bulb_r, bulb_cy - bulb_r, bulb_cx + bulb_r, bulb_cy + bulb_r], fill=fill)

    mask = Image.new("L", (W, H), 0)
    bottle_shape(ImageDraw.Draw(mask), 255)

    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow_layer.paste((0, 0, 0, 150), (0, int(H * 0.04)), mask)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(SS * 2.2))
    img.alpha_composite(shadow_layer)

    base_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    base_layer.paste((160, 185, 208, 255), (0, 0), mask)
    img.alpha_composite(base_layer)

    top_y, bottom_y = neck_top, bulb_cy + bulb_r
    fill_h = (bottom_y - top_y) * max(0.0, min(1.0, fraction))
    fill_top = bottom_y - fill_h
    liquid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if fill_h > 0:
        ld = ImageDraw.Draw(liquid)
        ld.rectangle([0, fill_top, W, H], fill=(50, 160, 225, 255))
        # meniscus -- a curved highlight arc right at the liquid surface
        ld.ellipse([bulb_cx - bulb_r * 0.85, fill_top - H * 0.02, bulb_cx + bulb_r * 0.85, fill_top + H * 0.05],
                   fill=(90, 190, 240, 255))
    liquid.putalpha(ImageChops.multiply(liquid.split()[3], mask))
    img.alpha_composite(liquid)

    eroded = mask.filter(ImageFilter.MinFilter(int(SS * 2.8) * 2 + 1))
    ring = ImageChops.subtract(mask, eroded)
    img.paste((255, 255, 255, 255), (0, 0), ring)

    draw = ImageDraw.Draw(img)
    rim_y = neck_top + neck_h * 0.35
    draw.line([(bulb_cx - neck_w / 2 + SS, rim_y), (bulb_cx + neck_w / 2 - SS, rim_y)],
              fill=(230, 240, 250, 200), width=int(SS * 1.2))

    cork_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cd = ImageDraw.Draw(cork_layer)
    cd.ellipse([bulb_cx - cork_w / 2, cork_top, bulb_cx + cork_w / 2, cork_top + cork_h * 0.7],
               fill=(178, 138, 88, 255))
    cd.rounded_rectangle(
        [bulb_cx - cork_w * 0.4, cork_top + cork_h * 0.35, bulb_cx + cork_w * 0.4, cork_top + cork_h * 1.3],
        radius=cork_w * 0.15, fill=(178, 138, 88, 255),
    )
    cd.ellipse([bulb_cx - cork_w * 0.15, cork_top + cork_h * 0.15, bulb_cx + cork_w * 0.05, cork_top + cork_h * 0.4],
               fill=(140, 105, 65, 255))  # texture dot
    img.alpha_composite(cork_layer)
    draw.rounded_rectangle(
        [bulb_cx - cork_w * 0.42, cork_top - SS, bulb_cx + cork_w * 0.42, cork_top + cork_h * 1.3],
        radius=cork_w * 0.15, outline=(110, 80, 45, 255), width=int(SS * 0.8),
    )

    hl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    hd.ellipse([bulb_cx - bulb_r * 0.65, bulb_cy - bulb_r * 0.65, bulb_cx - bulb_r * 0.05, bulb_cy - bulb_r * 0.05],
               fill=(255, 255, 255, 170))
    img.alpha_composite(hl)

    return img.resize((w, h), Image.LANCZOS)


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
    last_discard_card_id: Optional[str] = None  # template_id of the top of THEIR OWN discard pile
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

    icon_x = pfp_cx + (100 if is_left else -100)

    heart_size = (80, 74)
    heart_cy = 58
    heart_frac = player.hp / player.max_hp if player.max_hp else 0
    heart_img = _render_heart_icon(heart_frac, heart_size)
    img.paste(heart_img, (int(icon_x - heart_size[0] / 2), int(heart_cy - heart_size[1] / 2)), heart_img)
    _outlined_text(draw, icon_x, heart_cy - 6, str(player.hp), _font(18))
    _centered(draw, icon_x, heart_cy + heart_size[1] / 2 + 4, "HP", _font(11), MUTED_COLOR)

    potion_size = (64, 92)
    potion_cy = 151
    mana_frac = player.mana / player.max_mana if player.max_mana else 0
    potion_img = _render_potion_icon(mana_frac, potion_size)
    img.paste(potion_img, (int(icon_x - potion_size[0] / 2), int(potion_cy - potion_size[1] / 2)), potion_img)
    _outlined_text(draw, icon_x, potion_cy + 11, str(player.mana), _font(16))
    _centered(draw, icon_x, potion_cy + potion_size[1] / 2 + 4, "Mana", _font(11), MUTED_COLOR)

    thumb_w, thumb_h = 110, 154  # matches the card art's aspect ratio (300x420)
    label_h = 22
    box_w, box_h = thumb_w + 16, thumb_h + label_h + 12
    box_x = 24 if is_left else BOARD_W - 24 - box_w
    box_y = 290

    overlay = Image.new("RGBA", (box_w, box_h), PANEL_BG)
    img.paste(overlay, (box_x, box_y), overlay)
    draw.rectangle([box_x, box_y, box_x + box_w, box_y + box_h], outline=(255, 255, 255), width=2)
    _centered(draw, box_x + box_w / 2, box_y + 6, "Last Discard", _font(13), TEXT_COLOR)

    thumb_x, thumb_y = box_x + (box_w - thumb_w) // 2, box_y + label_h
    if player.last_discard_card_id:
        thumb = _load_card_image(player.last_discard_card_id).resize((thumb_w, thumb_h))
        img.paste(thumb, (thumb_x, thumb_y))
        draw.rectangle([thumb_x, thumb_y, thumb_x + thumb_w, thumb_y + thumb_h], outline=(255, 255, 255), width=2)
    else:
        draw.rectangle([thumb_x, thumb_y, thumb_x + thumb_w, thumb_y + thumb_h],
                        fill=(45, 47, 56), outline=(140, 140, 150), width=2)
        _centered(draw, thumb_x + thumb_w / 2, thumb_y + thumb_h / 2 - 8, "No discards", _font(12), MUTED_COLOR)
        _centered(draw, thumb_x + thumb_w / 2, thumb_y + thumb_h / 2 + 8, "yet", _font(12), MUTED_COLOR)


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