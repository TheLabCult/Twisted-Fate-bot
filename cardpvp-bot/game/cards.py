import json
from pathlib import Path

from game.engine import Card, PassiveEffect

CARDS_PATH = Path(__file__).parent.parent / "data" / "cards.json"


def load_card_pool() -> list[Card]:
    """The full pool every player picks their 10-card deck from."""
    with open(CARDS_PATH) as f:
        raw = json.load(f)

    cards = []
    for entry in raw:
        entry = dict(entry)
        entry["passive_effect"] = PassiveEffect(entry.get("passive_effect", "block"))
        cards.append(Card(**entry))
    return cards
