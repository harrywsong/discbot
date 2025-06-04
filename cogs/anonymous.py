# cogs/anonymous.py

import discord
from discord.ext import commands
from utils import config
from utils.logger import log_to_channel
from utils.henrik import henrik_get

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MiB
ALLOWED_EXT = {
    "png", "jpg", "jpeg", "gif",
    "mp4", "mov", "mp3", "wav",
    "pdf", "txt"
}

class AnonymousBoard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.board_channel_id = config.ANON_BOARD_CHANNEL_ID
        self.log_channel_id   = config.ANON_LOG_CHANNEL_ID

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 1) 봇 메시지 무시
        if message.author.bot:
            return
        # 2) DM 채널에서 온 메시지인지 확인
        if not isinstance(message.channel, discord.DMChannel):
            return

        board  = self.bot.get_channel(self.board_channel_id)
        log_ch = self.bot.get_channel(self.log_channel_id)

        # 3) 파일 유효성 검사
        files = []
        for att in message.attachments:
            if att.size > MAX_FILE_SIZE:
                await message.channel.send(
                    "⚠️ 파일 크기가 50MiB를 초과하여 업로드할 수 없습니다."
                )
                return
            name = att.filename.lower()
            if "." not in name:
                await message.channel.send("⚠️ 파일 확장자가 없습니다. 업로드할 수 없습니다.")
                return
            ext = name.rsplit(".", 1)[1]
            if ext not in ALLOWED_EXT:
                await message.channel.send(
                    f"⚠️ .{ext} 파일은 허용되지 않습니다. 허용 확장자: {', '.join(sorted(ALLOWED_EXT))}"
                )
                return
            files.append(await att.to_file())

        # 4) 본문 내용 준비
        content = message.content or "​"

        # 5) 익명 게시물 전송 (임시 참조번호)
        embed = discord.Embed(
            description=content,
            color=discord.Color.blurple(),
            timestamp=message.created_at
        )
        embed.set_author(name="익명 게시글")
        if files and files[0].filename.lower().endswith(("png", "jpg", "jpeg", "gif")):
            embed.set_image(url=f"attachment://{files[0].filename}")
        embed.set_footer(text="참조번호: 준비 중…")

        post = await board.send(embed=embed, files=files)

        # 6) 실제 참조번호로 푸터 업데이트
        embed.set_footer(text=f"참조번호: {post.id}")
        await post.edit(embed=embed)

        # 7) 관리자에게 작성자와 참조번호 DM 알림
        admin = self.bot.get_user(config.ADMIN_USER_ID)
        if admin:
            user_display = f"{message.author.display_name} ({message.author.id})"
            await admin.send(
                f"📩 [익명 게시판] 새 게시물이 등록되었습니다.\n"
                f"작성자: {user_display}\n"
                f"참조번호: {post.id}"
            )

        # 8) 운영진 로그 채널에 익명 게시물 관련 정보 전송
        if log_ch:
            await log_ch.send(
                f"📩 [익명 게시판] 새 게시물이 기록되었습니다.\n"
                f"참조번호: {post.id}\n"
                f"내용: {content}\n"
                f"첨부파일: {len(files)}개"
            )

        # 9) 작성자에게 확인 메시지 전송
        await message.channel.send("✅ 게시물이 익명 게시판에 등록되었습니다. 감사합니다!")

async def setup(bot: commands.Bot):
    await bot.add_cog(AnonymousBoard(bot))
