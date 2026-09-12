"""
Discord-facing layer. This talks to game.engine but never contains game
rules itself.

Flow overview:
  /duel @opponent
    -> bot creates a private text channel (under a "Duels" category, made
       on first use) visible only to the two duelists + the bot
    -> challenge message posted there (Accept/Decline)
    -> on Accept, both players get a "Build My Deck" button; each opens
       their own private (ephemeral) deckbuilder
    -> once BOTH players confirm a 10-card deck, the match starts and the
       channel's board message flips into the live game
    -> on Decline/timeout/game end, the channel is auto-deleted after a
       short delay

Privacy design: hand contents only ever appear in ephemeral replies
(visible solely to the user who triggered them), even though the channel
itself is already private to just the two players -- this also keeps
your OWN hand hidden from your opponent, not just from the rest of the
server.

Requires the bot to have the "Manage Channels" permission in the server.
"""

import asyncio
import io
import sqlite3
import discord
import uuid
from pathlib import Path
from typing import Optional
from discord import app_commands
from discord.ext import commands

from game.cards import load_card_pool
from game.engine import DECK_SIZE, MAX_MANA, STARTING_HP, ActiveCondition, ActiveEffect, Card, MatchManager, PassiveEffect
from render.board import BattleRenderState, PlayerRenderState, render_battle_image

CHALLENGE_TIMEOUT_SECONDS = 120
DECKBUILD_TIMEOUT_SECONDS = 300
EPHEMERAL_TIMEOUT_SECONDS = 60
CHANNEL_CLEANUP_DELAY_SECONDS = 30       # declined / timed-out challenges -- no battle happened
BATTLE_END_CLEANUP_DELAY_SECONDS = 600   # 10 minutes -- give players time to see the final board

CATEGORIES = ["Red", "Blue", "Green", "Yellow", "Purple"]
CATEGORY_EMOJI = {"Red": "🔴", "Blue": "🔵", "Green": "🟢", "Yellow": "🟡", "Purple": "🟣"}
DUEL_CATEGORY_NAME = "Duels"

DB_PATH = Path(__file__).parent.parent / "runtime" / "duel_channels.db"


def describe_passive_compact(card: Card) -> str:
    """Short form for the deckbuilder, which has limited description space."""
    if card.passive_effect == PassiveEffect.NEGATE_COLOR:
        return f"🛡{card.passive_cost} full-block vs {card.passive_condition_color}"
    if card.passive_effect == PassiveEffect.BLOCK_BONUS_COLOR:
        total = card.passive_block + card.passive_bonus_block
        return f"🛡{card.passive_cost} {card.passive_block} ({total} vs {card.passive_condition_color})"
    if card.passive_effect == PassiveEffect.REFLECT:
        return f"🛡{card.passive_cost} reflect dmg"
    if card.passive_effect == PassiveEffect.REFLECT_COLOR:
        return f"🛡{card.passive_cost} reflect vs {card.passive_condition_color}"
    return f"🛡{card.passive_cost}/{card.passive_block}"


def describe_passive_full(card: Card) -> str:
    """Longer form for the in-match Block menu, where only 3 hand cards are ever listed."""
    if card.passive_effect == PassiveEffect.NEGATE_COLOR:
        return f"Cost {card.passive_cost} • fully blocks {card.passive_condition_color} attacks (else blocks {card.passive_block})"
    if card.passive_effect == PassiveEffect.BLOCK_BONUS_COLOR:
        total = card.passive_block + card.passive_bonus_block
        return f"Cost {card.passive_cost} • blocks {card.passive_block} (blocks {total} vs {card.passive_condition_color})"
    if card.passive_effect == PassiveEffect.REFLECT:
        return f"Cost {card.passive_cost} • reflects all damage back at the attacker"
    if card.passive_effect == PassiveEffect.REFLECT_COLOR:
        return f"Cost {card.passive_cost} • reflects vs {card.passive_condition_color} (else blocks {card.passive_block})"
    return f"Cost {card.passive_cost} • blocks {card.passive_block}"


def _active_condition_text(card: Card) -> str:
    if card.active_condition == ActiveCondition.LOWER_HP:
        return "if lower HP"
    if card.active_condition == ActiveCondition.LAST_DISCARD_COLOR:
        return f"if last discard is {card.active_condition_color}"
    if card.active_condition == ActiveCondition.HAND_ALL_COLOR:
        return f"if hand is all {card.active_condition_color}"
    return ""


def describe_active_compact(card: Card) -> str:
    """Short form for the deckbuilder."""
    if card.active_effect == ActiveEffect.SWAP_MANA:
        return f"⚔{card.active_cost} swap mana"
    if card.active_effect == ActiveEffect.SWAP_HP:
        return f"⚔{card.active_cost} swap HP"
    if card.active_effect == ActiveEffect.HEAL_IF_COLOR:
        return f"⚔{card.active_cost} heal {card.active_heal_amount} ({_active_condition_text(card)})"
    if card.active_effect == ActiveEffect.GAIN_MANA_IF_COLOR:
        return f"⚔{card.active_cost} +{card.active_mana_gain} mana ({_active_condition_text(card)})"

    base = f"⚔{card.active_cost} {card.active_damage}dmg"
    if card.active_condition != ActiveCondition.NONE and card.active_bonus_damage:
        base += f" (+{card.active_bonus_damage} {_active_condition_text(card)})"
    return base


def describe_active_full(card: Card) -> str:
    """Longer form for the in-match Attack menu."""
    if card.active_effect == ActiveEffect.SWAP_MANA:
        return f"Cost {card.active_cost} • swaps your mana with your opponent's"
    if card.active_effect == ActiveEffect.SWAP_HP:
        return f"Cost {card.active_cost} • swaps your HP with your opponent's"
    if card.active_effect == ActiveEffect.HEAL_IF_COLOR:
        return f"Cost {card.active_cost} • heal {card.active_heal_amount} HP ({_active_condition_text(card)})"
    if card.active_effect == ActiveEffect.GAIN_MANA_IF_COLOR:
        return f"Cost {card.active_cost} • gain {card.active_mana_gain} mana ({_active_condition_text(card)})"

    base = f"Cost {card.active_cost} • {card.active_damage} dmg"
    if card.active_condition != ActiveCondition.NONE and card.active_bonus_damage:
        base += f" (+{card.active_bonus_damage} {_active_condition_text(card)})"
    return base


# ---------------------------------------------------------------------------
# Channel management
# ---------------------------------------------------------------------------
#
# Every duel channel is tracked in a small SQLite table with an explicit
# status: 'active' (still in use) or 'orphaned' (decided to be deleted --
# either the delay just hasn't elapsed yet, or a previous process died
# before it could finish the job). The status flips to 'orphaned' the
# INSTANT we decide to delete something, before any delay or await --
# so if the bot dies during the wait, the next startup's sweep still
# finds it correctly flagged. A row is only removed once the channel is
# CONFIRMED gone from Discord; a failed delete (rate limit, transient
# permission issue) leaves the row in place so it gets retried later
# instead of silently falling out of tracking forever.

def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS duel_channels ("
        "  channel_id INTEGER PRIMARY KEY,"
        "  status TEXT NOT NULL DEFAULT 'active'"
        ")"
    )
    return conn


def _register_channel_id(channel_id: int) -> None:
    with _get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO duel_channels (channel_id, status) VALUES (?, 'active')",
            (channel_id,),
        )


def _mark_channel_orphaned(channel_id: int) -> None:
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO duel_channels (channel_id, status) VALUES (?, 'orphaned') "
            "ON CONFLICT(channel_id) DO UPDATE SET status = 'orphaned'",
            (channel_id,),
        )


def _unregister_channel_id(channel_id: int) -> None:
    """Only call this once the channel is CONFIRMED gone from Discord (deleted, or a
    NotFound proves it already was) -- never as a blanket 'we tried' cleanup step."""
    with _get_db() as conn:
        conn.execute("DELETE FROM duel_channels WHERE channel_id = ?", (channel_id,))


def _load_orphaned_channel_ids() -> list[int]:
    with _get_db() as conn:
        rows = conn.execute("SELECT channel_id FROM duel_channels WHERE status = 'orphaned'").fetchall()
    return [row[0] for row in rows]


async def _delete_channel_and_unregister(channel: discord.TextChannel) -> bool:
    """Attempts the actual Discord deletion. Only clears tracking on confirmed success or
    confirmed absence (NotFound) -- a Forbidden or other transient failure leaves the row
    in place so a future sweep retries it, rather than losing track of it forever."""
    try:
        await channel.delete(reason="Duel finished")
        _unregister_channel_id(channel.id)
        return True
    except discord.NotFound:
        _unregister_channel_id(channel.id)  # already gone -- fine, just stop tracking it
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False  # left tracked as 'orphaned' -- will be retried on next startup sweep


def _schedule_channel_deletion(channel: discord.TextChannel, delay: int) -> None:
    """Marks the channel orphaned IMMEDIATELY (synchronously, before any delay), then
    schedules the actual delayed deletion as a background task."""
    _mark_channel_orphaned(channel.id)
    asyncio.create_task(_delayed_delete(channel, delay))


async def _delayed_delete(channel: discord.TextChannel, delay: int) -> None:
    await asyncio.sleep(delay)
    await _delete_channel_and_unregister(channel)


def _slugify_channel_name(a: discord.Member, b: discord.Member) -> str:
    def clean(name: str) -> str:
        cleaned = "".join(ch if ch.isalnum() else "-" for ch in name.lower())
        return cleaned.strip("-") or "player"
    return f"duel-{clean(a.display_name)}-vs-{clean(b.display_name)}"[:90]


async def get_or_create_duel_category(guild: discord.Guild) -> discord.CategoryChannel:
    category = discord.utils.get(guild.categories, name=DUEL_CATEGORY_NAME)
    if category is None:
        category = await guild.create_category(DUEL_CATEGORY_NAME)
    return category


async def create_duel_channel(guild: discord.Guild, category: discord.CategoryChannel,
                               player_a: discord.Member, player_b: discord.Member) -> discord.TextChannel:
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
        player_a: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        player_b: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    return await guild.create_text_channel(
        name=_slugify_channel_name(player_a, player_b),
        category=category,
        overwrites=overwrites,
        reason=f"Duel channel for {player_a} vs {player_b}",
    )


# ---------------------------------------------------------------------------
# Board rendering
# ---------------------------------------------------------------------------

async def get_avatar_bytes(member: discord.Member, avatar_cache: dict[int, bytes]) -> Optional[bytes]:
    """Fetches and caches a member's current avatar (or Discord's default one if they haven't
    set one) as raw image bytes. Returns None on any failure so the caller can fall back to the
    placeholder circle rather than breaking the board render."""
    cached = avatar_cache.get(member.id)
    if cached is not None:
        return cached
    try:
        data = await member.display_avatar.read()
    except discord.HTTPException:
        return None
    avatar_cache[member.id] = data
    return data


async def render_board_file(match, members: dict[int, discord.Member], avatar_cache: dict[int, bytes],
                             winner_name: str | None = None) -> discord.File:
    """Builds the visual battle board (see render/board.py) as a fresh discord.File attachment."""
    player_ids = list(match.players.keys())
    left_id, right_id = player_ids[0], player_ids[1]

    async def to_render_state(uid: int) -> PlayerRenderState:
        p = match.players[uid]
        avatar_bytes = await get_avatar_bytes(members[uid], avatar_cache)
        last_discard = p.discard[-1] if p.discard else None
        return PlayerRenderState(
            name=members[uid].display_name,
            hp=p.hp, max_hp=STARTING_HP,
            mana=p.mana, max_mana=MAX_MANA,
            hand_count=len(p.hand), deck_count=len(p.deck), discard_count=len(p.discard),
            last_discard_card_id=(last_discard.template_id or last_discard.id) if last_discard else None,
            avatar_bytes=avatar_bytes,
        )

    attacker_id = match.active_player_id
    defender_id = match.opponent_of(attacker_id)

    played_card_id = None
    played_card_caption = None
    if match.awaiting_reaction:
        played_card_id = match.awaiting_reaction.template_id
        played_card_caption = f"{members[match.awaiting_reaction.attacker_id].display_name} attacks"
    elif match.last_played:
        played_card_id = match.last_played.template_id
        verb = "attacked with" if match.last_played.role == "active" else "blocked with"
        played_card_caption = f"{members[match.last_played.player_id].display_name} {verb}"

    state = BattleRenderState(
        turn_number=match.turn_number,
        left=await to_render_state(left_id),
        right=await to_render_state(right_id),
        attacker_name=members[attacker_id].display_name,
        defender_name=members[defender_id].display_name,
        played_card_id=played_card_id,
        played_card_caption=played_card_caption,
        winner_name=winner_name,
    )
    png_bytes = await asyncio.to_thread(render_battle_image, state)
    return discord.File(io.BytesIO(png_bytes), filename="battle.png")


# ---------------------------------------------------------------------------
# Deckbuilding
# ---------------------------------------------------------------------------

class DraftSession:
    """Tracks a pending duel while both players are still picking their decks."""

    def __init__(self, player_a_id: int, player_b_id: int, public_message: discord.Message,
                 members: dict[int, discord.Member], channel: discord.TextChannel):
        self.player_a_id = player_a_id
        self.player_b_id = player_b_id
        self.public_message = public_message
        self.members = members
        self.channel = channel
        self.decks: dict[int, list[Card]] = {}

    @property
    def ready(self) -> bool:
        return self.player_a_id in self.decks and self.player_b_id in self.decks

    def other(self, user_id: int) -> int:
        return self.player_b_id if user_id == self.player_a_id else self.player_a_id


class BuildDeckPromptView(discord.ui.View):
    """
    Posted once both players have accepted. A single shared button either
    player can click -- clicking opens YOUR OWN private deckbuilder,
    regardless of who clicks it.
    """

    def __init__(self, cog: "Duel", draft_id: str, player_a_id: int, player_b_id: int):
        super().__init__(timeout=DECKBUILD_TIMEOUT_SECONDS)
        self.cog = cog
        self.draft_id = draft_id
        self.participants = {player_a_id, player_b_id}

    async def on_timeout(self):
        session = self.cog.drafts.pop(self.draft_id, None)
        if session:
            try:
                await session.public_message.edit(
                    content="Deckbuilding timed out. This channel will be deleted shortly.", view=None,
                )
            except discord.HTTPException:
                pass
            _schedule_channel_deletion(session.channel, CHANNEL_CLEANUP_DELAY_SECONDS)

    @discord.ui.button(label="Build My Deck", style=discord.ButtonStyle.primary)
    async def build(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.participants:
            await interaction.response.send_message("This isn't your duel.", ephemeral=True)
            return
        session = self.cog.drafts.get(self.draft_id)
        if session is None:
            await interaction.response.send_message("This duel is no longer active.", ephemeral=True)
            return
        if interaction.user.id in session.decks:
            await interaction.response.send_message(
                "You've already locked in your deck. Waiting on your opponent.", ephemeral=True,
            )
            return

        deckbuilder = DeckBuilderView(self.cog, self.draft_id, interaction.user.id)
        await interaction.response.send_message(
            f"Build your deck ({DECK_SIZE} cards):", view=deckbuilder, ephemeral=True,
        )


class DeckBuilderView(discord.ui.View):
    """
    Ephemeral. Shows one color category at a time (Discord caps a message
    at 5 component rows, and 5 simultaneous 10-option selects would leave
    no room for Confirm/navigation) with Prev/Next tabs. Picks persist
    across category switches and rebuild the view (with `default=True` on
    already-picked options) so the UI reflects running state.
    """

    def __init__(self, cog: "Duel", draft_id: str, user_id: int,
                 picked: set[str] | None = None, category_index: int = 0):
        super().__init__(timeout=DECKBUILD_TIMEOUT_SECONDS)
        self.cog = cog
        self.draft_id = draft_id
        self.user_id = user_id
        self.picked: set[str] = set(picked or set())
        self.category_index = category_index

        color = CATEGORIES[self.category_index]
        cards_here = cog.cards_by_color[color]
        self.current_category_ids = {c.id for c in cards_here}

        select = discord.ui.Select(
            custom_id=f"deck_select_{color}",
            placeholder=f"{CATEGORY_EMOJI[color]} {color} cards",
            min_values=0,
            max_values=len(cards_here),
            options=[
                discord.SelectOption(
                    label=c.name,
                    description=f"{describe_active_compact(c)}  {describe_passive_compact(c)}",
                    value=c.id,
                    default=c.id in self.picked,
                )
                for c in cards_here
            ],
        )
        select.callback = self._on_pick
        self.add_item(select)

        prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary,
                                      disabled=self.category_index == 0)
        prev_btn.callback = self._on_prev
        self.add_item(prev_btn)

        next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary,
                                      disabled=self.category_index == len(CATEGORIES) - 1)
        next_btn.callback = self._on_next
        self.add_item(next_btn)

        confirm_btn = discord.ui.Button(
            label=f"Confirm Deck ({len(self.picked)}/{DECK_SIZE})",
            style=discord.ButtonStyle.success,
            disabled=len(self.picked) != DECK_SIZE,
        )
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

    def _status_text(self) -> str:
        color = CATEGORIES[self.category_index]
        status = f"{CATEGORY_EMOJI[color]} Browsing **{color}**. Selected **{len(self.picked)}/{DECK_SIZE}** total."
        if len(self.picked) > DECK_SIZE:
            status += " ⚠️ That's too many — deselect some before confirming."
        return status

    async def _on_pick(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your deck to build.", ephemeral=True)
            return

        # This select only reports ITS OWN current selection. Replace this
        # category's slice of `picked` with what it just reported, leaving
        # picks from other (currently hidden) categories untouched.
        chosen_here = set(interaction.data.get("values", []))
        self.picked -= self.current_category_ids
        self.picked |= chosen_here

        new_view = DeckBuilderView(self.cog, self.draft_id, self.user_id,
                                    picked=self.picked, category_index=self.category_index)
        await interaction.response.edit_message(content=new_view._status_text(), view=new_view)

    async def _on_prev(self, interaction: discord.Interaction):
        await self._change_page(interaction, -1)

    async def _on_next(self, interaction: discord.Interaction):
        await self._change_page(interaction, 1)

    async def _change_page(self, interaction: discord.Interaction, delta: int):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your deck to build.", ephemeral=True)
            return
        new_index = max(0, min(len(CATEGORIES) - 1, self.category_index + delta))
        new_view = DeckBuilderView(self.cog, self.draft_id, self.user_id,
                                    picked=self.picked, category_index=new_index)
        await interaction.response.edit_message(content=new_view._status_text(), view=new_view)

    async def _on_confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your deck to build.", ephemeral=True)
            return
        if len(self.picked) != DECK_SIZE:
            await interaction.response.send_message(
                f"You need exactly {DECK_SIZE} cards (currently {len(self.picked)}).", ephemeral=True,
            )
            return

        deck = [self.cog.pool_by_id[card_id] for card_id in self.picked]
        await interaction.response.edit_message(
            content="✅ Deck locked in! Waiting for your opponent to finish building theirs...", view=None,
        )
        await self.cog.on_deck_confirmed(self.draft_id, self.user_id, deck)


# ---------------------------------------------------------------------------
# In-match UI
# ---------------------------------------------------------------------------

class PublicView(discord.ui.View):
    """
    Lives on the board message inside the private duel channel. Never
    shows card names -- only generic action buttons. Attack/Block hand
    off to an ephemeral view so even your opponent (who does share this
    channel) can't see your hand.
    """

    def __init__(self, match, manager: MatchManager, members: dict[int, discord.Member],
                 messages: dict[str, discord.Message], channels: dict[str, discord.TextChannel],
                 avatar_cache: dict[int, bytes]):
        super().__init__(timeout=None)
        self.match = match
        self.manager = manager
        self.members = members
        self.messages = messages
        self.channels = channels
        self.avatar_cache = avatar_cache

        if match.awaiting_reaction:
            self._add_button("Block", discord.ButtonStyle.primary, self._on_block_open)
            self._add_button("Take Damage (+1 Mana)", discord.ButtonStyle.secondary, self._on_take_damage)
        else:
            self._add_button("Attack", discord.ButtonStyle.primary, self._on_attack_open)
            self._add_button("Skip Turn (+1 Mana)", discord.ButtonStyle.secondary, self._on_skip)

    def _add_button(self, label, style, callback):
        btn = discord.ui.Button(label=label, style=style)
        btn.callback = callback
        self.add_item(btn)

    async def _on_attack_open(self, interaction: discord.Interaction):
        if interaction.user.id != self.match.active_player_id:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return
        attacker = self.match.players[interaction.user.id]
        if not attacker.hand:
            await interaction.response.send_message("You have no cards to attack with.", ephemeral=True)
            return
        view = HandSelectView(
            match=self.match, manager=self.manager, members=self.members,
            messages=self.messages, channels=self.channels, avatar_cache=self.avatar_cache, mode="attack",
        )
        await interaction.response.send_message("Choose a card to attack with:", view=view, ephemeral=True)

    async def _on_block_open(self, interaction: discord.Interaction):
        pending = self.match.awaiting_reaction
        if not pending or interaction.user.id != pending.defender_id:
            await interaction.response.send_message("You're not the one reacting.", ephemeral=True)
            return
        defender = self.match.players[interaction.user.id]
        if not defender.hand:
            await interaction.response.send_message("You have no cards to block with.", ephemeral=True)
            return
        view = HandSelectView(
            match=self.match, manager=self.manager, members=self.members,
            messages=self.messages, channels=self.channels, avatar_cache=self.avatar_cache, mode="block",
        )
        await interaction.response.send_message("Choose a card to block with:", view=view, ephemeral=True)

    async def _on_skip(self, interaction: discord.Interaction):
        if interaction.user.id != self.match.active_player_id:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return
        result = self.match.skip(interaction.user.id)
        await refresh_public_message(interaction, self.match, self.manager, self.members,
                                      self.messages, self.channels, self.avatar_cache, result)

    async def _on_take_damage(self, interaction: discord.Interaction):
        pending = self.match.awaiting_reaction
        if not pending or interaction.user.id != pending.defender_id:
            await interaction.response.send_message("You're not the one reacting.", ephemeral=True)
            return
        result = self.match.react(interaction.user.id, None)
        await refresh_public_message(interaction, self.match, self.manager, self.members,
                                      self.messages, self.channels, self.avatar_cache, result)


class HandSelectView(discord.ui.View):
    """
    Ephemeral -- only the acting player ever sees this. Lists their real
    hand with real stats, since Discord guarantees ephemeral replies are
    private to the invoking user (even inside a channel your opponent
    can also see).
    """

    def __init__(self, match, manager: MatchManager, members: dict[int, discord.Member],
                 messages: dict[str, discord.Message], channels: dict[str, discord.TextChannel],
                 avatar_cache: dict[int, bytes], mode: str):
        super().__init__(timeout=EPHEMERAL_TIMEOUT_SECONDS)
        self.match = match
        self.manager = manager
        self.members = members
        self.messages = messages
        self.channels = channels
        self.avatar_cache = avatar_cache
        self.mode = mode  # "attack" or "block"

        if mode == "attack":
            hand = match.players[match.active_player_id].hand
            options = [
                discord.SelectOption(label=c.name, description=describe_active_full(c), value=c.id)
                for c in hand
            ]
        else:
            hand = match.players[match.awaiting_reaction.defender_id].hand
            options = [
                discord.SelectOption(label=c.name, description=describe_passive_full(c), value=c.id)
                for c in hand
            ]

        select = discord.ui.Select(placeholder="Pick a card...", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        card_id = interaction.data["values"][0]
        if self.mode == "attack":
            result = self.match.attack(interaction.user.id, card_id)
        else:
            result = self.match.react(interaction.user.id, card_id)

        if not result.success:
            await interaction.response.edit_message(content=result.message, view=None)
            return

        await interaction.response.edit_message(content=f"✅ {result.message}", view=None)
        await update_public_message(self.match, self.manager, self.members, self.messages,
                                     self.channels, self.avatar_cache, result)


async def refresh_public_message(interaction: discord.Interaction, match, manager, members, messages, channels,
                                  avatar_cache, result):
    """For actions (Skip/Take Damage) that happen directly on the public message itself."""
    if not result.success:
        await interaction.response.send_message(result.message, ephemeral=True)
        return

    if result.game_over:
        winner_name = members[result.winner_id].display_name
        file = await render_board_file(match, members, avatar_cache, winner_name=winner_name)
        manager.end_match(match.match_id)
        messages.pop(match.match_id, None)
        await interaction.response.edit_message(content=result.message, embed=None, attachments=[file], view=None)
        channel = channels.pop(match.match_id, None)
        if channel:
            await interaction.followup.send("This channel will be deleted in 10 minutes. GG!")
            _schedule_channel_deletion(channel, BATTLE_END_CLEANUP_DELAY_SECONDS)
        return

    file = await render_board_file(match, members, avatar_cache)
    new_view = PublicView(match, manager, members, messages, channels, avatar_cache)
    await interaction.response.edit_message(content=result.message, embed=None, attachments=[file], view=new_view)


async def update_public_message(match, manager, members, messages, channels, avatar_cache, result):
    """For actions (Attack/Block/Forfeit) resolved off the public message -- edit the stored one."""
    public_message = messages.get(match.match_id)
    if public_message is None or not members:
        return  # state reference lost (e.g. bot restarted) -- public board just won't live-update

    if result.game_over:
        winner_name = members[result.winner_id].display_name
        file = await render_board_file(match, members, avatar_cache, winner_name=winner_name)
        manager.end_match(match.match_id)
        messages.pop(match.match_id, None)
        await public_message.edit(content=result.message, embed=None, attachments=[file], view=None)
        channel = channels.pop(match.match_id, None)
        if channel:
            await channel.send("This channel will be deleted in 10 minutes. GG!")
            _schedule_channel_deletion(channel, BATTLE_END_CLEANUP_DELAY_SECONDS)
        return

    file = await render_board_file(match, members, avatar_cache)
    new_view = PublicView(match, manager, members, messages, channels, avatar_cache)
    await public_message.edit(content=result.message, embed=None, attachments=[file], view=new_view)


# ---------------------------------------------------------------------------
# Challenge flow
# ---------------------------------------------------------------------------

class ChallengeView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, cog: "Duel", draft_id: str):
        super().__init__(timeout=CHALLENGE_TIMEOUT_SECONDS)
        self.challenger = challenger
        self.opponent = opponent
        self.cog = cog
        self.draft_id = draft_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("This challenge isn't addressed to you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        session = self.cog.drafts.pop(self.draft_id, None)
        if session:
            try:
                await session.public_message.edit(
                    content="Challenge timed out. This channel will be deleted shortly.", embed=None, view=None,
                )
            except discord.HTTPException:
                pass
            _schedule_channel_deletion(session.channel, CHANNEL_CLEANUP_DELAY_SECONDS)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"Duel accepted! Both {self.challenger.mention} and {self.opponent.mention} "
                    f"can now build their decks — click below (10 cards each).",
            embed=None,
            view=BuildDeckPromptView(self.cog, self.draft_id, self.challenger.id, self.opponent.id),
        )
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = self.cog.drafts.pop(self.draft_id, None)
        await interaction.response.edit_message(
            content="Challenge declined. This channel will be deleted shortly.", embed=None, view=None,
        )
        if session:
            _schedule_channel_deletion(session.channel, CHANNEL_CLEANUP_DELAY_SECONDS)
        self.stop()


class Duel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.manager = MatchManager()
        self.card_pool = load_card_pool()
        self.pool_by_id: dict[str, Card] = {c.id: c for c in self.card_pool}
        self.cards_by_color: dict[str, list[Card]] = {color: [] for color in CATEGORIES}
        for card in self.card_pool:
            self.cards_by_color[card.color].append(card)
        self.messages: dict[str, discord.Message] = {}          # match_id -> live board message
        self.match_members: dict[str, dict[int, discord.Member]] = {}  # match_id -> {user_id: Member}
        self.channels: dict[str, discord.TextChannel] = {}       # match_id -> the duel's private channel
        self.drafts: dict[str, DraftSession] = {}                # draft_id -> in-progress deckbuilding session
        self.avatar_cache: dict[int, bytes] = {}                 # user_id -> their avatar image bytes

    async def on_deck_confirmed(self, draft_id: str, user_id: int, deck: list[Card]):
        session = self.drafts.get(draft_id)
        if session is None:
            return  # duel was declined/timed out/already started -- nothing to do

        session.decks[user_id] = deck

        if not session.ready:
            waiting_on = session.members[session.other(user_id)]
            ready_player = session.members[user_id]
            await session.public_message.edit(
                content=f"{ready_player.mention} has locked in their deck! Waiting on {waiting_on.mention}...",
            )
            return

        match = self.manager.create_match(session.player_a_id, session.player_b_id, session.decks)
        self.messages[match.match_id] = session.public_message
        self.match_members[match.match_id] = session.members
        self.channels[match.match_id] = session.channel
        self.drafts.pop(draft_id, None)

        file = await render_board_file(match, session.members, self.avatar_cache)
        await session.public_message.edit(
            content=f"Both decks are locked in! {session.members[match.active_player_id].mention} goes first.",
            embed=None, attachments=[file],
            view=PublicView(match, self.manager, session.members, self.messages, self.channels, self.avatar_cache),
        )

    async def cleanup_all_channels(self):
        """
        Best-effort cleanup for a CLEAN shutdown (one where close() actually
        gets to run) -- deletes everything currently tracked in memory.
        This is a fast path, not the safety net: see sweep_orphaned_channels
        for what handles crashes, forced kills, or a Ctrl+C that doesn't let
        this coroutine finish (a known flaky spot on Windows + asyncio).
        """
        channels_to_delete = list(self.channels.values())
        channels_to_delete += [session.channel for session in self.drafts.values()]

        for channel in channels_to_delete:
            _mark_channel_orphaned(channel.id)  # in case delete() below doesn't get to finish
            await _delete_channel_and_unregister(channel)

    async def sweep_orphaned_channels(self):
        """
        Called once on startup (after the bot is connected). Deletes every
        channel currently flagged 'orphaned' in the database -- this is what
        actually fixes channels surviving an unclean shutdown, since it
        doesn't depend on any shutdown code having run at all. Channels
        still flagged 'active' are deliberately left alone here; only ones
        explicitly marked for deletion (decline/timeout/game-over) count as
        orphaned. A channel that fails to delete (permissions, a transient
        API error) stays tracked for the next sweep to retry, instead of
        being forgotten.
        """
        orphaned_ids = _load_orphaned_channel_ids()
        if not orphaned_ids:
            return

        deleted = 0
        for channel_id in orphaned_ids:
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            except discord.NotFound:
                _unregister_channel_id(channel_id)
                continue
            except discord.HTTPException:
                continue  # transient fetch failure -- leave tracked, retry on the next startup

            if await _delete_channel_and_unregister(channel):
                deleted += 1

        if deleted:
            print(f"[duel] Swept {deleted} orphaned duel channel(s) from a previous session.")

    @app_commands.command(name="duel", description="Challenge another player to a card duel in a private channel.")
    async def duel(self, interaction: discord.Interaction, opponent: discord.Member):
        if not interaction.guild:
            await interaction.response.send_message("Duels can only be started in a server.", ephemeral=True)
            return
        if opponent.bot:
            await interaction.response.send_message("You can't duel a bot.", ephemeral=True)
            return
        if opponent.id == interaction.user.id:
            await interaction.response.send_message("You can't duel yourself.", ephemeral=True)
            return
        if self.manager.get_match_for_user(interaction.user.id):
            await interaction.response.send_message("You're already in a match.", ephemeral=True)
            return
        if self.manager.get_match_for_user(opponent.id):
            await interaction.response.send_message(f"{opponent.display_name} is already in a match.", ephemeral=True)
            return
        if not interaction.guild.me.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "I need the **Manage Channels** permission in this server to create a private duel channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)  # channel creation can take a moment

        category = await get_or_create_duel_category(interaction.guild)
        channel = await create_duel_channel(interaction.guild, category, interaction.user, opponent)
        _register_channel_id(channel.id)

        draft_id = str(uuid.uuid4())
        challenge_message = await channel.send(
            f"{opponent.mention}, you've been challenged to a duel by {interaction.user.mention}!",
            view=ChallengeView(interaction.user, opponent, self, draft_id),
        )
        members = {interaction.user.id: interaction.user, opponent.id: opponent}
        self.drafts[draft_id] = DraftSession(interaction.user.id, opponent.id, challenge_message, members, channel)

        await interaction.followup.send(f"Duel channel created: {channel.mention}", ephemeral=True)

    @app_commands.command(name="forfeit", description="Forfeit your current match.")
    async def forfeit(self, interaction: discord.Interaction):
        match = self.manager.get_match_for_user(interaction.user.id)
        if not match:
            await interaction.response.send_message("You're not in a match.", ephemeral=True)
            return

        result = match.forfeit(interaction.user.id)
        if not result.success:
            await interaction.response.send_message(result.message, ephemeral=True)
            return

        members = self.match_members.get(match.match_id, {})
        await interaction.response.send_message(result.message)
        await update_public_message(match, self.manager, members, self.messages, self.channels,
                                     self.avatar_cache, result)
        self.match_members.pop(match.match_id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(Duel(bot))
