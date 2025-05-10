# cogs/welcome.py

import discord
from discord.ext import commands
from discord import File
from datetime import datetime
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
import os

from utils import config
from utils.logger import log_to_channel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BG_PATH  = os.path.join(BASE_DIR, "..", "assets", "welcome_bg.png")
FONT_PATH_KR = os.path.join(BASE_DIR, "..", "assets", "fonts", "NotoSansKR-Bold.ttf")
FONT_SIZE = 72

# fallback font
try:
    FONT = ImageFont.truetype(FONT_PATH_KR, FONT_SIZE)
except OSError:
    FONT = ImageFont.load_default()
    print("⚠️ fallback font used; Korean may not render")

class WelcomeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        ch = self.bot.get_channel(config.WELCOME_CHANNEL_ID)
        if not ch:
            return

        try:
            card = await self.make_welcome_card(member)
        except Exception as e:
            return await ch.send(f"⚠️ 환영 카드 생성 실패: {e}")

        file = File(card, filename="welcome.png")

        embed = discord.Embed(
            title=f"{member.display_name}님 안녕하세요!",
            description="스튜디오에서 새로운 시작을 환영합니다!",
            color=discord.Color.green()
        )
        embed.add_field(
            name="1️⃣ 서버 규칙을 반드시 확인하고 숙지해 주세요!",
            value=f" • <#{config.RULES_CHANNEL_ID}>",
            inline=False
        )
        embed.add_field(
            name="2️⃣ 역할지급 채널에서 본인에게 맞는 역할을 선택해 주세요!",
            value=f" • <#{config.ROLE_ASSIGN_CHANNEL_ID}>",
            inline=False
        )
        embed.add_field(
            name="3️⃣ 공지사항을 놓치지 말고 꼭 확인해 주세요!",
            value=f" • <#{config.ANNOUNCEMENTS_CHANNEL_ID}>",
            inline=False
        )
        embed.set_image(url="attachment://welcome.png")

        await ch.send(
            content=member.mention,
            embed=embed,
            file=file,
            allowed_mentions=discord.AllowedMentions(users=True)
        )

    async def make_welcome_card(self, member: discord.Member) -> BytesIO:
        bg = Image.open(BG_PATH).convert("RGBA")
        draw = ImageDraw.Draw(bg)

        avatar_asset = member.display_avatar.with_size(128).with_format("png")
        avatar_bytes = await avatar_asset.read()
        avatar = Image.open(BytesIO(avatar_bytes)).resize((128, 128)).convert("RGBA")

        bg.paste(avatar, (40, bg.height // 2 - 64), avatar)

        try:
            font = ImageFont.truetype(FONT_PATH_KR, FONT_SIZE)
        except OSError:
            font = ImageFont.load_default()

        text = f"하이요, {member.display_name}님!"
        bbox = draw.textbbox((0, 0), text, font=font)
        x = 200
        y = (bg.height // 2) - ((bbox[3] - bbox[1]) // 2)

        draw.text((x, y), text, font=font, fill="white")

        buf = BytesIO()
        bg.save(buf, "PNG")
        buf.seek(0)
        return buf

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        ch = self.bot.get_channel(config.LEAVE_CHANNEL_ID)
        if not ch:
            return

        embed = discord.Embed(
            title="회원 퇴장",
            description=f"**{member}**님이 서버를 떠났습니다.",
            color=discord.Color.dark_grey(),
            timestamp=datetime.now()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await ch.send(embed=embed)

async def setup(bot):
    await bot.add_cog(WelcomeCog(bot))
