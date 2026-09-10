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
│   ├── cards/                    # one placeholder PNG per card, keyed by
│   │                              # card id (e.g. fireball.png), + _blank.png
│   └── board/
│       └── hex_background.png    # pre-rendered board background
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

## Regenerating card art / the board background

`assets/` is pre-generated, not built at runtime — the bot just loads
whatever PNGs are sitting there. If you edit `data/cards.json` (add cards,
rename them, recolor them), regenerate the matching art:

```bash
python scripts/generate_assets.py
```

This is placeholder art (flat color + shape + text baked in from each
card's stats) — swap in real illustrations later by replacing the PNGs in
`assets/cards/` with same-named files; `render/board.py` doesn't care how
they were made, only that a file exists at `assets/cards/<card_id>.png`.

## What's implemented

- Private per-duel channel under a "Duels" category, visible only to the
  two players; auto-deleted 10 minutes after a match ends (or immediately,
  for declined/timed-out challenges), and swept on bot shutdown too.
- Deckbuilding: pick exactly 10 of 50 cards, browsable by color (Red /
  Blue / Green / Yellow / Purple), via a private ephemeral menu.
- Turn-based combat: attack (pay mana, deal damage) → opponent reacts
  (block / take it) → turn passes. Hands are private even though the
  channel itself is shared with your opponent.
- Four passive-ability archetypes: plain block, full negate vs. one
  specific color, bonus block vs. one specific color, and reflect
  (redirects damage back at the attacker, which can kill them).
- A rendered battle board (HP hearts, mana stars, avatar placeholders,
  Attacker/Defender labels, the currently-in-flight card's art) instead of
  a text embed, regenerated as a fresh PNG on every action.
- `/forfeit`.

## What's still stubbed out / natural next steps

- **Real card art** — currently generated placeholders; see the
  regeneration section above for how to swap in real illustrations.
- **Real player avatars** — the board uses generic colored circles with
  initials rather than each player's actual Discord avatar. Fetchable via
  `member.display_avatar`, just not wired in yet.
- **Persistence** — everything (matches, drafts, message/channel
  references) lives in memory and resets if the bot restarts mid-game.
- **Win/loss records, matchmaking queue, ELO** — not started.
