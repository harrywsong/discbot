import discord
from discord.ext import commands
from utils import config
from utils.henrik import henrik_get

class JoinGate(commands.Cog):
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        unv = member.guild.get_role(config.UNVERIFIED_ROLE_ID)
        if unv:
            await member.add_roles(unv, reason="Join‑gate: unverified")

async def setup(bot: commands.Bot):
    await bot.add_cog(JoinGate(bot))
