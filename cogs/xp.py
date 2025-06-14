# cogs/xp.py

import discord
import re
import pytz
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from utils import config
from utils.logger import log_to_channel
from utils.henrik import henrik_get

# ─── XP SETTINGS ────────────────────────────────────
VOICE_XP_PER_MIN     = 1
DAILY_BONUS          = 200
BASE_XP_PER_LEVEL    = 100
INCREMENT_PER_LEVEL  = 20

def xp_to_next_level(level: int) -> int:
    return BASE_XP_PER_LEVEL + level * INCREMENT_PER_LEVEL

voice_session_starts: dict[int, datetime] = {}

class DailyXPView(View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="오늘의 XP 받기", style=discord.ButtonStyle.primary, custom_id="dailyxp_button")
    async def dailyxp_button(self, interaction: discord.Interaction, button: Button):
        print("Button pressed (coins/xp)")

        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        now_utc = datetime.now(timezone.utc)

        # Use pytz for America/New_York
        eastern = pytz.timezone("America/New_York")
        now_eastern = now_utc.astimezone(eastern)
        today_et = now_eastern.date()

        # 2) fetch last_claim
        try:
            row = await self.bot.db.fetchrow(
                "SELECT last_claim FROM daily_claim WHERE user_id = $1",
                user.id
            )
        except Exception as e:
            await log_to_channel(self.bot, f"[DailyXP] DB 조회 오류: {e}")
            return await interaction.followup.send("❌ 데이터베이스 오류가 발생했습니다.", ephemeral=True)

        # 3) if claimed already today (ET), deny until next ET midnight
        if row:
            last_utc = row["last_claim"]
            last_et_date = last_utc.astimezone(eastern).date()
            if last_et_date == today_et:
                # Use proper timezone-aware datetime and replace()
                next_midnight_et = (now_eastern + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                delta = next_midnight_et - now_eastern
                hrs, rem = divmod(int(delta.total_seconds()), 3600)
                mins = rem // 60
                return await interaction.followup.send(
                    f"⏳ 이미 오늘의 보상을 받으셨습니다. 다음 보상은 `{hrs}시간 {mins}분` 후 자정(12AM 동부 시간)에 리셋됩니다.",
                    ephemeral=True
                )

        # 4) grant bonus & record claim
        try:
            await grant_xp(self.bot, user, DAILY_BONUS)
            await self.bot.db.execute(
                """
                INSERT INTO daily_claim (user_id, last_claim)
                VALUES ($1, $2) ON CONFLICT (user_id) DO
                UPDATE
                    SET last_claim = EXCLUDED.last_claim
                """,
                user.id, now_utc
            )
        except Exception as e:
            await log_to_channel(self.bot, f"[DailyXP] 보상 처리 오류: {e}")
            return await interaction.followup.send("❌ 보상 처리 중 오류가 발생했습니다.", ephemeral=True)

        # 5) confirmation
        await interaction.followup.send(
            f"✅ 오늘의 **{DAILY_BONUS} XP** 보너스를 받았습니다!",
            ephemeral=True
        )
        await log_to_channel(self.bot, f"✅ {user.display_name}님이 오늘의 XP 보너스를 받았습니다.")

async def grant_xp(bot: commands.Bot, user: discord.Member, amount: int):
    try:
        # double if they hold the XP Booster role
        if discord.utils.get(user.roles, name="XP Booster"):
            amount *= 2

        row = await bot.db.fetchrow("SELECT xp, level FROM xp WHERE user_id = $1", user.id)
        xp, lvl = (row["xp"], row["level"]) if row else (0, 0)

        xp += amount
        needed = xp_to_next_level(lvl)
        if xp >= needed:
            xp -= needed
            lvl += 1
            chan = bot.get_channel(config.LEVELUP_CHANNEL_ID)
            if chan:
                await chan.send(f"🎉 {user.mention}, 레벨업! 지금 레벨 **{lvl}**입니다!")
            await log_to_channel(bot, f"🎉 {user.display_name}님 레벨업: {lvl}")

        await bot.db.execute(
            """
            INSERT INTO xp (user_id, xp, level)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
              SET xp    = EXCLUDED.xp,
                  level = EXCLUDED.level
            """,
            user.id, xp, lvl
        )

        xp_cog = bot.get_cog("XPSystem")
        if xp_cog:
            await xp_cog.refresh_leaderboard()
    except Exception as e:
        await log_to_channel(bot, f"[grant_xp] 오류: {e}")

class XPSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._xp_setup_done = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._xp_setup_done:
            return
        self._xp_setup_done = True

        xp_ch = self.bot.get_channel(config.XP_CHANNEL_ID)
        if not xp_ch:
            print(f"[XPSystem] Invalid XP_CHANNEL_ID: {config.XP_CHANNEL_ID}")
            await log_to_channel(self.bot, f"[XPSystem] 채널을 찾을 수 없습니다: {config.XP_CHANNEL_ID}")
            return

        # Clear old messages
        try:
            await xp_ch.purge(limit=None)
            print("[XPSystem] 이전 메시지 정리 완료")
        except Exception as e:
            await log_to_channel(self.bot, f"[XPSystem] 메시지 정리 실패: {e}")

        # Leaderboard: send and save message ID
        try:
            lb_embed = await self.build_leaderboard_embed()
            lb_msg   = await xp_ch.send(embed=lb_embed)
            config.LEADERBOARD_MESSAGE_ID = lb_msg.id
            print(f"[XPSystem] 리더보드 전송 완료 (ID={lb_msg.id})")
            await log_to_channel(self.bot, "✅ XP 리더보드 게시됨")
        except Exception as e:
            await log_to_channel(self.bot, f"[XPSystem] 리더보드 전송 오류: {e}")

        # Daily XP Button: send and save message ID
        try:
            xp_embed = discord.Embed(
                title="🎁 오늘의 XP 받기",
                description="아래 버튼을 눌러 오늘의 보너스 XP를 받으세요!",
                color=discord.Color.gold()
            )
            view   = DailyXPView(self.bot)
            xp_msg = await xp_ch.send(embed=xp_embed, view=view)
            config.DAILY_XP_MESSAGE_ID = xp_msg.id
            print(f"[XPSystem] Daily XP 버튼 게시 완료 (ID={xp_msg.id})")
            await log_to_channel(self.bot, "✅ Daily XP 버튼 게시됨")
        except Exception as e:
            await log_to_channel(self.bot, f"[XPSystem] Daily XP 버튼 전송 오류: {e}")

    async def build_leaderboard_embed(self) -> discord.Embed:
        try:
            rows = await self.bot.db.fetch(
                "SELECT user_id, xp, level FROM xp ORDER BY level DESC, xp DESC LIMIT 10"
            )
        except Exception as e:
            await log_to_channel(self.bot, f"[XPSystem] 리더보드 조회 오류: {e}")
            rows = []

        embed = discord.Embed(
            title="🏆 XP 리더보드 (Top 10)",
            color=discord.Color.gold()
        )
        if not rows:
            embed.description = "아직 아무도 XP를 획득하지 않았습니다."
        else:
            lines = []
            for idx, r in enumerate(rows, start=1):
                uid, xp_val, lvl = r["user_id"], r["xp"], r["level"]
                needed = xp_to_next_level(lvl)
                lines.append(f"**{idx}.** <@{uid}> — 레벨 {lvl} ({xp_val}/{needed} XP)")
            embed.description = "\n".join(lines)
        embed.set_footer(text=f"업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return embed

    async def refresh_leaderboard(self):
        chan = self.bot.get_channel(config.XP_CHANNEL_ID)
        if not chan:
            return
        try:
            embed = await self.build_leaderboard_embed()
            msg = await chan.fetch_message(config.LEADERBOARD_MESSAGE_ID)
            await msg.edit(embed=embed)
            print(f"[XPSystem] 리더보드 업데이트 완료 (ID={config.LEADERBOARD_MESSAGE_ID})")
        except discord.NotFound:
            sent = await chan.send(embed=embed)
            config.LEADERBOARD_MESSAGE_ID = sent.id
            print(f"[XPSystem] 리더보드 메시지를 찾을 수 없어 새로 보냄 (ID={sent.id})")
        except Exception as e:
            await log_to_channel(self.bot, f"[XPSystem] 리더보드 업데이트 오류: {e}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        now = datetime.now(timezone.utc)

        # 음성 채널에서 나갈 때
        if before.channel and (not after.channel or after.channel.id != before.channel.id):
            start = voice_session_starts.pop(member.id, None)
            if start:
                minutes = int((now - start).total_seconds() // 60)
                if minutes > 0:
                    earned = minutes * VOICE_XP_PER_MIN
                    await log_to_channel(
                        self.bot,
                        f"🗣️ {member.display_name}님이 음성 {minutes}분 → {earned} XP 획득"
                    )
                    await grant_xp(self.bot, member, earned)

        # 음성 채널에 입장할 때
        if after.channel and (not before.channel or before.channel.id != after.channel.id):
            voice_session_starts[member.id] = now

    @app_commands.command(name="dailyxp", description="매일 한 번 XP 보너스를 받습니다.")
    async def dailyxp(self, interaction: discord.Interaction):
        view = DailyXPView(self.bot)
        await interaction.response.send_message(
            f"🎁 {interaction.user.mention}, 아래 버튼을 눌러 오늘의 XP를 받으세요!",
            view=view,
            ephemeral=True
        )

    @app_commands.command(
        name="xp_modify",
        description="관리자가 여러 사용자의 XP를 추가/제거/설정합니다."
    )
    @app_commands.describe(
        users="공백으로 구분된 멘션 (예: @User1 @User2)",
        action="add: 추가, remove: 제거, set: 설정",
        amount="적용할 XP 양"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="추가",  value="add"),
        app_commands.Choice(name="제거", value="remove"),
        app_commands.Choice(name="설정", value="set"),
    ])
    async def xp_modify(
        self,
        interaction: discord.Interaction,
        users: str,
        action: app_commands.Choice[str],
        amount: int
    ):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ 이 명령을 사용할 권한이 없습니다.", ephemeral=True
            )
        # 즉시 defer 처리하여 타임아웃 방지
        await interaction.response.defer(ephemeral=True)

        # extract up to 5 user IDs from mention string
        ids = re.findall(r"<@!?(\d+)>", users)[:5]
        members = []
        for uid in ids:
            try:
                m = await interaction.guild.fetch_member(int(uid))
                members.append(m)
            except discord.NotFound:
                pass

        if not members:
            return await interaction.followup.send(
                "❌ 올바른 멘션을 입력해주세요. (최대 5명)", ephemeral=True
            )

        summary = []
        for m in members:
            try:
                row = await self.bot.db.fetchrow(
                    "SELECT xp, level FROM xp WHERE user_id = $1", m.id
                )
                old_xp, lvl = (row["xp"], row["level"]) if row else (0, 0)
            except Exception as e:
                await log_to_channel(self.bot, f"[xp_modify] DB 조회 오류: {e}")
                old_xp, lvl = 0, 0

            if action.value == "add":
                new_xp = old_xp + amount
                delta  = amount
            elif action.value == "remove":
                new_xp = max(0, old_xp - amount)
                delta  = new_xp - old_xp
            else:
                new_xp = max(0, amount)
                delta  = new_xp - old_xp

            try:
                await self.bot.db.execute(
                    """
                    INSERT INTO xp (user_id, xp, level)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id) DO UPDATE
                      SET xp    = EXCLUDED.xp,
                          level = EXCLUDED.level
                    """,
                    m.id, new_xp, lvl
                )
                sign = "+" if delta > 0 else ""
                summary.append(f"{m.mention}: {sign}{delta} XP ({old_xp} → {new_xp})")
                await log_to_channel(
                    self.bot,
                    f"🛠️ {interaction.user.display_name}님이 {m.display_name}님의 XP를 "
                    f"{old_xp} → {new_xp}로 {action.name}했습니다."
                )
            except Exception as e:
                await log_to_channel(self.bot, f"[xp_modify] DB 업데이트 오류: {e}")
                summary.append(f"{m.mention}: 오류 발생 ({e})")

        await self.refresh_leaderboard()

        embed = discord.Embed(
            title="🛠️ XP 수정 결과",
            description="\n".join(summary),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="xp",
        description="내 XP, 레벨, 그리고 리더보드 순위를 확인합니다."
    )
    async def xp(self, interaction: discord.Interaction):
        user = interaction.user
        try:
            row = await self.bot.db.fetchrow(
                "SELECT xp, level FROM xp WHERE user_id = $1", user.id
            )
            current_xp, level = (row["xp"], row["level"]) if row else (0, 0)
        except Exception as e:
            await log_to_channel(self.bot, f"[xp] DB 조회 오류: {e}")
            current_xp, level = 0, 0

        needed = xp_to_next_level(level)

        try:
            rows = await self.bot.db.fetch(
                "SELECT user_id FROM xp ORDER BY level DESC, xp DESC"
            )
            rank = next(
                (i for i, r in enumerate(rows, start=1) if r["user_id"] == user.id),
                len(rows) + 1
            )
        except Exception as e:
            await log_to_channel(self.bot, f"[xp] 리더보드 순위 조회 오류: {e}")
            rank = 0

        embed = discord.Embed(
            title=f"{user.display_name}님의 XP 정보",
            color=discord.Color.blurple()
        )
        embed.add_field(name="레벨", value=str(level), inline=True)
        embed.add_field(name="XP", value=f"{current_xp} / {needed}", inline=True)
        embed.add_field(name="리더보드 순위", value=f"#{rank}", inline=True)
        embed.set_footer(text=f"업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        await interaction.response.send_message(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(XPSystem(bot))
