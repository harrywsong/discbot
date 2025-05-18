# utils/logger.py new

import discord
from utils import config

async def log_to_channel(bot, message: str):
    channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if channel:
        await channel.send(f"📋 {message}")
    print("[LOG]", message)
