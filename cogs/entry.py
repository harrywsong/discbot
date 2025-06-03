# cogs/entry.py

import re
import discord
import traceback
from discord.ext import commands
from discord.ui import View, Modal, TextInput, Button
from discord import Interaction
from utils import config
from utils.henrik import henrik_get

# ─── Modal ────────────────────────────────────────────────────────────
class EntryModal(Modal):
    def __init__(self):
        super().__init__(title="스튜디오 입장 양식")
        self.riot_tag   = TextInput(label="라이엇 아이디 (태그 포함)", placeholder="Ex: Connect#CAN")
        self.birth_year = TextInput(label="출생연도",             placeholder="Ex: 1998")
        self.curr_tier  = TextInput(label="현 티어",             placeholder="Ex: 실버 2")
        self.top_tier   = TextInput(label="최고 티어",           placeholder="Ex: 골드 3")
        self.inviter    = TextInput(
            label="초대자 디스코드 닉네임 / 유저네임",
            placeholder="Ex: 햄붤거 / k00wh",
            required=True
        )
        for fld in (self.riot_tag, self.birth_year, self.curr_tier, self.top_tier, self.inviter):
            self.add_item(fld)

    async def on_submit(self, inter: Interaction):
        # 1) defer so we can follow up
        try:
            await inter.response.defer(ephemeral=True)
        except:
            traceback.print_exc()
            return

        # 2) parse inputs
        riot  = self.riot_tag.value
        birth = self.birth_year.value
        curr  = self.curr_tier.value
        top   = self.top_tier.value
        raw   = self.inviter.value or ""
        m     = re.search(r"\d{17,19}", raw)
        if m:
            inv_id = int(m.group())
            member = inter.guild.get_member(inv_id) or await inter.guild.fetch_member(inv_id)
            inviter_mention = member.mention if member else f"<@{inv_id}>"
        else:
            inviter_mention = raw or "—"

        # 3) build embed
        embed = discord.Embed(
            title="신규 입장 양식",
            description=f"제출자: {inter.user.mention}",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="라이엇 아이디",     value=riot,            inline=False)
        embed.add_field(name="출생연도",         value=birth,           inline=True)
        embed.add_field(name="현 티어",          value=curr,            inline=True)
        embed.add_field(name="최고 티어",        value=top,             inline=True)
        embed.add_field(name="초대자 닉네임/ID", value=inviter_mention, inline=False)
        embed.set_footer(text=f"Discord ID: {inter.user.id}")

        # 4) send to log channel
        try:
            log_ch = inter.guild.get_channel(config.ENTRY_LOG_CHANNEL_ID)
            if log_ch:
                await log_ch.send(embed=embed)
            else:
                print(f"[EntryModal] log channel not found: {config.ENTRY_LOG_CHANNEL_ID}")
        except:
            traceback.print_exc()
            return await inter.followup.send("⚠️ 오류가 발생했습니다.", ephemeral=True)

        # 5) remove Unverified role
        try:
            unv = inter.guild.get_role(config.UNVERIFIED_ROLE_ID)
            if unv:
                await inter.user.remove_roles(unv, reason="Completed entry form")
        except:
            traceback.print_exc()

        # 6) confirmation
        await inter.followup.send(
            "✅ 입장 양식 제출 완료! https://discord.com/channels/1059211805567746090/1207972911420538900 채널 접근 권한을 활성화했습니다.",
            ephemeral=True
        )


# ─── Button ───────────────────────────────────────────────────────────
class EntryButton(Button):
    def __init__(self):
        super().__init__(
            custom_id="entry_button",
            label="📝 입장 양식 작성",
            style=discord.ButtonStyle.primary
        )

    async def callback(self, inter: Interaction):
        await inter.response.send_modal(EntryModal())


# ─── Cog to mirror the shop-pattern ────────────────────────────────────
class EntryPersistent(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 1) Create & register the persistent view globally
        view = View(timeout=None)
        view.add_item(EntryButton())
        bot.add_view(view)

    @commands.Cog.listener()
    async def on_ready(self):
        chan = self.bot.get_channel(config.ENTRY_BUTTON_CHANNEL_ID)
        if not chan:
            print(f"[EntryPersistent] invalid channel ID: {config.ENTRY_BUTTON_CHANNEL_ID}")
            return

        # 2) clear out old buttons
        try:
            await chan.purge(limit=None)
        except:
            traceback.print_exc()

        # 3) send the embed + fresh view
        embed = discord.Embed(
            title="🎴 유곽의 문이 열립니다",
            description="스튜디오에 입장을 시작합니다.\n아래 버튼을 눌러 입장 양식을 작성해 주세요.",
            color=discord.Color.blurple()
        )
        view = View(timeout=None)
        view.add_item(EntryButton())
        try:
            await chan.send(embed=embed, view=view)
        except:
            traceback.print_exc()


async def setup(bot: commands.Bot):
    await bot.add_cog(EntryPersistent(bot))
