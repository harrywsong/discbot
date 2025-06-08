# cogs/party_finder.py
import asyncio
import json
import logging
import discord
from discord import PartialEmoji, ui, SelectOption
from discord.ext import commands
from discord import app_commands
from utils import config

# configure logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# load settings
PARTY_CHANNEL_ID: int = config.PARTY_CHANNEL_ID           # 텍스트 파티 알림 채널
PARTY_CATEGORY_ID: int = 1365878115196211272              # 음성 채널 생성 카테고리 ID
TIER_ROLE_IDS: dict = (
    config.TIER_ROLE_IDS if isinstance(config.TIER_ROLE_IDS, dict)
    else json.loads(config.TIER_ROLE_IDS)
)

TIERS = [
    "아이언", "브론즈", "실버", "골드",
    "플레티넘", "다이아몬드", "초월자",
    "불멸", "레디언트"
]

TIER_EMOJIS = {
    "아이언":      PartialEmoji(name="iron_icon",      id=1367050325457899590),
    "브론즈":      PartialEmoji(name="bronze_icon",    id=1367050339987095563),
    "실버":        PartialEmoji(name="silver_icon",    id=1367050333083402280),
    "골드":        PartialEmoji(name="gold_icon",      id=1367050331242106951),
    "플레티넘":    PartialEmoji(name="plat_icon",      id=1367055859435175986),
    "다이아몬드":  PartialEmoji(name="diamond_icon",   id=1367055861351972905),
    "초월자":      PartialEmoji(name="ascendant_icon", id=1367050328976920606),
    "불멸":        PartialEmoji(name="immortal_icon",  id=1367050346874011668),
    "레디언트":    PartialEmoji(name="radiant_icon",   id=1367055860479692822),
}


VALORANT_ROLE_ID: int = 1209013681753563156

class PartyView(ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.size: int | None = None
        self.min_tier: str | None = None
        self.max_tier: str | None = None

    def _update_defaults(self):
        for child in self.children:
            if isinstance(child, ui.Select):
                for opt in child.options:
                    if child.custom_id == 'party_size_select':
                        opt.default = (self.size is not None and opt.value == str(self.size))
                    elif child.custom_id == 'min_tier_select':
                        opt.default = (self.min_tier is not None and opt.value == self.min_tier)
                    elif child.custom_id == 'max_tier_select':
                        opt.default = (self.max_tier is not None and opt.value == self.max_tier)

    @ui.select(
        custom_id='party_size_select',
        placeholder='파티 인원 선택',
        min_values=1, max_values=1,
        options=[SelectOption(label=str(n), value=str(n)) for n in (2,3,5)]
    )
    async def select_size(self, interaction: discord.Interaction, select: ui.Select):
        self.size = int(select.values[0])
        self._update_defaults()
        await interaction.response.edit_message(view=self)

    @ui.select(
        custom_id='min_tier_select',
        placeholder='최소 티어 선택',
        min_values=1, max_values=1,
        options=[SelectOption(label=t, value=t, emoji=TIER_EMOJIS[t]) for t in TIERS]
    )
    async def select_min(self, interaction: discord.Interaction, select: ui.Select):
        self.min_tier = select.values[0]
        self._update_defaults()
        await interaction.response.edit_message(view=self)

    @ui.select(
        custom_id='max_tier_select',
        placeholder='최대 티어 선택',
        min_values=1, max_values=1,
        options=[SelectOption(label=t, value=t, emoji=TIER_EMOJIS[t]) for t in TIERS]
    )
    async def select_max(self, interaction: discord.Interaction, select: ui.Select):
        self.max_tier = select.values[0]
        self._update_defaults()
        await interaction.response.edit_message(view=self)

    @ui.button(label='제출', style=discord.ButtonStyle.success, custom_id='party_submit')
    async def submit(self, interaction: discord.Interaction, button: ui.Button):
        # acknowledge to avoid "This interaction failed"
        await interaction.response.defer()

        if not (self.size and self.min_tier and self.max_tier):
            return await interaction.followup.send("⚠️ 모든 옵션을 선택해주세요.", ephemeral=True)

        i1, i2 = TIERS.index(self.min_tier), TIERS.index(self.max_tier)
        if i1 > i2:
            i1, i2 = i2, i1

        guild = interaction.guild
        category = guild.get_channel(PARTY_CATEGORY_ID)
        vc = None
        if category and isinstance(category, discord.CategoryChannel):
            vc = await guild.create_voice_channel(
                name=f"🔊┆{interaction.user.display_name}님의 스튜디오",
                category=category
            )

        invite_url = None
        if vc:
            invite = await vc.create_invite(max_age=86400, max_uses=0, unique=True)
            invite_url = invite.url

            # schedule auto-delete if unused for 10 minutes
            async def delete_if_unused(ch: discord.VoiceChannel):
                await asyncio.sleep(600)
                if len(ch.members) == 0:
                    await ch.delete()
            interaction.client.loop.create_task(delete_if_unused(vc))

        valorant_ping = f"<@&{VALORANT_ROLE_ID}>"
        tier_pings = " ".join(f"<@&{TIER_ROLE_IDS[t]}>" for t in TIERS[i1:i2+1])

        embed = discord.Embed(
            title=f"🎮 발로란트 파티 — 1/{self.size}",
            description=(
                f"**플레이어 {self.size - 1}명 더 구합니다**\n\n"
                f"티어 범위: {TIERS[i1]}–{TIERS[i2]}"
            ),
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name='현재 플레이어',
            value=f"1. {interaction.user.mention}", inline=False
        )

        channel = guild.get_channel(PARTY_CHANNEL_ID)
        if not channel:
            return await interaction.followup.send("❌ 파티 채널을 찾을 수 없습니다.", ephemeral=True)

        # post public party announcement
        await channel.send(
            content=f"{valorant_ping} {tier_pings}",
            embed=embed,
            view=JoinView(self.size, [interaction.user], vc, invite_url)
        )

        # delete the original ephemeral view
        await interaction.delete_original_response()

class JoinView(ui.View):
    def __init__(self, size: int, members: list[discord.Member], vc: discord.VoiceChannel | None, invite_url: str | None):
        super().__init__(timeout=None)
        self.size = size
        self.members = members
        self.vc = vc

        if invite_url:
            self.add_item(
                ui.Button(label="🔗 음성 채널 참여", style=discord.ButtonStyle.link, url=invite_url)
            )

    @ui.button(label='파티 참가', style=discord.ButtonStyle.primary, custom_id='party_join')
    async def join(self, interaction: discord.Interaction, button: ui.Button):
        # existing join logic...
        if interaction.user in self.members:
            return await interaction.response.send_message("⚠️ 이미 파티에 참여 중입니다.", ephemeral=True)
        if len(self.members) >= self.size:
            return await interaction.response.send_message("⚠️ 파티 인원이 가득 찼습니다.", ephemeral=True)
        self.members.append(interaction.user)
        embed = interaction.message.embeds[0]
        embed.title = f"🎮 발로란트 파티 — {len(self.members)}/{self.size}"
        lines = embed.description.split('\n')
        remaining = self.size - len(self.members)
        for idx, line in enumerate(lines):
            if line.startswith("**플레이어") or line.startswith("**모든 인원"):
                lines[idx] = (f"**플레이어 {remaining}명 더 구합니다**" if remaining > 0 else "**모든 인원 모집 완료!**")
                break
        embed.description = '\n'.join(lines)
        embed.set_field_at(0, name='현재 플레이어', value='\n'.join(f"{i+1}. {m.mention}" for i, m in enumerate(self.members)), inline=False)
        if remaining == 0:
            for child in self.children:
                if getattr(child, 'custom_id', None) == 'party_join':
                    child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label='파티 탈퇴', style=discord.ButtonStyle.danger, custom_id='party_leave')
    async def leave(self, interaction: discord.Interaction, button: ui.Button):
        # existing leave logic...
        if interaction.user not in self.members:
            return await interaction.response.send_message("⚠️ 파티에 참여하고 있지 않습니다.", ephemeral=True)
        self.members.remove(interaction.user)
        embed = interaction.message.embeds[0]
        embed.title = f"🎮 발로란트 파티 — {len(self.members)}/{self.size}"
        lines = embed.description.split('\n')
        remaining = self.size - len(self.members)
        for idx, line in enumerate(lines):
            if line.startswith("**플레이어") or line.startswith("**모든 인원"):
                lines[idx] = (f"**플레이어 {remaining}명 더 구합니다**" if remaining > 0 else "**모든 인원 모집 완료!**")
                break
        embed.description = '\n'.join(lines)
        embed.set_field_at(0, name='현재 플레이어', value='\n'.join(f"{i+1}. {m.mention}" for i, m in enumerate(self.members)), inline=False)
        if remaining > 0:
            for child in self.children:
                if getattr(child, 'custom_id', None) == 'party_join':
                    child.disabled = False
        await interaction.response.edit_message(embed=embed, view=self)

class PartyFinder(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(self.partyfinder)  # ⬅️ This line makes it work!

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        ch = before.channel
        if ch and isinstance(ch, discord.VoiceChannel) and ch.category_id == PARTY_CATEGORY_ID and ch.name != "🔊┆임시 음성채널 생성":
            if len(ch.members) == 0:
                try:
                    await ch.delete()
                except Exception as e:
                    logger.warning(f"Failed to delete empty VC {ch.id}: {e}")

    @app_commands.command(name='구인구직', description='발로란트 파티를 생성합니다 (인원 수 + 티어)')
    async def partyfinder(self, interaction: discord.Interaction):
        view = PartyView()
        view._update_defaults()
        await interaction.response.send_message("파티 인원 수와 티어 범위를 선택하세요:", view=view, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(PartyFinder(bot))
