# cogs/welcome.py new

import discord
from discord.ext import commands
from discord import File
from datetime import datetime
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
import os
import traceback
import asyncio

from utils import config

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
BG_PATH      = os.path.join(BASE_DIR, "..", "assets", "welcome_bg.png")
FONT_PATH_KR = os.path.join(BASE_DIR, "..", "assets", "fonts", "NotoSansKR-Bold.ttf")
FONT_SIZE    = 72

# Preload a fallback font
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
        print(f"⚙️ on_member_join fired for {member} (ID: {member.id}); channel → {ch}")
        if not ch:
            print("❌ No channel found; check WELCOME_CHANNEL_ID")
            return

        # ─── ORIGINAL WELCOME‐CARD LOGIC ────────────────────
        # 1) generate image buffer
        try:
            print("🔧 [welcome] generating welcome card…")
            card_buf = await self.make_welcome_card(member)
            print("✅ [welcome] card generated")
        except Exception as e:
            traceback.print_exc()
            return await ch.send(f"⚠️ 환영 카드 생성 실패: {e}")

        # 2) wrap in File
        try:
            print("🔧 [welcome] wrapping buffer in File…")
            file = File(card_buf, filename="welcome.png")
            print("✅ [welcome] File created")
        except Exception as e:
            print("❌ [welcome] File creation failed:", e)
            traceback.print_exc()
            return

        # 3) build full embed
        try:
            print("🔧 [welcome] building embed…")
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
            print("✅ [welcome] embed built")
        except Exception as e:
            print("❌ [welcome] embed build failed:", e)
            traceback.print_exc()
            return

        # 4) send it
        try:
            print("🔧 [welcome] sending final welcome…")
            await ch.send(
                content=member.mention,
                embed=embed,
                file=file,
                allowed_mentions=discord.AllowedMentions(users=True)
            )
            print("✅ [welcome] welcome message sent")
        except Exception as e:
            print("❌ [welcome] failed to send welcome message:", e)
            traceback.print_exc()

    async def make_welcome_card(self, member: discord.Member) -> BytesIO:
        # open background
        bg = Image.open(BG_PATH).convert("RGBA")
        draw = ImageDraw.Draw(bg)

        # fetch avatar (with timeout)
        avatar_asset = member.display_avatar.with_size(128).with_format("png")
        try:
            avatar_bytes = await asyncio.wait_for(avatar_asset.read(), timeout=5)
        except Exception as e:
            print("❌ [welcome] avatar fetch failed:", e)
            avatar_bytes = None

        if avatar_bytes:
            avatar = Image.open(BytesIO(avatar_bytes)).resize((128, 128)).convert("RGBA")
            bg.paste(avatar, (40, bg.height // 2 - 64), avatar)

        # draw text
        try:
            font = ImageFont.truetype(FONT_PATH_KR, FONT_SIZE)
        except OSError:
            font = ImageFont.load_default()

        text = f"하이요, {member.display_name}님!"
        bbox = draw.textbbox((0, 0), text, font=font)
        x = 200
        y = (bg.height // 2) - ((bbox[3] - bbox[1]) // 2)
        draw.text((x, y), text, font=font, fill="white")

        # output buffer
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
