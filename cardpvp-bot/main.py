import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DEV_GUILD_ID")  # optional: for instant slash-command sync while developing

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cardpvp")

INTENTS = discord.Intents.default()
INTENTS.message_content = False  # we don't need raw message reading; slash commands + components only


class CardPvPBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)
        self._swept_orphans = False

    async def setup_hook(self):
        # Load all cogs
        for filename in os.listdir("./cogs"):
            if filename.endswith(".py") and not filename.startswith("_"):
                await self.load_extension(f"cogs.{filename[:-3]}")
                log.info(f"Loaded cog: {filename}")

        # Sync slash commands
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info(f"Synced {len(synced)} commands to dev guild {GUILD_ID}")
        else:
            synced = await self.tree.sync()
            log.info(f"Synced {len(synced)} global commands (can take up to 1hr to propagate)")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")

        # Only once per process -- on_ready can re-fire after a gateway
        # reconnect, and we don't want to re-sweep every time.
        if not self._swept_orphans:
            self._swept_orphans = True
            duel_cog = self.get_cog("Duel")
            if duel_cog:
                await duel_cog.sweep_orphaned_channels()
                # Background task: warms the deckbuilder image cache so the first
                # player to open it doesn't pay the cold-render cost. Not awaited,
                # since nothing needs to block on it.
                asyncio.create_task(duel_cog.prewarm_render_cache())

    async def close(self):
        # Run this BEFORE super().close() disconnects -- channel.delete()
        # calls need a live HTTP connection to succeed.
        duel_cog = self.get_cog("Duel")
        if duel_cog:
            log.info("Cleaning up active duel channels before shutdown...")
            await duel_cog.cleanup_all_channels()
        await super().close()


async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set. Copy .env.example to .env and fill it in.")

    bot = CardPvPBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())