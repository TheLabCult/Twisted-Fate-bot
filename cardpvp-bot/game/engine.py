"""
Core PvP card game engine implementing the reactive attack/block system.

Rules encoded here:
- 10 HP, 5 mana to start. Mana caps at 5 (regen never exceeds the cap).
- 10-card deck, shuffled. Draw 3 to open, then top back up to 3 at the
  end of every turn. When a deck runs dry, its discard pile is shuffled
  back in (HP/mana carry over untouched -- no "fatigue" damage).
- On your turn you either:
    - Attack: play a card's ACTIVE ability (pay its active_cost, deal
      active_damage), which is discarded, OR
    - Skip: gain 1 mana (capped) and do nothing else.
- If you attack, the opponent gets a reaction window before damage lands:
    - Block: play a card's PASSIVE ability (pay its passive_cost), which
      is discarded, reducing incoming damage by passive_block (min 0), OR
    - Take it: gain 1 mana (capped) and take full damage.
- Turn then passes to the other player.

Deliberately knows nothing about Discord -- pure state machine, so it can
be unit tested (and was, below) without a bot connection.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

STARTING_HP = 10
STARTING_MANA = 5
MAX_MANA = 5
HAND_SIZE = 3
DECK_SIZE = 10


class GamePhase(Enum):
    IN_PROGRESS = auto()
    FINISHED = auto()


class PassiveEffect(Enum):
    BLOCK = "block"                          # reduce incoming damage by passive_block (the default)
    NEGATE_COLOR = "negate_color"            # fully negate damage IF the attacking card's color == condition_color;
                                              # otherwise falls back to a normal BLOCK using passive_block
    BLOCK_BONUS_COLOR = "block_bonus_color"  # blocks passive_block normally, but blocks
                                              # passive_block + passive_bonus_block IF the attacking
                                              # card's color == condition_color
    REFLECT = "reflect"                      # redirect the full incoming damage back onto the attacker instead,
                                              # unconditionally
    REFLECT_COLOR = "reflect_color"          # redirect the full incoming damage back onto the attacker IF the
                                              # attacking card's color == condition_color; otherwise falls back
                                              # to a normal BLOCK using passive_block


class ActiveCondition(Enum):
    NONE = "none"
    LOWER_HP = "lower_hp"                        # bonus if the attacker currently has less HP than the defender
    LAST_DISCARD_COLOR = "last_discard_color"    # bonus if the attacker's own most recent discard matches
                                                  # active_condition_color
    HAND_ALL_COLOR = "hand_all_color"            # bonus if every card in the attacker's hand (including the
                                                  # one being played) matches active_condition_color


class ActiveEffect(Enum):
    DAMAGE = "damage"                            # the default: deals active_damage (+ conditional bonus per
                                                  # ActiveCondition) to the opponent, who gets a reaction window
    SWAP_MANA = "swap_mana"                      # unconditionally swaps current mana pools with the opponent
    SWAP_HP = "swap_hp"                          # unconditionally swaps current HP pools with the opponent
    HEAL_IF_COLOR = "heal_if_color"              # heals active_heal_amount to self IF active_condition is met
                                                  # (only LAST_DISCARD_COLOR makes sense here); otherwise no effect
    GAIN_MANA_IF_COLOR = "gain_mana_if_color"    # gains active_mana_gain mana to self IF active_condition is met;
                                                  # otherwise no effect
    # None of these target the opponent with damage, so playing one does NOT
    # open a reaction window -- the turn resolves and passes immediately,
    # the same way a Skip does.


@dataclass
class Card:
    id: str
    name: str
    active_cost: int
    active_damage: int
    passive_cost: int
    passive_block: int
    color: str = ""
    passive_effect: PassiveEffect = PassiveEffect.BLOCK
    passive_condition_color: str = ""  # meaningful for NEGATE_COLOR, BLOCK_BONUS_COLOR, and REFLECT_COLOR
    passive_bonus_block: int = 0       # meaningful for BLOCK_BONUS_COLOR only
    active_condition: ActiveCondition = ActiveCondition.NONE
    active_condition_color: str = ""   # meaningful for LAST_DISCARD_COLOR and HAND_ALL_COLOR
    active_bonus_damage: int = 0       # extra damage dealt if active_condition is met (DAMAGE effect only)
    active_effect: ActiveEffect = ActiveEffect.DAMAGE
    active_heal_amount: int = 0        # meaningful for HEAL_IF_COLOR only
    active_mana_gain: int = 0          # meaningful for GAIN_MANA_IF_COLOR only
    template_id: str = ""              # stable id for asset lookup (e.g. "fireball") -- unlike
                                        # `id`, this survives clone() so a played card's art can
                                        # still be found even though its instance id is a fresh uuid
    text: str = ""

    def clone(self) -> "Card":
        # each copy in a deck/hand needs its own identity so it can be
        # tracked individually (e.g. two copies of the same card in hand)
        return Card(
            id=str(uuid.uuid4()),
            name=self.name,
            active_cost=self.active_cost,
            active_damage=self.active_damage,
            passive_cost=self.passive_cost,
            passive_block=self.passive_block,
            color=self.color,
            passive_effect=self.passive_effect,
            passive_condition_color=self.passive_condition_color,
            passive_bonus_block=self.passive_bonus_block,
            active_condition=self.active_condition,
            active_condition_color=self.active_condition_color,
            active_bonus_damage=self.active_bonus_damage,
            active_effect=self.active_effect,
            active_heal_amount=self.active_heal_amount,
            active_mana_gain=self.active_mana_gain,
            template_id=self.template_id or self.id,
            text=self.text,
        )


@dataclass
class PlayerState:
    user_id: int
    deck: list[Card] = field(default_factory=list)
    hand: list[Card] = field(default_factory=list)
    discard: list[Card] = field(default_factory=list)
    hp: int = STARTING_HP
    mana: int = STARTING_MANA

    def gain_mana(self, amount: int = 1) -> None:
        self.mana = min(self.mana + amount, MAX_MANA)

    def take_damage(self, amount: int) -> None:
        self.hp = max(0, self.hp - amount)


@dataclass
class ActionResult:
    success: bool
    message: str = ""
    game_over: bool = False
    winner_id: Optional[int] = None


@dataclass
class PendingReaction:
    attacker_id: int
    defender_id: int
    card_name: str
    template_id: str
    color: str
    damage: int
    is_utility: bool = False
    utility_effect: ActiveEffect = ActiveEffect.DAMAGE
    utility_heal_amount: int = 0
    utility_mana_gain: int = 0
    utility_condition_met: bool = False


def passive_has_effect(card: "Card", pending: "PendingReaction") -> bool:
    """
    Whether playing `card`'s passive in reaction to `pending` would actually do
    anything -- used to stop a defender from wasting mana and a card on a
    reaction that provably can't affect the outcome (e.g. a plain BLOCK card
    with passive_block=0, or any non-matching passive against a utility
    effect, which only a matching NEGATE_COLOR can touch at all).
    """
    if pending.is_utility:
        # Only a matching full negate can cancel a utility effect -- plain
        # block/bonus-block/reflect have nothing (no damage) to act on.
        return card.passive_effect == PassiveEffect.NEGATE_COLOR and card.passive_condition_color == pending.color

    if pending.damage <= 0:
        return False  # nothing to block or reflect

    if card.passive_effect == PassiveEffect.BLOCK:
        return card.passive_block > 0
    if card.passive_effect == PassiveEffect.NEGATE_COLOR:
        return pending.color == card.passive_condition_color or card.passive_block > 0
    if card.passive_effect == PassiveEffect.BLOCK_BONUS_COLOR:
        if card.passive_block > 0:
            return True
        return pending.color == card.passive_condition_color and card.passive_bonus_block > 0
    if card.passive_effect == PassiveEffect.REFLECT:
        return True
    if card.passive_effect == PassiveEffect.REFLECT_COLOR:
        return pending.color == card.passive_condition_color or card.passive_block > 0
    return False


@dataclass
class LastPlayed:
    player_id: int
    card_name: str
    template_id: str
    color: str
    role: str  # "active" (attacker's card) or "passive" (defender's block/reflect/negate card)


class Match:
    def __init__(self, player_a_id: int, player_b_id: int, decks: dict[int, list[Card]]):
        self.match_id = str(uuid.uuid4())
        self.phase = GamePhase.IN_PROGRESS
        self.players: dict[int, PlayerState] = {
            player_a_id: PlayerState(user_id=player_a_id, deck=self._shuffled(decks[player_a_id])),
            player_b_id: PlayerState(user_id=player_b_id, deck=self._shuffled(decks[player_b_id])),
        }
        self.turn_order = [player_a_id, player_b_id]
        self.active_player_id = player_a_id
        self.turn_number = 1
        self.awaiting_reaction: Optional[PendingReaction] = None
        self.last_played: Optional[LastPlayed] = None

    @staticmethod
    def _shuffled(cards: list[Card]) -> list[Card]:
        deck = [c.clone() for c in cards]
        random.shuffle(deck)
        return deck

    def start(self) -> None:
        for player in self.players.values():
            self._draw_up_to(player, HAND_SIZE)

    def opponent_of(self, user_id: int) -> int:
        return self._opponent_of(user_id)

    def _opponent_of(self, user_id: int) -> int:
        idx = self.turn_order.index(user_id)
        return self.turn_order[1 - idx]

    def _draw_up_to(self, player: PlayerState, size: int) -> None:
        while len(player.hand) < size:
            if not player.deck:
                if not player.discard:
                    break  # no cards left anywhere -- nothing more to draw
                player.deck = player.discard
                player.discard = []
                random.shuffle(player.deck)
            player.hand.append(player.deck.pop())

    def _end_turn(self, message: str) -> ActionResult:
        for player in self.players.values():
            self._draw_up_to(player, HAND_SIZE)
        self.active_player_id = self._opponent_of(self.active_player_id)
        self.turn_number += 1
        return ActionResult(True, message)

    # -- Player actions --------------------------------------------------

    def attack(self, attacker_id: int, card_id: str) -> ActionResult:
        if self.phase != GamePhase.IN_PROGRESS:
            return ActionResult(False, "The match has already ended.")
        if self.awaiting_reaction:
            return ActionResult(False, "Waiting on a reaction first.")
        if attacker_id != self.active_player_id:
            return ActionResult(False, "It's not your turn.")

        attacker = self.players[attacker_id]
        card = next((c for c in attacker.hand if c.id == card_id), None)
        if card is None:
            return ActionResult(False, "That card isn't in your hand.")
        if card.active_cost > attacker.mana:
            return ActionResult(False, f"Not enough mana ({attacker.mana}/{card.active_cost} needed).")

        defender_id = self._opponent_of(attacker_id)
        defender = self.players[defender_id]

        if card.active_effect != ActiveEffect.DAMAGE:
            return self._declare_utility_active(attacker, defender, card)

        # Conditional bonus damage is checked against state as it stood at
        # the moment of playing -- e.g. "hand is all Red" includes this
        # card itself, and is checked BEFORE it's removed from hand.
        bonus_triggered = False
        if card.active_condition == ActiveCondition.LOWER_HP:
            bonus_triggered = attacker.hp < defender.hp
        elif card.active_condition == ActiveCondition.LAST_DISCARD_COLOR:
            bonus_triggered = bool(attacker.discard) and attacker.discard[-1].color == card.active_condition_color
        elif card.active_condition == ActiveCondition.HAND_ALL_COLOR:
            bonus_triggered = all(c.color == card.active_condition_color for c in attacker.hand)

        damage = card.active_damage + (card.active_bonus_damage if bonus_triggered else 0)

        attacker.mana -= card.active_cost
        attacker.hand.remove(card)
        attacker.discard.append(card)

        self.awaiting_reaction = PendingReaction(
            attacker_id=attacker_id,
            defender_id=defender_id,
            card_name=card.name,
            template_id=card.template_id or card.id,
            color=card.color,
            damage=damage,
        )
        self.last_played = LastPlayed(
            player_id=attacker_id,
            card_name=card.name,
            template_id=card.template_id or card.id,
            color=card.color,
            role="active",
        )
        bonus_note = " (bonus triggered!)" if bonus_triggered and card.active_bonus_damage else ""
        return ActionResult(
            True,
            f"<@{attacker_id}> attacks with **{card.name}** for {damage}{bonus_note}. "
            f"<@{defender_id}> may react.",
        )

    def _declare_utility_active(self, attacker: PlayerState, defender: PlayerState, card: Card) -> ActionResult:
        """
        Handles playing a non-damage active card (mana/HP swap, conditional
        heal, conditional mana gain). These don't hit the opponent with
        damage, but they're still reactable -- a defender whose passive
        NEGATE_COLORs the attacking card's color can cancel the effect
        entirely (same as fully blocking an attack). Declining, or reacting
        with a passive that doesn't match, lets the effect resolve in full.
        """
        # LAST_DISCARD_COLOR is checked against state BEFORE this card joins
        # the discard pile (otherwise it would always match itself).
        condition_met = (
            card.active_condition == ActiveCondition.LAST_DISCARD_COLOR
            and bool(attacker.discard)
            and attacker.discard[-1].color == card.active_condition_color
        )

        attacker.mana -= card.active_cost
        attacker.hand.remove(card)
        attacker.discard.append(card)

        self.awaiting_reaction = PendingReaction(
            attacker_id=attacker.user_id,
            defender_id=defender.user_id,
            card_name=card.name,
            template_id=card.template_id or card.id,
            color=card.color,
            damage=0,
            is_utility=True,
            utility_effect=card.active_effect,
            utility_heal_amount=card.active_heal_amount,
            utility_mana_gain=card.active_mana_gain,
            utility_condition_met=condition_met,
        )
        self.last_played = LastPlayed(
            player_id=attacker.user_id,
            card_name=card.name,
            template_id=card.template_id or card.id,
            color=card.color,
            role="active",
        )
        return ActionResult(
            True,
            f"<@{attacker.user_id}> plays **{card.name}**. <@{defender.user_id}> may react.",
        )

    def _apply_utility_effect(self, attacker: PlayerState, defender: PlayerState, pending: PendingReaction) -> str:
        """Actually applies a utility effect that went unblocked. Returns a short message
        describing what happened."""
        if pending.utility_effect == ActiveEffect.SWAP_MANA:
            attacker.mana, defender.mana = defender.mana, attacker.mana
            return (
                f"**{pending.card_name}** swaps mana! "
                f"Now {attacker.mana}/{MAX_MANA} vs {defender.mana}/{MAX_MANA}."
            )
        if pending.utility_effect == ActiveEffect.SWAP_HP:
            # Safe without a fresh defeat-check: both values were already
            # positive (a match ends the instant either hits 0), so swapping
            # two positive numbers can't newly create a <=0 HP state.
            attacker.hp, defender.hp = defender.hp, attacker.hp
            return f"**{pending.card_name}** swaps HP! Now {attacker.hp} HP vs {defender.hp} HP."
        if pending.utility_effect == ActiveEffect.HEAL_IF_COLOR:
            if pending.utility_condition_met:
                attacker.hp = min(attacker.hp + pending.utility_heal_amount, STARTING_HP)
                return f"**{pending.card_name}** heals <@{attacker.user_id}> to {attacker.hp} HP!"
            return f"**{pending.card_name}**'s heal condition wasn't met -- no effect."
        if pending.utility_effect == ActiveEffect.GAIN_MANA_IF_COLOR:
            if pending.utility_condition_met:
                attacker.gain_mana(pending.utility_mana_gain)
                return f"**{pending.card_name}** grants <@{attacker.user_id}> mana ({attacker.mana}/{MAX_MANA})!"
            return f"**{pending.card_name}**'s mana condition wasn't met -- no effect."
        return f"**{pending.card_name}** resolves."

    def skip(self, user_id: int) -> ActionResult:
        if self.phase != GamePhase.IN_PROGRESS:
            return ActionResult(False, "The match has already ended.")
        if self.awaiting_reaction:
            return ActionResult(False, "Waiting on a reaction first.")
        if user_id != self.active_player_id:
            return ActionResult(False, "It's not your turn.")

        player = self.players[user_id]
        player.gain_mana(1)
        return self._end_turn(f"<@{user_id}> skips, gaining 1 mana ({player.mana}/{MAX_MANA}).")

    def react(self, defender_id: int, card_id: Optional[str]) -> ActionResult:
        """card_id=None means the defender declines to block."""
        if self.phase != GamePhase.IN_PROGRESS:
            return ActionResult(False, "The match has already ended.")
        if not self.awaiting_reaction:
            return ActionResult(False, "There's nothing to react to right now.")
        if defender_id != self.awaiting_reaction.defender_id:
            return ActionResult(False, "You're not the one reacting.")

        pending = self.awaiting_reaction
        defender = self.players[defender_id]
        attacker = self.players[pending.attacker_id]

        if pending.is_utility:
            return self._resolve_utility_reaction(defender_id, card_id)

        damage_to_defender = pending.damage
        damage_to_attacker = 0

        if card_id is not None:
            card = next((c for c in defender.hand if c.id == card_id), None)
            if card is None:
                return ActionResult(False, "That card isn't in your hand.")
            if card.passive_cost > defender.mana:
                return ActionResult(False, f"Not enough mana ({defender.mana}/{card.passive_cost} needed).")
            if not passive_has_effect(card, pending):
                return ActionResult(False, f"**{card.name}** would have no effect here — pick something else or take it.")

            defender.mana -= card.passive_cost
            defender.hand.remove(card)
            defender.discard.append(card)
            self.last_played = LastPlayed(
                player_id=defender_id,
                card_name=card.name,
                template_id=card.template_id or card.id,
                color=card.color,
                role="passive",
            )

            if card.passive_effect == PassiveEffect.NEGATE_COLOR and pending.color == card.passive_condition_color:
                damage_to_defender = 0
                resolution = (
                    f"<@{defender_id}> blocks with **{card.name}** — full negation vs {pending.color}! "
                    f"No damage gets through."
                )
            elif card.passive_effect == PassiveEffect.BLOCK_BONUS_COLOR:
                effective_block = card.passive_block
                if pending.color == card.passive_condition_color:
                    effective_block += card.passive_bonus_block
                    note = f" (bonus block vs {pending.color}!)"
                else:
                    note = ""
                damage_to_defender = max(0, damage_to_defender - effective_block)
                resolution = f"<@{defender_id}> blocks with **{card.name}**{note} — {damage_to_defender} damage gets through."
            elif card.passive_effect == PassiveEffect.REFLECT:
                damage_to_attacker = damage_to_defender
                damage_to_defender = 0
                resolution = (
                    f"<@{defender_id}> reflects **{pending.card_name}** with **{card.name}** — "
                    f"<@{pending.attacker_id}> takes {damage_to_attacker} instead!"
                )
            elif card.passive_effect == PassiveEffect.REFLECT_COLOR and pending.color == card.passive_condition_color:
                damage_to_attacker = damage_to_defender
                damage_to_defender = 0
                resolution = (
                    f"<@{defender_id}> reflects **{pending.card_name}** with **{card.name}** "
                    f"(vs {pending.color}!) — <@{pending.attacker_id}> takes {damage_to_attacker} instead!"
                )
            else:
                # Default BLOCK behavior -- also the fallback for a NEGATE_COLOR or
                # REFLECT_COLOR card used against the "wrong" color.
                damage_to_defender = max(0, damage_to_defender - card.passive_block)
                resolution = f"<@{defender_id}> blocks with **{card.name}** — {damage_to_defender} damage gets through."
        else:
            defender.gain_mana(1)
            resolution = f"<@{defender_id}> takes the hit and gains 1 mana ({defender.mana}/{MAX_MANA})."

        defender.take_damage(damage_to_defender)
        attacker.take_damage(damage_to_attacker)
        self.awaiting_reaction = None

        if attacker.hp <= 0:
            self.phase = GamePhase.FINISHED
            return ActionResult(
                True,
                f"{resolution} <@{pending.attacker_id}> is down to {attacker.hp} HP. <@{defender_id}> wins!",
                game_over=True,
                winner_id=defender_id,
            )

        if defender.hp <= 0:
            self.phase = GamePhase.FINISHED
            return ActionResult(
                True,
                f"{resolution} <@{defender_id}> is down to {defender.hp} HP. <@{pending.attacker_id}> wins!",
                game_over=True,
                winner_id=pending.attacker_id,
            )

        return self._end_turn(resolution)

    def _resolve_utility_reaction(self, defender_id: int, card_id: Optional[str]) -> ActionResult:
        """
        Reaction to a non-damage active play (mana/HP swap, conditional
        heal/mana-gain). Only a NEGATE_COLOR passive that matches the
        attacking card's color can cancel the effect outright -- other
        passive types (plain block, bonus block, reflect) have nothing to
        reduce or redirect since there's no damage involved, so the effect
        still resolves normally if the defender reacts with one of those
        (they've just spent the card and mana for nothing). Declining lets
        it resolve too, with the usual consolation mana.
        """
        pending = self.awaiting_reaction
        defender = self.players[defender_id]
        attacker = self.players[pending.attacker_id]
        negated = False
        declined = card_id is None

        if not declined:
            card = next((c for c in defender.hand if c.id == card_id), None)
            if card is None:
                return ActionResult(False, "That card isn't in your hand.")
            if card.passive_cost > defender.mana:
                return ActionResult(False, f"Not enough mana ({defender.mana}/{card.passive_cost} needed).")
            if not passive_has_effect(card, pending):
                return ActionResult(False, f"**{card.name}** would have no effect here — pick something else or let it happen.")

            defender.mana -= card.passive_cost
            defender.hand.remove(card)
            defender.discard.append(card)
            self.last_played = LastPlayed(
                player_id=defender_id,
                card_name=card.name,
                template_id=card.template_id or card.id,
                color=card.color,
                role="passive",
            )

            if card.passive_effect == PassiveEffect.NEGATE_COLOR and pending.color == card.passive_condition_color:
                negated = True
                resolution = (
                    f"<@{defender_id}> blocks with **{card.name}** — **{pending.card_name}** "
                    f"is negated vs {pending.color}!"
                )
            else:
                resolution = (
                    f"<@{defender_id}> reacts with **{card.name}**, but it has no effect here."
                )
        else:
            resolution = f"<@{defender_id}> lets it happen."

        # Effect resolves using state as it stood at reaction time; any
        # consolation mana for declining is granted AFTER, not before --
        # otherwise a declined SWAP_MANA would swap in its own bonus.
        if not negated:
            resolution = f"{resolution} {self._apply_utility_effect(attacker, defender, pending)}"

        if declined:
            defender.gain_mana(1)
            resolution = f"{resolution} (+1 mana, now {defender.mana}/{MAX_MANA})"

        self.awaiting_reaction = None
        return self._end_turn(resolution)

    def forfeit(self, user_id: int) -> ActionResult:
        if self.phase != GamePhase.IN_PROGRESS:
            return ActionResult(False, "The match has already ended.")
        if user_id not in self.players:
            return ActionResult(False, "You're not part of this match.")

        winner_id = self._opponent_of(user_id)
        self.phase = GamePhase.FINISHED
        self.awaiting_reaction = None
        return ActionResult(
            True,
            f"<@{user_id}> forfeited. <@{winner_id}> wins!",
            game_over=True,
            winner_id=winner_id,
        )


class MatchManager:
    """
    Tracks all active matches in memory, keyed by match_id, plus a lookup
    from user_id -> match_id. Swap for a Redis/DB-backed version later if
    the bot needs to survive restarts mid-match.
    """

    def __init__(self):
        self._matches: dict[str, Match] = {}
        self._user_to_match: dict[int, str] = {}

    def create_match(self, player_a_id: int, player_b_id: int, decks: dict[int, list[Card]]) -> Match:
        match = Match(player_a_id, player_b_id, decks)
        match.start()
        self._matches[match.match_id] = match
        self._user_to_match[player_a_id] = match.match_id
        self._user_to_match[player_b_id] = match.match_id
        return match

    def get_match_for_user(self, user_id: int) -> Optional[Match]:
        match_id = self._user_to_match.get(user_id)
        return self._matches.get(match_id) if match_id else None

    def end_match(self, match_id: str) -> None:
        match = self._matches.pop(match_id, None)
        if match:
            for uid in match.players:
                self._user_to_match.pop(uid, None)