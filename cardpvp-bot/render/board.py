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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).parent.parent
BACKGROUND_PATH = ROOT / "assets" / "board" / "hex_background.png"
CARDS_DIR = ROOT / "assets" / "cards"
BLANK_CARD_PATH = CARDS_DIR / "_blank.png"
ICONS_DIR = ROOT / "assets" / "icons"

BOARD_W, BOARD_H = 900, 600
CARD_SLOT_W, CARD_SLOT_H = 260, 340

# Fill-level sprite sets (5 discrete frames each, not a continuous procedural
# fill) -- thresholds are the midpoints between each sprite's actual measured
# fill percentage, so a given HP/Mana fraction snaps to whichever pre-made
# frame it's visually closest to.
HEART_LEVELS = [
    (0.925, "heart_1.png"),  # ~94% fill
    (0.805, "heart_2.png"),  # ~91% fill
    (0.585, "heart_3.png"),  # ~70% fill
    (0.355, "heart_4.png"),  # ~47% fill
    (0.0, "heart_5.png"),    # ~24% fill -- also the floor for anything lower, including 0
]
POTION_LEVELS = [
    (0.66, "potion_1.png"),   # ~71% fill
    (0.565, "potion_2.png"),  # ~61% fill
    (0.46, "potion_3.png"),   # ~52% fill
    (0.33, "potion_4.png"),   # ~40% fill
    (0.0, "potion_5.png"),    # ~26% fill -- also the floor for anything lower, including 0
]

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


def _select_sprite(fraction: float, levels: list[tuple[float, str]]) -> str:
    fraction = max(0.0, min(1.0, fraction))
    for threshold, filename in levels:
        if fraction >= threshold:
            return filename
    return levels[-1][1]


_icon_cache: dict[str, Image.Image] = {}


def _load_icon_sprite(filename: str) -> Image.Image:
    """Loads an icon sprite, auto-cropped to its visible silhouette -- the source images
    have a very faint semi-transparent halo (soft shadow / anti-aliasing falloff) extending
    almost to the canvas edges, invisible to the eye but non-zero, which plain getbbox()
    would include -- so crop against a THRESHOLDED alpha mask instead, or the "crop"
    does effectively nothing and the real icon gets squished when resized into a much
    smaller target box. Cached since these are static files reloaded on every board render."""
    cached = _icon_cache.get(filename)
    if cached is not None:
        return cached
    img = Image.open(ICONS_DIR / filename).convert("RGBA")
    alpha_mask = img.split()[-1].point(lambda a: 255 if a > 25 else 0)
    bbox = alpha_mask.getbbox()
    if bbox:
        img = img.crop(bbox)
    _icon_cache[filename] = img
    return img


def _render_heart_icon(fraction: float, size: tuple[int, int]) -> Image.Image:
    """Loads the pre-made heart sprite (see assets/icons/) whose fill level is closest to
    `fraction` (0..1) and scales it to `size`. Used for the HP indicator."""
    filename = _select_sprite(fraction, HEART_LEVELS)
    return _load_icon_sprite(filename).resize(size, Image.LANCZOS)


def _render_potion_icon(fraction: float, size: tuple[int, int]) -> Image.Image:
    """Loads the pre-made potion sprite (see assets/icons/) whose fill level is closest to
    `fraction` (0..1) and scales it to `size`. Used for the Mana indicator."""
    filename = _select_sprite(fraction, POTION_LEVELS)
    return _load_icon_sprite(filename).resize(size, Image.LANCZOS)




def _load_background() -> Image.Image:
    return Image.open(BACKGROUND_PATH).convert("RGB").copy()


def _load_card_image(card_id: Optional[str]) -> Image.Image:
    path = CARDS_DIR / f"{card_id}.png" if card_id else BLANK_CARD_PATH
    if not path.exists():
        path = BLANK_CARD_PATH
    return Image.open(path).convert("RGB")


HAND_THUMB_W, HAND_THUMB_H = 180, 252  # matches the card art's ~0.714 aspect ratio
HAND_GAP = 24
HAND_MARGIN = 24
HAND_BADGE_H = 44


def render_hand_image(card_ids: list[Optional[str]], usable_flags: Optional[list[bool]] = None) -> bytes:
    """
    Renders a horizontal row of card-art thumbnails, each with a numbered badge above it,
    for the ephemeral hand-selection menu. If `usable_flags` is given (one bool per card),
    any card flagged False is desaturated and darkened -- shown for context (so the player
    can see their whole hand and understand *why* nothing helps) but visually marked as
    not a valid pick, matching the numbered buttons Discord-side (which only appear for
    usable cards).
    """
    n = max(1, len(card_ids))
    width = HAND_MARGIN * 2 + n * HAND_THUMB_W + (n - 1) * HAND_GAP
    height = HAND_MARGIN * 2 + HAND_BADGE_H + HAND_THUMB_H

    img = Image.new("RGB", (width, height), (30, 32, 40))
    draw = ImageDraw.Draw(img)

    for i, card_id in enumerate(card_ids):
        usable = usable_flags[i] if usable_flags else True
        x = HAND_MARGIN + i * (HAND_THUMB_W + HAND_GAP)
        y = HAND_MARGIN + HAND_BADGE_H

        thumb = _load_card_image(card_id).resize((HAND_THUMB_W, HAND_THUMB_H))
        if not usable:
            grey = ImageOps.grayscale(thumb).convert("RGB")
            thumb = Image.blend(thumb, grey, 0.75)
            dark_overlay = Image.new("RGB", thumb.size, (0, 0, 0))
            thumb = Image.blend(thumb, dark_overlay, 0.35)
        img.paste(thumb, (x, y))
        draw.rectangle([x, y, x + HAND_THUMB_W, y + HAND_THUMB_H],
                       outline=(255, 255, 255) if usable else (110, 110, 118), width=3)

        badge_color = (90, 160, 235) if usable else (80, 80, 90)
        badge_r = 19
        bx, by = x + HAND_THUMB_W / 2, HAND_MARGIN + HAND_BADGE_H / 2
        draw.ellipse([bx - badge_r, by - badge_r, bx + badge_r, by + badge_r],
                     fill=badge_color, outline=(255, 255, 255), width=2)
        _centered(draw, bx, by - 12, str(i + 1), _font(19), (255, 255, 255))

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.getvalue()


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

    heart_size = (80, 66)  # matches the sprite's native ~1.22:1 aspect ratio
    heart_cy = 54
    heart_frac = player.hp / player.max_hp if player.max_hp else 0
    heart_img = _render_heart_icon(heart_frac, heart_size)
    img.paste(heart_img, (int(icon_x - heart_size[0] / 2), int(heart_cy - heart_size[1] / 2)), heart_img)
    _outlined_text(draw, icon_x, heart_cy - 4, str(player.hp), _font(17))
    _centered(draw, icon_x, heart_cy + heart_size[1] / 2 + 4, "HP", _font(11), MUTED_COLOR)

    potion_size = (64, 104)  # matches the sprite's native ~0.61:1 aspect ratio
    potion_cy = 157
    mana_frac = player.mana / player.max_mana if player.max_mana else 0
    potion_img = _render_potion_icon(mana_frac, potion_size)
    img.paste(potion_img, (int(icon_x - potion_size[0] / 2), int(potion_cy - potion_size[1] / 2)), potion_img)
    _outlined_text(draw, icon_x, potion_cy + 30, str(player.mana), _font(16))
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