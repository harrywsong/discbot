# cogs/xp.py

import discord
import re
import pytz
import asyncio
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
from datetime import datetime, timedelta, timezone
from utils import config
from utils.logger import log_to_channel

# ─── XP SETTINGS ────────────────────────────────────
VOICE_XP_PER_MIN = 1
DAILY_BONUS = 200
BASE_XP_PER_LEVEL = 100
INCREMENT_PER_LEVEL = 20

# Database table creation SQL
CREATE_XP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS xp (
    user_id BIGINT PRIMARY KEY,
    xp INTEGER NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 0,
    last_active TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_DAILY_CLAIM_TABLE_SQL = """
                               CREATE TABLE IF NOT EXISTS daily_claim \
                               ( \
                                   user_id \
                                   BIGINT \
                                   PRIMARY \
                                   KEY, \
                                   last_claim \
                                   TIMESTAMPTZ \
                                   NOT \
                                   NULL
                               ); \
                               """


def xp_to_next_level(level: int) -> int:
    return BASE_XP_PER_LEVEL + level * INCREMENT_PER_LEVEL


voice_session_starts: dict[int, datetime] = {}


class LeaderboardView(View):
    def __init__(self, cog, page=0, per_page=10):
        super().__init__(timeout=None)
        self.cog = cog
        self.page = page
        self.per_page = per_page

    async def update_embed(self, interaction: discord.Interaction):
        embed = await self.cog.build_leaderboard_embed(page=self.page, per_page=self.per_page)
        await interaction.response.edit_message(
            embed=embed,
            view=LeaderboardView(self.cog, page=self.page, per_page=self.per_page)
        )

    # In LeaderboardView class, modify the navigation buttons:
    @discord.ui.button(label="⏮️", style=discord.ButtonStyle.secondary, custom_id="prev_page")
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await self.update_embed(interaction)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.secondary, custom_id="next_page")
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        total_count = await self.cog.bot.db.fetchval("SELECT COUNT(*) FROM xp")
        max_page = (total_count - 1) // self.per_page
        if self.page < max_page:
            self.page += 1
            await self.update_embed(interaction)
        else:
            await interaction.response.defer()


class DailyXPView(View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="오늘의 XP 받기", style=discord.ButtonStyle.primary, custom_id="dailyxp_button")
    async def dailyxp_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        now_utc = datetime.now(timezone.utc)

        eastern = pytz.timezone("America/New_York")
        now_eastern = now_utc.astimezone(eastern)
        today_et = now_eastern.date()

        try:
            row = await self.bot.db.fetchrow(
                "SELECT last_claim FROM daily_claim WHERE user_id = $1",
                user.id
            )
        except Exception as e:
            await log_to_channel(self.bot, f"[DailyXP] DB 조회 오류: {e}")
            return await interaction.followup.send("❌ 데이터베이스 오류가 발생했습니다.", ephemeral=True)

        if row:
            last_utc = row["last_claim"]
            last_et_date = last_utc.astimezone(eastern).date()
            if last_et_date == today_et:
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

        try:
            xp_cog = self.bot.get_cog("XPSystem")
            if xp_cog:
                await xp_cog.grant_xp(user, DAILY_BONUS, force_leaderboard=False)

            await self.bot.db.execute(
                """
                INSERT INTO daily_claim (user_id, last_claim)
                VALUES ($1, $2) ON CONFLICT (user_id) DO
                UPDATE SET last_claim = EXCLUDED.last_claim
                """,
                user.id, now_utc
            )

            await interaction.followup.send(
                f"✅ 오늘의 **{DAILY_BONUS} XP** 보너스를 받았습니다!",
                ephemeral=True
            )
            await log_to_channel(self.bot, f"✅ {user.display_name}님이 오늘의 XP 보너스를 받았습니다.")

        except Exception as e:
            await log_to_channel(self.bot, f"[DailyXP] 보상 처리 오류: {e}")
            await interaction.followup.send("❌ 보상 처리 중 오류가 발생했습니다.", ephemeral=True)


class XPSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._xp_setup_done = False
        self._last_leaderboard_update = None
        self._leaderboard_cache = None
        self._update_lock = asyncio.Lock()
        self._backoff_time = 5
        self._leaderboard_message = None

    @commands.Cog.listener()
    async def on_ready(self):
        if self._xp_setup_done:
            return
        self._xp_setup_done = True

        async with self.bot.db.acquire() as conn:
            await conn.execute(CREATE_XP_TABLE_SQL)
            await conn.execute(CREATE_DAILY_CLAIM_TABLE_SQL)

        xp_ch = self.bot.get_channel(config.XP_CHANNEL_ID)
        if not xp_ch:
            print(f"[XPSystem] Invalid XP_CHANNEL_ID: {config.XP_CHANNEL_ID}")
            await log_to_channel(self.bot, f"[XPSystem] 채널을 찾을 수 없습니다: {config.XP_CHANNEL_ID}")
            return

        try:
            await xp_ch.purge(limit=10)
            print("[XPSystem] 이전 메시지 정리 완료")
        except Exception as e:
            await log_to_channel(self.bot, f"[XPSystem] 메시지 정리 실패: {e}")

        try:
            lb_embed = await self.build_leaderboard_embed()
            self._leaderboard_message = await xp_ch.send(embed=lb_embed, view=LeaderboardView(self))
            config.LEADERBOARD_MESSAGE_ID = self._leaderboard_message.id
            print(f"[XPSystem] 리더보드 전송 완료 (ID={self._leaderboard_message.id})")
            await log_to_channel(self.bot, "✅ XP 리더보드 게시됨")
        except Exception as e:
            await log_to_channel(self.bot, f"[XPSystem] 리더보드 전송 오류: {e}")

        try:
            xp_embed = discord.Embed(
                title="🎁 오늘의 XP 받기",
                description="아래 버튼을 눌러 오늘의 보너스 XP를 받으세요!",
                color=discord.Color.gold()
            )
            view = DailyXPView(self.bot)
            xp_msg = await xp_ch.send(embed=xp_embed, view=view)
            config.DAILY_XP_MESSAGE_ID = xp_msg.id
            print(f"[XPSystem] Daily XP 버튼 게시 완료 (ID={xp_msg.id})")
            await log_to_channel(self.bot, "✅ Daily XP 버튼 게시됨")
        except Exception as e:
            await log_to_channel(self.bot, f"[XPSystem] Daily XP 버튼 전송 오류: {e}")

    async def build_leaderboard_embed(self, page=0, per_page=10) -> discord.Embed:
        offset = page * per_page
        try:
            total_count = await self.bot.db.fetchval("SELECT COUNT(*) FROM xp")
            rows = await self.bot.db.fetch(
                "SELECT user_id, xp, level FROM xp ORDER BY level DESC, xp DESC LIMIT $1 OFFSET $2",
                per_page, offset
            )
        except Exception as e:
            await log_to_channel(self.bot, f"[XPSystem] 리더보드 조회 오류: {e}")
            rows = []

        embed = discord.Embed(
            title=f"🏆 XP 리더보드 (Top {offset + 1}-{offset + len(rows)})",
            color=discord.Color.gold()
        )

        if not rows:
            embed.description = "아직 아무도 XP를 획득하지 않았습니다."
        else:
            lines = []
            for idx, r in enumerate(rows, start=offset + 1):
                uid, xp_val, lvl = r["user_id"], r["xp"], r["level"]
                needed = xp_to_next_level(lvl)
                lines.append(f"**{idx}.** <@{uid}> — 레벨 {lvl} ({xp_val}/{needed} XP)")
            embed.description = "\n".join(lines)

        max_page = max(0, (total_count - 1) // per_page)
        embed.set_footer(text=f"페이지 {page + 1}/{max_page + 1} | 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return embed

    async def refresh_leaderboard(self, force=False):
        if self._update_lock.locked():
            return

        async with self._update_lock:
            try:
                current_time = datetime.now(timezone.utc)
                if not force and self._last_leaderboard_update and \
                        (current_time - self._last_leaderboard_update).total_seconds() < 300:
                    return

                chan = self.bot.get_channel(config.XP_CHANNEL_ID)
                if not chan:
                    return

                embed = await self.build_leaderboard_embed()

                if not force and self._leaderboard_cache and self._leaderboard_cache == embed.description:
                    return

                self._leaderboard_cache = embed.description

                if self._leaderboard_message:
                    try:
                        await self._leaderboard_message.edit(embed=embed, view=LeaderboardView(self))
                        self._last_leaderboard_update = current_time
                        return
                    except discord.NotFound:
                        self._leaderboard_message = None
                    except discord.HTTPException as e:
                        if e.status == 429 or e.code == 30046:
                            try:
                                # Clear last 10 messages
                                await chan.purge(limit=10)
                                await log_to_channel(self.bot, "♻️ Rate limit hit - cleared old messages")

                                # Create fresh leaderboard
                                self._leaderboard_message = await chan.send(
                                    embed=embed,
                                    view=LeaderboardView(self)
                                )
                                config.LEADERBOARD_MESSAGE_ID = self._leaderboard_message.id
                                self._last_leaderboard_update = datetime.now(timezone.utc)
                                return
                            except Exception as purge_error:
                                await log_to_channel(self.bot, f"⚠️ Failed to purge messages: {purge_error}")
                                raise e

                            await asyncio.sleep(self._backoff_time)
                            self._backoff_time = min(60, self._backoff_time * 2)
                            self._leaderboard_message = await chan.send(
                                embed=embed,
                                view=LeaderboardView(self)
                            )
                            config.LEADERBOARD_MESSAGE_ID = self._leaderboard_message.id
                            self._last_leaderboard_update = current_time
                            return

                # Create new message if needed
                self._leaderboard_message = await chan.send(
                    embed=embed,
                    view=LeaderboardView(self)
                )
                config.LEADERBOARD_MESSAGE_ID = self._leaderboard_message.id
                self._last_leaderboard_update = current_time
                self._backoff_time = 5

            except Exception as e:
                await log_to_channel(self.bot, f"[XPSystem] 리더보드 업데이트 오류: {e}")
                await asyncio.sleep(self._backoff_time)
                self._backoff_time = min(60, self._backoff_time * 2)

    async def grant_xp(self, user: discord.Member, amount: int, force_leaderboard=True):
        try:
            if discord.utils.get(user.roles, name="XP Booster"):
                amount *= 2

            async with self.bot.db.acquire() as conn:
                async with conn.transaction():  # Add transaction
                    row = await conn.fetchrow(
                        "SELECT xp, level FROM xp WHERE user_id = $1 FOR UPDATE",
                        user.id
                    )
                xp, lvl = (row["xp"], row["level"]) if row else (0, 0)

                xp += amount
                needed = xp_to_next_level(lvl)
                level_up = False

                if xp >= needed:
                    xp -= needed
                    lvl += 1
                    level_up = True
                    chan = self.bot.get_channel(config.LEVELUP_CHANNEL_ID)
                    if chan:
                        await chan.send(f"🎉 {user.mention}, 레벨업! 지금 레벨 **{lvl}**입니다!")
                    await log_to_channel(self.bot, f"🎉 {user.display_name}님 레벨업: {lvl}")

                await conn.execute(
                    """
                    INSERT INTO xp (user_id, xp, level, last_active)
                    VALUES ($1, $2, $3, NOW()) ON CONFLICT (user_id) DO
                    UPDATE
                        SET xp = EXCLUDED.xp,
                        level = EXCLUDED.level,
                        last_active = EXCLUDED.last_active
                    """,
                    user.id, xp, lvl
                )

                if force_leaderboard or level_up or amount >= 50:
                    await self.refresh_leaderboard()

        except Exception as e:
            await log_to_channel(self.bot, f"[grant_xp] 오류: {e}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        now = datetime.now(timezone.utc)

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
                    await self.grant_xp(member, earned, force_leaderboard=False)

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
