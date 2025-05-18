# cogs/voice.py new

import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone
from discord.utils import find

from utils import config
from utils.logger import log_to_channel

created_channels: dict[int, datetime] = {}

voice_channel_2_name = "🎗️ discord.gg/ourstudio"

def get_channels(guild: discord.Guild):
    vc1 = find(lambda c: c.name.startswith("🟢"), guild.voice_channels)
    vc2 = find(lambda c: c.name.startswith("🎗"), guild.voice_channels)
    vc3 = find(lambda c: c.name.startswith("👥"), guild.voice_channels)
    return vc1, vc2, vc3

class VoiceManager(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.periodic_update.start()
        self.periodic_cleanup.start()

    @tasks.loop(minutes=60)
    async def periodic_update(self):
        await log_to_channel(self.bot, "📊 통계 채널 이름 업데이트 실행")
        for guild in self.bot.guilds:
            vc1, vc2, vc3 = get_channels(guild)
            if not all([vc1, vc2, vc3]):
                continue

            online_count = sum(1 for m in guild.members if m.status == discord.Status.online)
            idle_count   = sum(1 for m in guild.members if m.status == discord.Status.idle)
            dnd_count    = sum(1 for m in guild.members if m.status == discord.Status.dnd)
            total_count  = len(guild.members)

            name1 = f"🟢 {online_count}    🌙 {idle_count}    ⛔ {dnd_count}"
            name2 = voice_channel_2_name
            name3 = f"👥 Users: {total_count}"

            if vc1.name != name1:
                await vc1.edit(name=name1)
            if vc2.name != name2:
                await vc2.edit(name=name2)
            if vc3.name != name3:
                await vc3.edit(name=name3)

    @tasks.loop(minutes=60)
    async def periodic_cleanup(self):
        await log_to_channel(self.bot, "🧹 자동 채널 정리 실행")
        now = datetime.now(timezone.utc)
        to_remove = []

        for chan_id, created_at in list(created_channels.items()):
            channel = self.bot.get_channel(chan_id)
            if not channel:
                to_remove.append(chan_id)
                continue
            if isinstance(channel, discord.VoiceChannel) and len(channel.members) == 0:
                if now >= created_at + timedelta(minutes=60):
                    try:
                        await channel.delete()
                        await log_to_channel(self.bot, f"🗑️ 비어있는 채널 `{channel.name}` 삭제됨")
                    except Exception as e:
                        await log_to_channel(self.bot, f"❌ 삭제 실패: `{channel.name}` - {e}")
                    to_remove.append(chan_id)

        for cid in to_remove:
            created_channels.pop(cid, None)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        now = datetime.now(timezone.utc)

        # ── Auto-delete empty temp channel ──
        if before.channel and before.channel.id in created_channels:
            if len(before.channel.members) == 0:
                try:
                    await before.channel.delete()
                    await log_to_channel(self.bot, f"🗑️ `{before.channel.name}` 자동 삭제됨")
                    created_channels.pop(before.channel.id, None)
                except Exception as e:
                    await log_to_channel(self.bot, f"❌ 채널 삭제 오류: {e}")

        # ── Create new temp channel ──
        if after.channel and after.channel.name == "🔊┆임시 음성채널 생성":
            category = after.channel.category or member.guild.categories[0]
            new_channel = await member.guild.create_voice_channel(
                f"🔊┆{member.display_name}님의 스튜디오", category=category,
                overwrites={
                    member.guild.default_role: discord.PermissionOverwrite(connect=True),
                    member: discord.PermissionOverwrite(manage_channels=True, move_members=True)
                }
            )
            await member.move_to(new_channel)
            created_channels[new_channel.id] = now
            await log_to_channel(self.bot, f"🎧 `{new_channel.name}` 생성됨 (by {member.display_name})")

async def setup(bot):
    await bot.add_cog(VoiceManager(bot))
