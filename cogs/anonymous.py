# cogs/anonymous.py new

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
        # 1) ignore bots
        if message.author.bot:
            return
        # 2) only handle DMs
        if not isinstance(message.channel, discord.DMChannel):
            return

        board  = self.bot.get_channel(self.board_channel_id)
        log_ch = self.bot.get_channel(self.log_channel_id)

        # 3) file validation
        files = []
        for att in message.attachments:
            if att.size > MAX_FILE_SIZE:
                await message.channel.send(
                    "⚠️ 파일 크기가 50MiB를 초과하여 업로드할 수 없습니다."
                )
                return
            name = att.filename.lower()
            if "." not in name:
                await message.channel.send("⚠️ 파일 확장자가 없습니다. 업로드 불가.")
                return
            ext = name.rsplit(".", 1)[1]
            if ext not in ALLOWED_EXT:
                await message.channel.send(
                    f"⚠️ .{ext} 파일은 허용되지 않습니다. 허용: {', '.join(sorted(ALLOWED_EXT))}"
                )
                return
            files.append(await att.to_file())

        # 4) prepare content
        content = message.content or "​"

        # 5) send anonymous post (placeholder footer)
        embed = discord.Embed(
            description=content,
            color=discord.Color.blurple(),
            timestamp=message.created_at
        )
        embed.set_author(name="익명 게시글")
        if files and files[0].filename.lower().endswith(("png","jpg","jpeg","gif")):
            embed.set_image(url=f"attachment://{files[0].filename}")
        embed.set_footer(text="참조번호: 준비중…")

        post = await board.send(embed=embed, files=files)

        # 6) update footer with real ID
        embed.set_footer(text=f"참조번호: {post.id}")
        await post.edit(embed=embed)

        # 7) DM admin with author & ref
        admin = self.bot.get_user(config.ADMIN_USER_ID)
        if admin:
            await admin.send(
                f"📩 익명 게시물 등록 알림\n"
                f"작성자: {message.author} (`{message.author.id}`)\n"
                f"참조번호: {post.id}"
            )

        # 8) log minimal info to 운영진-로그 without author
        if log_ch:
            await log_ch.send(
                f"📩 익명 게시물 등록됨\n"
                f"참조번호: {post.id}\n"
                f"내용: {content}\n"
                f"첨부파일: {len(files)}개"
            )

        # 9) confirm to user
        await message.channel.send("✅ 게시물이 익명 게시판에 등록되었습니다. 감사합니다!")

async def setup(bot: commands.Bot):
    await bot.add_cog(AnonymousBoard(bot))
