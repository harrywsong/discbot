# cogs/tickets.py new

import discord
from discord.ext import commands
from discord import app_commands, File
from discord.ui import View, Button
from datetime import datetime, timezone
from io import BytesIO
import base64
import html

from utils import config
from utils.logger import log_to_channel

class HelpView(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="문의하기", style=discord.ButtonStyle.primary, custom_id="open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: Button):
        guild  = interaction.guild
        member = interaction.user
        cat    = guild.get_channel(config.TICKET_CATEGORY_ID)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
            guild.get_role(config.SUPPORT_ROLE_ID): discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }

        # Prevent duplicate ticket
        existing = discord.utils.get(cat.text_channels, name=f"ticket-{member.id}")
        if existing:
            return await interaction.response.send_message(
                f"❗ 이미 열린 티켓이 있습니다: {existing.mention}", ephemeral=True
            )

        ticket_chan = await cat.create_text_channel(f"ticket-{member.id}", overwrites=overwrites)
        await interaction.response.send_message(
            f"✅ 티켓 채널이 생성되었습니다: {ticket_chan.mention}", ephemeral=True
        )

        embed = discord.Embed(
            title="🎫 새 티켓 생성됨",
            description=f"{member.mention}님의 문의입니다.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="생성자", value=f"{member} | {member.id}", inline=False)
        embed.add_field(name="티켓 채널", value=ticket_chan.mention, inline=False)
        await ticket_chan.send(embed=embed, view=CloseTicketView(self.bot))

        await log_to_channel(self.bot, f"{member}님이 `{ticket_chan.name}` 티켓을 생성했습니다.")


class CloseTicketView(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="티켓 닫기", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: Button):
        channel   = interaction.channel
        owner_id  = int(channel.name.split("-", 1)[1])
        ticket_owner = channel.guild.get_member(owner_id)
        is_owner  = interaction.user.id == owner_id
        has_sup   = config.SUPPORT_ROLE_ID in [r.id for r in interaction.user.roles]

        if not (is_owner or has_sup):
            return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)

        await interaction.response.send_message("⏳ 티켓을 닫는 중입니다...", ephemeral=True)

        # 1) Metadata
        created_ts = channel.created_at.strftime("%Y-%m-%d %H:%M UTC")

        # 2) Fetch messages
        msgs = [m async for m in channel.history(limit=100, oldest_first=True)]

        # 3) Chat‑bubble CSS & Layout
        css = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap');

body {
  margin: 0;
  padding: 20px;
  background: #1F1F23;
  color: #E1E1E6;
  font-family: 'Inter', sans-serif;
}
.container {
  max-width: 800px;
  margin: 0 auto;
}
.header {
  text-align: center;
  margin-bottom: 30px;
}
.header h1 {
  margin: 0;
  color: #FFD369;
  font-size: 2.4em;
}
.header .meta {
  font-size: 0.9em;
  color: #A3A3A3;
  margin-top: 8px;
}
.messages {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.msg {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.avatar {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  flex-shrink: 0;
}
.bubble {
  background: #2C2C33;
  border-radius: 14px;
  padding: 14px 18px;
  position: relative;
  box-shadow: 0 4px 8px rgba(0,0,0,0.2);
  max-width: calc(100% - 60px);
}
.bubble::before {
  content: '';
  position: absolute;
  top: 16px;
  left: -8px;
  border-width: 8px 8px 8px 0;
  border-style: solid;
  border-color: transparent #2C2C33 transparent transparent;
}
.username {
  font-weight: 600;
  color: #FFFFFF;
}
.timestamp {
  font-size: 0.8em;
  color: #8B8B8B;
  margin-left: 10px;
}
.text {
  margin-top: 8px;
  line-height: 1.5;
  white-space: pre-wrap;
}
img.attachment {
  max-width: 100%;
  border-radius: 8px;
  margin-top: 12px;
  box-shadow: 0 4px 8px rgba(0,0,0,0.2);
}
.footer {
  text-align: center;
  margin-top: 40px;
  font-size: 0.8em;
  color: #7A7A7A;
}
"""

        # 4) Build each message bubble
        messages_html = ""
        for m in msgs:
            when    = m.created_at.strftime("%Y-%m-%d %H:%M")
            name    = html.escape(m.author.display_name)
            content = html.escape(m.content or "")
            avatar  = m.author.avatar.url if m.author.avatar else ""

            messages_html += f"""
<div class="msg">
  <img class="avatar" src="{avatar}" alt="avatar">
  <div class="bubble">
    <span class="username">{name}</span>
    <span class="timestamp">{when}</span>
    <div class="text">{content}</div>
"""

            # inline attachments
            for att in m.attachments:
                b64  = base64.b64encode(await att.read()).decode("ascii")
                ctype = att.content_type or "image/png"
                messages_html += f"""
    <img class="attachment" src="data:{ctype};base64,{b64}" alt="{att.filename}">
"""

            messages_html += "  </div>\n</div>"

        # 5) Assemble full HTML
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        html_doc = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>{css}</style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Transcript for {channel.name}</h1>
      <p class="meta">Created: {created_ts} • Owner: {ticket_owner}</p>
    </div>
    <div class="messages">
      {messages_html}
    </div>
    <div class="footer">Generated by {self.bot.user.name} on {now_utc}</div>
  </div>
</body>
</html>
""".strip()

        buf = BytesIO(html_doc.encode("utf-8"))
        buf.seek(0)

        close_embed = discord.Embed(
            title="🎫 티켓 닫힘",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        close_embed.add_field(name="티켓",         value=channel.name, inline=False)
        close_embed.add_field(name="생성자",       value=str(ticket_owner), inline=False)
        close_embed.add_field(name="닫은 사람",    value=str(interaction.user), inline=False)

        history_ch = channel.guild.get_channel(config.HISTORY_CHANNEL_ID)
        if history_ch:
            await history_ch.send(embed=close_embed, file=File(buf, filename=f"{channel.name}.html"))
        else:
            await log_to_channel(self.bot, "⚠️ HISTORY 채널을 찾을 수 없습니다.")

        await channel.delete(reason="티켓 종료")


class TicketSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="운영진에게 문의할 수 있는 티켓을 엽니다.")
    async def slash_help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="문의 사항이 있으신가요?",
            description=(
                "아래 '문의하기' 버튼을 눌러주세요.\n"
                "개별 티켓 채널이 생성되어 운영진이 도움을 드립니다."
            ),
            color=discord.Color.teal()
        )
        embed.set_footer(text=f"{self.bot.user.name} | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        await interaction.response.send_message(embed=embed, view=HelpView(self.bot), ephemeral=False)


async def setup(bot):
    await bot.add_cog(TicketSystem(bot))
