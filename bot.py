import os, sys
import ssl
import asyncio
import asyncpg
import logging

import discord
from discord.ext import commands

from dotenv import load_dotenv
load_dotenv()

from utils import config

# ─── Logging Setup ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("discord_bot")

# ─── Discord Bot Setup ─────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ─── Create DB pool before bot startup ─────────────────────────
async def init_db_pool():
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    bot.db = await asyncpg.create_pool(
        dsn=config.DATABASE_URL,
        ssl=ssl_ctx,
        timeout=10,
        statement_cache_size=0
    )
    logger.info("✅ Database pool created")

# ─── Sync slash commands on first ready ────────────────────────
@bot.event
async def on_ready():
    print("🟢 on_ready triggered")

    if not getattr(bot, "synced", False):
        try:
            # 🔧 TEMP: clear all global commands to prevent duplicates
            await bot.tree.sync()
            bot.synced = True
            print("✅ Slash commands force-cleared and synced")
        except Exception as e:
            print(f"❌ Slash sync failed: {e}")

    logger.info(f"✅ Logged in as {bot.user}")
    logger.info("🟡 Attempting to set presence...")

    try:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Streaming(name="ㅎㅇㅎㅇ", url="https://twitch.tv/asdf")
        )
        logger.info("✅ Presence set to Streaming")
    except Exception as e:
        logger.exception("❌ Failed to set presence")

# ─── Load all cogs from /cogs ──────────────────────────────────
async def load_extensions():
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py") and filename != "__init__.py":
            try:
                logger.info(f"🔄 Loading cog: {filename}")
                await bot.load_extension(f"cogs.{filename[:-3]}")
                logger.info(f"✅ Loaded: {filename}")
            except Exception as e:
                logger.exception(f"❌ Failed to load {filename}")

# ─── Entry point ───────────────────────────────────────────────
async def main():
    await init_db_pool()
    await load_extensions()

    from cogs.tickets import HelpView
    from cogs.xp import DailyXPView
    from cogs.coins import DailyCoinsView

    bot.add_view(HelpView(bot))
    bot.add_view(DailyXPView(bot))
    bot.add_view(DailyCoinsView(bot))

    await bot.start(config.DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
