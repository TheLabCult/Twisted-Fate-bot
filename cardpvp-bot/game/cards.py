import json
from pathlib import Path

from game.engine import ActiveCondition, ActiveEffect, Card, PassiveEffect

CARDS_PATH = Path(__file__).parent.parent / "data" / "cards.json"


def load_card_pool() -> list[Card]:
    """The full pool every player picks their 10-card deck from."""
    with open(CARDS_PATH) as f:
        raw = json.load(f)

    cards = []
    for entry in raw:
        entry = dict(entry)
        entry.pop("custom_art", None)  # build-time flag for scripts/generate_assets.py, not a Card field
        entry["passive_effect"] = PassiveEffect(entry.get("passive_effect", "block"))
        entry["active_condition"] = ActiveCondition(entry.get("active_condition", "none"))
        entry["active_effect"] = ActiveEffect(entry.get("active_effect", "damage"))
        cards.append(Card(**entry))
    return cards