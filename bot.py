import os
import ssl
import asyncio
import asyncpg

import discord
from discord.ext import commands

from dotenv import load_dotenv
load_dotenv()

from utils import config

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ─── Create DB pool before bot startup ────────────────────────────────────
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
    print("✅ Database pool created")

# ─── Only sync commands once ───────────────────────────────────────────────
@bot.event
async def on_ready():
    if not getattr(bot, "synced", False):
        await bot.tree.sync()
        bot.synced = True
        print("✅ Slash commands synced")

    print(f"✅ Logged in as {bot.user}")

    # Presence can stay here
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Streaming(name="ㅎㅇㅎㅇ", url="https://twitch.tv/imheju")
    )

# ─── Load all cogs from /cogs ─────────────────────────────────────────────
async def load_extensions():
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py") and filename != "__init__.py":
            await bot.load_extension(f"cogs.{filename[:-3]}")

# ─── Entry point ─────────────────────────────────────────────────────────
async def main():
    # 1) init DB pool
    await init_db_pool()

    # 2) load all cogs (this runs setup() in cogs/entry.py)
    await load_extensions()

    # 3) register your other persistent views…
    from cogs.tickets import HelpView
    from cogs.xp      import DailyXPView

    bot.add_view(HelpView(bot))
    bot.add_view(DailyXPView(bot))

    # 4) start the bot
    await bot.start(config.DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
