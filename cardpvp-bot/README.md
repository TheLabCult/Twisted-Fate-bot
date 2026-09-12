# Card PvP Discord Bot

`/duel @opponent` creates a private channel just for the two of you, both
players build a 10-card deck from a 50-card pool (5 colors), then play a
turn-based attack/block duel rendered as a generated battle-board image.

## Project layout

```
cardpvp-bot/
├── main.py                  # entry point, loads cogs, syncs slash commands,
│                             # cleans up duel channels on shutdown
├── cogs/
│   └── duel.py                # Discord-facing layer: channels, deckbuilding
│                               # UI, in-match buttons, board rendering glue
├── game/
│   ├── engine.py                # core game rules & state machine
│   │                             # (NO discord imports -- pure Python)
│   └── cards.py                  # loads data/cards.json into Card objects
├── render/
│   └── board.py                # composites the battle-board PNG from
│                                # assets/ (NO discord import either)
├── scripts/
│   └── generate_assets.py      # dev tool: (re)generates everything in assets/
├── assets/
│   ├── cards/                    # one card art PNG per card, keyed by card
│   │                              # id (e.g. fireball.png), + _blank.png
│   ├── board/
│   │   └── hex_background.png    # pre-rendered board background
│   └── icons/
│       ├── heart_1.png .. heart_5.png    # HP indicator, full -> empty
│       └── potion_1.png .. potion_5.png  # Mana indicator, full -> empty
├── data/
│   └── cards.json           # card definitions -- edit this to add/balance cards
├── requirements.txt
└── .env.example
```

## Design principle: rules, rendering, and Discord are three separate layers

- **`game/engine.py`** is a plain Python state machine — `Match.attack()`,
  `Match.react()`, `Match.skip()`, `Match.forfeit()` — returning
  `ActionResult` objects. It doesn't know Discord or Pillow exist.
- **`render/board.py`** takes plain dataclasses in (`BattleRenderState`) and
  returns PNG bytes out. It doesn't know Discord exists either.
- **`cogs/duel.py`** is the only file that imports `discord` — it bridges
  match state into render state, and render output into Discord messages.

This means the whole game (rules + visuals) can be tested without ever
starting a bot connection — see the engine/render test snippets in this
project's history for examples.

## Setup

1. Create a Discord application + bot user at
   https://discord.com/developers/applications, enable it, and copy the
   bot token.
2. Invite it to your test server with the `bot` and `applications.commands`
   scopes. Required permissions: **Manage Channels** (it creates a private
   channel per duel), Send Messages, Embed Links, Attach Files.
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN` (and
   `DEV_GUILD_ID` for instant command sync on your test server while
   developing).
5. Run it:
   ```bash
   python main.py
   ```
6. In Discord: `/duel @someone`. A private channel is created; both players
   accept, build a 10-card deck (paged by color), then play.

## Card art

All 50 cards use real artwork (`assets/cards/<card_id>.png`), each flagged
`"custom_art": true` in `data/cards.json` so `scripts/generate_assets.py`
won't overwrite it. If you add a *new* card without commissioned art yet,
leave that flag off and run:

```bash
python scripts/generate_assets.py
```

This generates simple placeholder art (flat color + shape + text baked in
from the card's stats) for any card missing `custom_art`, plus the board
background. Swap in real art later by dropping a same-named PNG into
`assets/cards/` and setting the flag — `render/board.py` doesn't care how
the file was made, only that it exists.

## HP / Mana icons

`assets/icons/` holds 5 fill-level sprites each for the heart (HP) and
potion (Mana) indicators — `heart_1.png`/`potion_1.png` are fullest,
`heart_5.png`/`potion_5.png` are emptiest. These are real sprites, not
generated. `render/board.py` picks whichever level is closest to the
player's actual HP/Mana fraction (see `HEART_LEVELS`/`POTION_LEVELS`) --
it's a discrete 5-step snap, not a continuous fill. To use a different
icon set, replace these 10 files (keeping the same names and full-to-empty
ordering) or adjust the threshold tables if you have a different number
of levels.

## What's implemented

- Private per-duel channel under a "Duels" category, visible only to the
  two players; auto-deleted 10 minutes after a match ends (or quickly, for
  declined/timed-out challenges). Which channels are pending deletion is
  tracked in `runtime/duel_channels.db` (SQLite), so a channel whose delete
  never got to run (crash, forced kill, a flaky shutdown) still gets swept
  up the next time the bot starts — the sweep isn't a best-effort shutdown
  hook, it's the actual mechanism, and it retries anything that fails
  rather than losing track of it.
- Deckbuilding: pick exactly 10 of 50 cards, browsable by color (Red /
  Blue / Green / Yellow / Purple), via a private ephemeral menu.
- Turn-based combat: attack (pay mana, deal damage) → opponent reacts
  (block / take it) → turn passes. Hands are private even though the
  channel itself is shared with your opponent.
- Five passive-ability archetypes (plain block, full negate vs. one
  color, bonus block vs. one color, unconditional reflect, and reflect
  vs. one color) and five active-ability archetypes (plain damage,
  conditional bonus damage, mana swap, HP swap, and conditional
  heal/mana-gain) — see `game/engine.py`'s `PassiveEffect`/`ActiveEffect`
  enums.
- A rendered battle board — real card art, real player avatars (circle-
  cropped from Discord, cached per match), sprite-based HP/Mana indicators
  (`assets/icons/`), Attacker/Defender labels, the currently-in-flight
  card — instead of a text embed, regenerated as a fresh PNG on every
  action. Pillow rendering runs in a background thread so it can't block
  other players' interactions while it draws.
- `/forfeit`.

## What's still stubbed out / natural next steps

- **Match state doesn't survive a restart** — `MatchManager`, draft
  sessions, and the board message/channel references are all in-memory.
  If the bot restarts mid-game, the players' buttons go dead (the channel
  itself still gets cleaned up correctly via the DB-backed sweep above,
  just the match itself is lost). Persisting match state to the same
  SQLite file would be the natural next step.
- **Win/loss records, matchmaking queue, ELO** — not started.
