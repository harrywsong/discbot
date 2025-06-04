# cogs/welcome.py

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
from utils.logger import log_to_channel
from utils.henrik import henrik_get

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
BG_PATH      = os.path.join(BASE_DIR, "..", "assets", "welcome_bg.png")
FONT_PATH_KR = os.path.join(BASE_DIR, "..", "assets", "fonts", "NotoSansKR-Bold.ttf")
FONT_SIZE    = 72

# Preload a fallback font
try:
    FONT = ImageFont.truetype(FONT_PATH_KR, FONT_SIZE)
except OSError:
    FONT = ImageFont.load_default()
    # 한글 렌더링이 어려울 수 있음
    print("⚠️ fallback font used; Korean may not render")

class WelcomeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        ch = self.bot.get_channel(config.WELCOME_CHANNEL_ID)
        await log_to_channel(self.bot, f"⚙️ 신규 회원 감지: {member} (ID: {member.id}); 채널 → {config.WELCOME_CHANNEL_ID}")
        if not ch:
            await log_to_channel(self.bot, "❌ 환영 채널을 찾을 수 없습니다. WELCOME_CHANNEL_ID 확인 필요")
            return

        # 1) generate image buffer
        try:
            await log_to_channel(self.bot, "🔧 [welcome] 환영 카드 생성 중…")
            card_buf = await self.make_welcome_card(member)
            await log_to_channel(self.bot, "✅ [welcome] 환영 카드 생성 완료")
        except Exception as e:
            traceback.print_exc()
            await log_to_channel(self.bot, f"❌ [welcome] 환영 카드 생성 실패: {e}")
            return await ch.send(f"⚠️ 환영 카드 생성 실패: {e}")

        # 2) wrap in File
        try:
            await log_to_channel(self.bot, "🔧 [welcome] File 래핑 생성 중…")
            file = File(card_buf, filename="welcome.png")
            await log_to_channel(self.bot, "✅ [welcome] File 생성 완료")
        except Exception as e:
            traceback.print_exc()
            await log_to_channel(self.bot, f"❌ [welcome] File 생성 실패: {e}")
            return

        # 3) build full embed
        try:
            await log_to_channel(self.bot, "🔧 [welcome] 임베드 빌드 중…")
            embed = discord.Embed(
                title=f"{member.display_name}님, 환영합니다!",
                description="스튜디오에서 새로운 시작을 함께해요!",
                color=discord.Color.green()
            )
            embed.add_field(
                name="1️⃣ 서버 규칙을 확인하고 숙지해 주세요!",
                value=f" • <#{config.RULES_CHANNEL_ID}>",
                inline=False
            )
            embed.add_field(
                name="2️⃣ 역할지급 채널에서 원하는 역할을 선택해 주세요!",
                value=f" • <#{config.ROLE_ASSIGN_CHANNEL_ID}>",
                inline=False
            )
            embed.add_field(
                name="3️⃣ 최신 공지사항을 놓치지 마세요!",
                value=f" • <#{config.ANNOUNCEMENTS_CHANNEL_ID}>",
                inline=False
            )
            embed.set_image(url="attachment://welcome.png")
            await log_to_channel(self.bot, "✅ [welcome] 임베드 빌드 완료")
        except Exception as e:
            traceback.print_exc()
            await log_to_channel(self.bot, f"❌ [welcome] 임베드 빌드 실패: {e}")
            return

        # 4) send it
        try:
            await log_to_channel(self.bot, "🔧 [welcome] 환영 메시지 전송 중…")
            await ch.send(
                content=member.mention,
                embed=embed,
                file=file,
                allowed_mentions=discord.AllowedMentions(users=True)
            )
            await log_to_channel(self.bot, "✅ [welcome] 환영 메시지 전송 완료")
        except Exception as e:
            traceback.print_exc()
            await log_to_channel(self.bot, f"❌ [welcome] 환영 메시지 전송 실패: {e}")

    async def make_welcome_card(self, member: discord.Member) -> BytesIO:
        # open background
        bg = Image.open(BG_PATH).convert("RGBA")
        draw = ImageDraw.Draw(bg)

        # fetch avatar (with timeout)
        avatar_asset = member.display_avatar.with_size(128).with_format("png")
        try:
            avatar_bytes = await asyncio.wait_for(avatar_asset.read(), timeout=5)
        except Exception as e:
            await log_to_channel(self.bot, f"❌ [welcome] 아바타 가져오기 실패: {e}")
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
            await log_to_channel(self.bot, "❌ 작별 채널을 찾을 수 없습니다. LEAVE_CHANNEL_ID 확인 필요")
            return

        embed = discord.Embed(
            title="회원 퇴장",
            description=f"**{member}**님이 서버를 떠났습니다.",
            color=discord.Color.dark_grey(),
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await log_to_channel(self.bot, f"👋 {member.display_name}님이 서버를 떠났습니다.")
        await ch.send(embed=embed)

async def setup(bot):
    await bot.add_cog(WelcomeCog(bot))
