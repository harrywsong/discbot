import discord
from discord.ext import commands
from utils import config
from utils.logger import log_to_channel
from utils.henrik import henrik_get

# message_id -> { emoji_str: [role_id, ...], ... }
reaction_mappings: dict[int, dict[str, list[int]]] = {}

class ReactionRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # 봇 자신의 리액션은 무시
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
            role_names = ", ".join(r.name for r in roles)
            await log_to_channel(
                self.bot,
                f"➕ {member.display_name}님에게 {role_names} 역할이 부여되었습니다."
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
            role_names = ", ".join(r.name for r in roles)
            await log_to_channel(
                self.bot,
                f"➖ {member.display_name}님에게서 {role_names} 역할이 제거되었습니다."
            )

    @commands.Cog.listener()
    async def on_ready(self):
        # 미리 설정된 reaction-role 매핑을 초기화
        await self.seed_reaction_roles()

    async def seed_reaction_roles(self):
        # 설정된 채널 및 메시지, 이모지-역할 매핑 목록
        channel_and_messages = [
            (config.ROLE_ASSIGN_CHANNEL_ID, config.ROLE_ASSIGN_MESSAGE_ID, config.REACTION_TO_ROLES),
            (config.COIN_ASSIGN_CHANNEL_ID, config.COIN_ASSIGN_MESSAGE_ID, config.REACTION_TO_COINS),
            (config.XP_ASSIGN_CHANNEL_ID,   config.XP_ASSIGN_MESSAGE_ID,   config.REACTION_TO_XP),
            (config.ANON_ASSIGN_CHANNEL_ID, config.ANON_ASSIGN_MESSAGE_ID, config.REACTION_TO_ANON_BOARD),
            (config.COLOR_ASSIGN_CHANNEL_ID, config.COLOR_ASSIGN_MESSAGE_ID, config.REACTION_TO_COLOR_ROLES),
            (config.TIER_ASSIGN_CHANNEL_ID,  config.TIER_ASSIGN_MESSAGE_ID,  config.REACTION_TO_TIERS),
            (config.GAME_ROLE_CHANNEL_ID,    config.GAME_ROLE_MESSAGE_ID,    config.REACTION_TO_GAMES),
        ]

        # 규칙 수락 메시지가 설정되어 있으면 맨 앞에 추가
        RULES_MESSAGE_ID = getattr(config, "RULES_MESSAGE_ID", None)
        if RULES_MESSAGE_ID:
            channel_and_messages.insert(
                0,
                (config.RULES_CHANNEL_ID, RULES_MESSAGE_ID, config.REACTION_TO_ACCEPT_RULES)
            )

        for ch_id, msg_id, mapping in channel_and_messages:
            ch = self.bot.get_channel(ch_id)
            if not ch:
                print(f"⚠️ 채널을 찾을 수 없습니다: {ch_id}")
                continue

            try:
                msg = await ch.fetch_message(msg_id)
            except discord.NotFound:
                print(f"⚠️ 메시지를 찾을 수 없습니다: {msg_id} (채널 {ch_id})")
                continue

            # 동일 메시지에 여러 매핑이 합쳐지도록 병합
            if msg.id not in reaction_mappings:
                reaction_mappings[msg.id] = {}
            for emoji, role_ids in mapping.items():
                if emoji:
                    reaction_mappings[msg.id][emoji] = role_ids

            # 매핑에 설정된 이모지가 메시지에 없는 경우 추가
            existing = {str(r.emoji) for r in msg.reactions}
            for emoji in mapping.keys():
                if emoji not in existing:
                    try:
                        await msg.add_reaction(emoji)
                    except discord.HTTPException as e:
                        print(f"⚠️ 이모지 추가 실패: {emoji} ({e})")

        await log_to_channel(self.bot, "✅ 리액션 역할 설정이 완료되고 봇이 온라인 상태입니다!")

async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))
