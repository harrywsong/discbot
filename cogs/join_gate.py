import discord
from discord.ext import commands
from utils import config
from utils.logger import log_to_channel

class JoinGate(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        unv = member.guild.get_role(config.UNVERIFIED_ROLE_ID)
        if unv:
            await member.add_roles(unv, reason="Join‑gate: unverified")
            # ▶ Log: 새 멤버 가입 및 역할 부여
            await log_to_channel(
                self.bot,
                f"➕ [가입] {member.display_name}님이 서버에 가입했습니다.\n"
                f"✅ Unverified 역할이 부여되었습니다."
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(JoinGate(bot))
