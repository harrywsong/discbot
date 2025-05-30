# cogs/reactions.py

import discord
from discord.ext import commands
from utils import config
from utils.logger import log_to_channel

# message_id -> { emoji_str: [role_id, ...], ... }
reaction_mappings: dict[int, dict[str, list[int]]] = {}

class ReactionRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # Ignore the bot's own reactions
        if payload.user_id == self.bot.user.id:
            return

        mapping = reaction_mappings.get(payload.message_id)
        if not mapping:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.NotFound:
            return

        emoji = str(payload.emoji)
        role_ids = mapping.get(emoji)
        if not role_ids:
            return

        roles = [guild.get_role(rid) for rid in role_ids if guild.get_role(rid)]
        if roles:
            await member.add_roles(*roles, reason="Reaction role add")
            await log_to_channel(
                self.bot,
                f"{member.display_name} → {', '.join(r.name for r in roles)} 역할 부여됨"
            )

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        mapping = reaction_mappings.get(payload.message_id)
        if not mapping:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.NotFound:
            return

        emoji = str(payload.emoji)
        role_ids = mapping.get(emoji)
        if not role_ids:
            return

        roles = [guild.get_role(rid) for rid in role_ids if guild.get_role(rid)]
        if roles:
            await member.remove_roles(*roles, reason="Reaction role remove")
            await log_to_channel(
                self.bot,
                f"{member.display_name} → {', '.join(r.name for r in roles)} 역할 제거됨"
            )

    @commands.Cog.listener()
    async def on_ready(self):
        # Seed (and merge) all configured reaction-role maps
        await self.seed_reaction_roles()

    async def seed_reaction_roles(self):
        # Note: using ANON_ASSIGN_* to match config.py
        channel_and_messages = [
            # 기존 일반 역할
            (config.ROLE_ASSIGN_CHANNEL_ID, config.ROLE_ASSIGN_MESSAGE_ID, config.REACTION_TO_ROLES),

            # XP, Coin, 익명게시판 → 모두 같은 메시지로 설정 가능
            (config.COIN_ASSIGN_CHANNEL_ID, config.COIN_ASSIGN_MESSAGE_ID, config.REACTION_TO_COINS),
            (config.XP_ASSIGN_CHANNEL_ID,   config.XP_ASSIGN_MESSAGE_ID,   config.REACTION_TO_XP),
            (config.ANON_ASSIGN_CHANNEL_ID, config.ANON_ASSIGN_MESSAGE_ID, config.REACTION_TO_ANON_BOARD),

            # 기존 색상·티어·게임 역할
            (config.COLOR_ASSIGN_CHANNEL_ID, config.COLOR_ASSIGN_MESSAGE_ID, config.REACTION_TO_COLOR_ROLES),
            (config.TIER_ASSIGN_CHANNEL_ID,  config.TIER_ASSIGN_MESSAGE_ID,  config.REACTION_TO_TIERS),
            (config.GAME_ROLE_CHANNEL_ID,    config.GAME_ROLE_MESSAGE_ID,    config.REACTION_TO_GAMES),
        ]

        # (Optional) 규칙 수락 메시지
        RULES_MESSAGE_ID = getattr(config, "RULES_MESSAGE_ID", None)
        if RULES_MESSAGE_ID:
            channel_and_messages.insert(
                0,
                (config.RULES_CHANNEL_ID, RULES_MESSAGE_ID, config.REACTION_TO_ACCEPT_RULES)
            )

        for ch_id, msg_id, mapping in channel_and_messages:
            ch = self.bot.get_channel(ch_id)
            if not ch:
                print(f"⚠️ Channel {ch_id} not found")
                continue

            try:
                msg = await ch.fetch_message(msg_id)
            except discord.NotFound:
                print(f"⚠️ Message {msg_id} not found in channel {ch_id}")
                continue

            # Merge this mapping into any existing for the same message
            if msg.id not in reaction_mappings:
                reaction_mappings[msg.id] = {}
            for emoji, role_ids in mapping.items():
                if emoji:
                    reaction_mappings[msg.id][emoji] = role_ids

            # Add any missing reactions to the message
            existing = {str(r.emoji) for r in msg.reactions}
            for emoji in mapping.keys():
                if emoji not in existing:
                    try:
                        await msg.add_reaction(emoji)
                    except discord.HTTPException as e:
                        print(f"⚠️ Failed to add reaction {emoji}: {e}")

        await log_to_channel(self.bot, "✅ 봇 온라인!")


async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))
