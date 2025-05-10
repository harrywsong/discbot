# bot.py

import os
import ssl
import asyncio
import asyncpg

import discord
from discord.ext import commands

from utils import config

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    # ─── Ensure DB pool exists BEFORE yielding ───
    if not hasattr(bot, "db") or bot.db is None:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        bot.db = await asyncpg.create_pool(
            dsn=config.DATABASE_URL,
            ssl=ssl_ctx,
            timeout=10,
            statement_cache_size=0
        )
        print("✅ Database pool created")

    print(f"✅ Logged in as {bot.user}")

    # ─── Now it’s safe to sync slash commands ───
    await bot.tree.sync()
    print("✅ Slash commands synced")

    # ─── Persistent views ───
    from cogs.tickets import HelpView
    from cogs.xp import DailyXPView

    bot.add_view(HelpView(bot))
    bot.add_view(DailyXPView(bot))

    # ─── Set presence ───
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Streaming(name="ㅎㅇㅎㅇ", url="https://twitch.tv/imheju")
    )

# ─── Load all cogs ────────────────────────────────
async def load_extensions():
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py") and filename != "__init__.py":
            await bot.load_extension(f"cogs.{filename[:-3]}")

async def main():
    await load_extensions()
    await bot.start(config.DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
