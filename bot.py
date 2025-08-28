# bot.py - Enhanced with stability fixes

import os
import ssl
import asyncio
import asyncpg
import logging
import signal
import sys
from contextlib import asynccontextmanager

import discord
from discord.ext import commands

from dotenv import load_dotenv

load_dotenv()

from utils import config

# ─── Enhanced Logging Setup ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("discord_bot")

# ─── Discord Bot Setup ─────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ─── Database Connection Pool with Better Error Handling ───────────────────
@asynccontextmanager
async def get_db_connection():
    """Context manager for database connections with automatic cleanup"""
    conn = None
    try:
        conn = await bot.db.acquire()
        yield conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        raise
    finally:
        if conn:
            await bot.db.release(conn)


async def init_db_pool():
    """Initialize database pool with better error handling and retry logic"""
    max_retries = 3
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            bot.db = await asyncpg.create_pool(
                dsn=config.DATABASE_URL,
                ssl=ssl_ctx,
                timeout=30,  # Increased timeout
                command_timeout=60,  # Command timeout
                max_size=20,  # Pool size
                min_size=1,
                statement_cache_size=0,
                server_settings={
                    'application_name': 'discord_bot'
                }
            )
            logger.info("✅ Database pool created successfully")
            return
        except Exception as e:
            logger.error(f"❌ Database pool creation failed (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
            else:
                raise


async def close_db_pool():
    """Clean up database pool"""
    if hasattr(bot, 'db') and bot.db:
        await bot.db.close()
        logger.info("✅ Database pool closed")


# ─── Graceful Shutdown Handler ─────────────────────────────────────────────
def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    asyncio.create_task(shutdown())


async def shutdown():
    """Graceful shutdown procedure"""
    logger.info("🛑 Starting graceful shutdown...")

    # Close database connections
    await close_db_pool()

    # Close bot
    if not bot.is_closed():
        await bot.close()

    logger.info("✅ Graceful shutdown completed")
    sys.exit(0)


# ─── Enhanced Error Handling ───────────────────────────────────────────────
@bot.event
async def on_error(event, *args, **kwargs):
    """Enhanced global error handler"""
    logger.exception(f"Unhandled error in event {event}")

    # Try to log to Discord channel if possible
    try:
        from utils.logger import log_to_channel
        await log_to_channel(bot, f"⚠️ Unhandled error in event {event}: {sys.exc_info()[1]}")
    except:
        pass  # Don't let logging errors crash the bot


@bot.event
async def on_command_error(ctx, error):
    """Enhanced command error handler"""
    if isinstance(error, commands.CommandOnCooldown):
        return  # Handled elsewhere

    logger.exception(f"Command error in {ctx.command}: {error}")

    try:
        from utils.logger import log_to_channel
        await log_to_channel(bot, f"⚠️ Command error in {ctx.command}: {error}")
    except:
        pass


# ─── Enhanced Ready Event ──────────────────────────────────────────────────
@bot.event
async def on_ready():
    print("🟢 on_ready triggered")
    logger.info(f"✅ Logged in as {bot.user}")

    if not getattr(bot, "synced", False):
        try:
            await bot.tree.sync()
            bot.synced = True
            logger.info("✅ Slash commands synced")
        except Exception as e:
            logger.error(f"❌ Slash sync failed: {e}")

    # Set presence with error handling
    logger.info("🟡 Attempting to set presence...")
    try:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Streaming(name="ㅎㅇㅎㅇ", url="https://twitch.tv/asdf")
        )
        logger.info("✅ Presence set successfully")
    except Exception as e:
        logger.exception("❌ Failed to set presence")


# ─── Enhanced Extension Loading ────────────────────────────────────────────
async def load_extensions():
    """Load extensions with better error handling"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cogs_dir = os.path.join(base_dir, "cogs")

    loaded_cogs = []
    failed_cogs = []

    for filename in os.listdir(cogs_dir):
        if filename.endswith(".py") and filename != "__init__.py":
            try:
                logger.info(f"🔄 Loading cog: {filename}")
                await bot.load_extension(f"cogs.{filename[:-3]}")
                loaded_cogs.append(filename)
                logger.info(f"✅ Loaded: {filename}")
            except Exception as e:
                failed_cogs.append(filename)
                logger.exception(f"❌ Failed to load {filename}")

    logger.info(f"📊 Cogs loaded: {len(loaded_cogs)}, failed: {len(failed_cogs)}")
    return loaded_cogs, failed_cogs


# ─── Health Check Task ─────────────────────────────────────────────────────
@bot.event
async def on_ready():
    """Start health monitoring after bot is ready"""
    if not hasattr(bot, '_health_check_started'):
        bot._health_check_started = True
        bot.loop.create_task(health_check_loop())


async def health_check_loop():
    """Periodic health check to detect issues early"""
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            # Check database connection
            if hasattr(bot, 'db') and bot.db:
                async with get_db_connection() as conn:
                    await conn.fetchval("SELECT 1")

            # Check Discord connection
            latency = bot.latency
            if latency > 5.0:  # High latency warning
                logger.warning(f"⚠️ High latency detected: {latency:.2f}s")

            await asyncio.sleep(300)  # Check every 5 minutes

        except Exception as e:
            logger.error(f"❌ Health check failed: {e}")
            try:
                from utils.logger import log_to_channel
                await log_to_channel(bot, f"⚠️ Health check failed: {e}")
            except:
                pass
            await asyncio.sleep(60)  # Shorter retry interval on failure


# ─── Main Entry Point with Enhanced Error Handling ─────────────────────────
async def main():
    """Main entry point with comprehensive error handling"""
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Initialize database
        await init_db_pool()

        # Load extensions
        loaded, failed = await load_extensions()

        # Add persistent views
        try:
            from cogs.tickets import HelpView
            from hidden.xp import DailyXPView
            from cogs.coins import DailyCoinsView

            bot.add_view(HelpView(bot))
            bot.add_view(DailyXPView(bot))
            bot.add_view(DailyCoinsView(bot))
        except ImportError as e:
            logger.warning(f"⚠️ Could not load some persistent views: {e}")

        # Start bot
        logger.info("🚀 Starting bot...")
        await bot.start(config.DISCORD_TOKEN)

    except KeyboardInterrupt:
        logger.info("🛑 Received keyboard interrupt")
    except Exception as e:
        logger.exception(f"❌ Fatal error in main: {e}")
        raise
    finally:
        await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as e:
        logger.exception(f"❌ Fatal startup error: {e}")
        sys.exit(1)