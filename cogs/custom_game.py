# cogs/custom_game.py new

import asyncio
import random
from typing import Optional
from utils.henrik import henrik_get

import pytz

import discord
from discord import app_commands, AllowedMentions
from discord.ext import commands
from datetime import datetime, timedelta, timezone

from utils import config
from utils.logger import log_to_channel

# ── Constants & Globals ─────────────────────────────────

MAPS = [
    "Ascent", "Haven", "Icebox", "Lotus", "Pearl", "Split", "Sunset"
]

#    "Bind", "Haven", "Split", "Ascent", "Icebox", "Breeze", "Fracture", "Pearl", "Lotus", "Sunset", "Abyss"

current_custom_game = None  # Tracks the active CustomGameView


def is_privileged(user: discord.Member, creator: discord.Member) -> bool:
    """True if the user is the creator or has any admin role."""
    if user == creator:
        return True
    return any(r.id in config.CUSTOM_GAME_ADMIN_ROLE_IDS for r in user.roles)


class FakeUser:
    """Represents a bot‐added placeholder player."""
    def __init__(self, name: str):
        self.name = name

    @property
    def mention(self) -> str:
        return f"`{self.name}`"

    @property
    def display_name(self) -> str:
        return self.name


# ── UI: Lobby View & Buttons ───────────────────────────

class CustomGameView(discord.ui.View):
    def __init__(
        self,
        creator: discord.Member,
        interaction: discord.Interaction,
        voice_channel: discord.VoiceChannel
    ):
        super().__init__(timeout=None)
        self.creator = creator
        self.interaction = interaction
        self.voice_channel = voice_channel
        self.participants: list[discord.Member | FakeUser] = []
        self.waitlist_open: bool = False
        self.waitlist: list[discord.Member] = []
        self.ping_schedule = {10: False, 5: False, 1: False}
        self.start_time: str | None = None
        self.voice_check_start: datetime | None = None
        self.voice_check_end: datetime | None = None
        self.lobby_message: discord.Message | None = None
        self.warning_task: asyncio.Task | None = None
        self.rebuild_buttons()

    def rebuild_buttons(self):
        self.clear_items()
        if len(self.participants) < 10:
            self.add_item(self.join_button)
        elif self.waitlist_open:
            self.add_item(self.waitlist_button)
        self.add_item(self.leave_button)
        if self.waitlist_open:
            self.add_item(self.waitlist_cancel_button)
        self.add_item(CancelCustomGameButton(self))

    def format_description(self) -> str:
        desc = (
            f"{self.creator.mention}님이 내전을 시작했습니다!\n\n"
            f"**시작 시간:**\n{self.start_time or '알 수 없음'}\n\n"
            f"**참가자 ({len(self.participants)}/10):**\n"
            f"{('*아직 아무도 없습니다.*' if not self.participants else chr(10).join(p.mention for p in self.participants))}"
        )
        if self.waitlist_open:
            desc += (
                "\n\n**대기열:**\n"
                f"{('*비어 있습니다.*' if not self.waitlist else chr(10).join(m.mention for m in self.waitlist))}"
            )
        return desc

    async def update_embed(self):
        assert self.lobby_message, "Lobby message not set"
        await self.lobby_message.edit(
            embed=discord.Embed(
                title="🕹️ 발로란트 커스텀",
                description=self.format_description(),
                color=discord.Color.green()
            ),
            view=self
        )

    @discord.ui.button(label="내전 참가", style=discord.ButtonStyle.success, custom_id="join")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user in self.participants:
            return await interaction.response.send_message("이미 참가하셨습니다!", ephemeral=True)
        if len(self.participants) >= 10:
            return await interaction.response.send_message(
                "가득 찼습니다. `/내전대기`로 대기열에 등록하세요.", ephemeral=True
            )

        self.participants.append(interaction.user)
        await log_to_channel(interaction.client, f"{interaction.user.display_name}님이 내전에 참가했습니다.")
        self.rebuild_buttons()
        await self.update_embed()
        await interaction.response.send_message("✅ 참가 완료!", ephemeral=True)

    @discord.ui.button(label="참가 취소", style=discord.ButtonStyle.danger, custom_id="leave")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in self.participants:
            return await interaction.response.send_message("참가 상태가 아닙니다.", ephemeral=True)

        self.participants.remove(interaction.user)
        await log_to_channel(interaction.client, f"{interaction.user.display_name}님이 내전 참가를 취소했습니다.")

        # Auto‑promote from waitlist
        if self.waitlist:
            next_up = self.waitlist.pop(0)
            self.participants.append(next_up)
            await interaction.channel.send(f"{next_up.mention}님, 빈 자리가 생겨 내전에 참가하셨습니다! 🎉")

        self.rebuild_buttons()
        await self.update_embed()
        await interaction.response.send_message("✅ 취소 완료!", ephemeral=True)

    @discord.ui.button(label="대기열 참가", style=discord.ButtonStyle.secondary, custom_id="waitlist_button")
    async def waitlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user in self.participants or interaction.user in self.waitlist:
            return await interaction.response.send_message("이미 참가 또는 대기열 중입니다.", ephemeral=True)
        self.waitlist.append(interaction.user)
        await self.update_embed()
        await interaction.response.send_message("✅ 대기열에 등록되었습니다.", ephemeral=True)

    @discord.ui.button(label="대기열 취소", style=discord.ButtonStyle.danger, custom_id="waitlist_cancel")
    async def waitlist_cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in self.waitlist:
            return await interaction.response.send_message("대기열에 없습니다.", ephemeral=True)
        self.waitlist.remove(interaction.user)
        self.rebuild_buttons()
        await self.update_embed()
        await interaction.response.send_message("✅ 대기열에서 빠졌습니다.", ephemeral=True)


class CancelCustomGameButton(discord.ui.Button):
    def __init__(self, parent: CustomGameView):
        super().__init__(label="내전 취소", style=discord.ButtonStyle.danger, custom_id="cancel_custom")
        self.parent = parent

    async def callback(self, interaction: discord.Interaction):
        if not is_privileged(interaction.user, self.parent.creator):
            return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)

        global current_custom_game
        await interaction.channel.purge(limit=100)
        await interaction.channel.send("✅ **내전이 취소되었습니다.**")
        await log_to_channel(interaction.client, f"{interaction.user.display_name}님이 내전을 취소했습니다.")
        if self.parent.warning_task:
            self.parent.warning_task.cancel()
        current_custom_game = None


# ── Cog Definition ─────────────────────────────────────

class CustomGame(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="내전시작",
        description="10인 내전을 시작합니다."
    )
    @app_commands.describe(
        time="시작 시간 (예: 22:00 또는 10:00 PM)",
        zone="시간대: est, cst, pst"
    )
    async def slash_start_custom(
        self,
        interaction: discord.Interaction,
        time: str,
        zone: str
    ):
        # 1) Pre‑validate timezone
        tz_map = {"est": "US/Eastern", "cst": "US/Central", "pst": "US/Pacific"}
        if zone.lower() not in tz_map:
            return await interaction.response.send_message(
                "❌ 지원되지 않는 시간대입니다. est/cst/pst 중 하나를 입력해주세요.",
                ephemeral=True
            )

        # 2) Immediately confirm to the user
        await interaction.response.send_message(
            "✅ 발로란트 내전이 열렸습니다! 채널에서 참가 버튼을 확인하세요.",
            ephemeral=True
        )

        try:
            # 3) Parse time string
            dt = None
            for fmt in ("%H:%M", "%I:%M %p"):
                try:
                    dt = datetime.strptime(time, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                raise ValueError("시간 형식(예: 22:00 또는 10:00 PM)이 잘못되었습니다.")

            # 4) Convert to UTC
            user_tz = pytz.timezone(tz_map[zone.lower()])
            local_date = datetime.now(user_tz).date()
            local_dt = user_tz.localize(datetime.combine(local_date, dt.time()))
            utc_dt = local_dt.astimezone(timezone.utc)
            if utc_dt < datetime.now(timezone.utc):
                utc_dt += timedelta(days=1)

            # 5) Build display string
            display = "\n".join(
                f"**{lbl}:** {utc_dt.astimezone(pytz.timezone(tz)).strftime('%I:%M %p').lstrip('0')}"
                for lbl, tz in [
                    ("EST", "US/Eastern"),
                    ("CST", "US/Central"),
                    ("PST", "US/Pacific")
                ]
            )

            # 6) Find waiting VC
            vc = self.bot.get_channel(config.CUSTOM_GAME_VOICE_CHANNEL_ID)
            if not vc:
                raise RuntimeError("대기 음성 채널을 찾을 수 없습니다.")

            # 7) Build and send lobby as a normal bot message
            view = CustomGameView(interaction.user, interaction, vc)
            view.start_time        = display
            view.voice_check_start = utc_dt - timedelta(minutes=60)
            view.voice_check_end   = utc_dt

            channel = interaction.channel
            lobby_embed = discord.Embed(
                title="🕹️ 발로란트 내전",
                description=view.format_description(),
                color=discord.Color.green()
            )
            lobby_msg = await channel.send(
                content=f"<@&{config.CUSTOM_GAME_ROLE_ID}>",
                embed=lobby_embed,
                view=view,
                allowed_mentions=AllowedMentions(roles=True)
            )
            view.lobby_message = lobby_msg
            self.bot.add_view(view, message_id=lobby_msg.id)

            # 8) Activate global state & scheduled tasks
            self.bot.current_custom_game = view  # <-- Save to the bot object for global access

            view.warning_task = asyncio.create_task(self._warning_30min(view))
            asyncio.create_task(self._monitor_voice_check(view))

            await log_to_channel(
                self.bot,
                f"{interaction.user.display_name}님이 내전을 열었습니다:\n{display}"
            )

        except Exception as e:
            # If the initial send() wasn’t done, send error there; otherwise use followup
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ 오류: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"⚠️ slash_start_custom 오류: {e}")

    async def _warning_30min(self, view: CustomGameView):
        """Sends 30‑minute warning before start."""
        warn_time = view.voice_check_end - timedelta(minutes=30)
        delay = (warn_time - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        if current_custom_game is view:
            mentions = " ".join(p.mention for p in view.participants if isinstance(p, discord.Member))
            if mentions:
                await view.interaction.channel.send(f"⏰ 내전 시작 30분 전입니다!\n {mentions}\n이제부터 취소 불가합니다.")
            await log_to_channel(self.bot, "30분 전 경고 발송")

    async def _monitor_voice_check(self, view: CustomGameView):
        """Checks at 10/5/1 minutes to ping users not in VC."""
        # wait until view is active
        while current_custom_game is not view:
            await asyncio.sleep(0.5)
        # wait until 60 min before
        delay = (view.voice_check_start - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

        while current_custom_game is view and datetime.now(timezone.utc) < view.voice_check_end:
            remaining = (view.voice_check_end - datetime.now(timezone.utc)).total_seconds() / 60
            for mark in (10, 5, 1):
                if not view.ping_schedule[mark] and abs(remaining - mark) < 0.5:
                    not_in_vc = [
                        p.mention
                        for p in view.participants
                        if isinstance(p, discord.Member)
                        and (not p.voice or p.voice.channel.id != config.CUSTOM_GAME_VOICE_CHANNEL_ID)
                    ]
                    if not_in_vc:
                        await view.interaction.channel.send(
                            f"{' '.join(not_in_vc)} \n⏰ **{mark}분 전!** \n<#{config.CUSTOM_GAME_VOICE_CHANNEL_ID}> 으로 모여주세요!",
                            allowed_mentions=AllowedMentions(users=True)
                        )
                    view.ping_schedule[mark] = True
                    await log_to_channel(self.bot, f"{mark}분 전 알림 발송")
            await asyncio.sleep(30)

    @app_commands.command(
        name="내전종료",
        description="내전 종료하고, 참가한 모든 유저의 MMR을 업데이트합니다."
    )
    @app_commands.describe(region_hint="(선택) 지역(na/eu/kr 등)")
    async def slash_close_customs(self, interaction: discord.Interaction, region_hint: Optional[str] = "na"):
        await interaction.response.defer(ephemeral=True)
        try:
            user = interaction.user
            async with self.bot.db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT riot_name, riot_tag, puuid FROM players WHERE discord_id = $1",
                    str(user.id))
            if not row:
                await interaction.followup.send("❌ Riot account not linked. Use `/연동` first.", ephemeral=True)
                return

            puuid = row["puuid"]

            # 1. Fetch last 5 custom matches
            endpoint = f"/valorant/v3/by-puuid/matches/{region_hint}/{puuid}?filter=custom"
            data = await henrik_get(endpoint)
            if not data or data.get("status") != 200 or not data.get("data"):
                await interaction.followup.send("❌ Could not fetch recent custom matches.", ephemeral=True)
                return

            matches = data["data"][:5]
            if not matches:
                await interaction.followup.send("⚠️ No recent custom matches found.", ephemeral=True)
                return

            processed_count = 0
            already_analyzed = 0
            error_matches = []

            for match in matches:
                meta = match.get("metadata", {})
                match_id = meta.get("matchid", None)
                if not match_id:
                    continue
                # 2. Check if already analyzed
                async with self.bot.db.acquire() as conn:
                    exists = await conn.fetchval(
                        "SELECT 1 FROM analyzed_matches WHERE match_id = $1", match_id)
                if exists:
                    already_analyzed += 1
                    continue

                # 3. Analyze: process and update MMR for all involved
                try:
                    # Use your method (can adapt for custom games if needed)
                    await self.process_and_store_match(match_id, region_hint)
                    async with self.bot.db.acquire() as conn:
                        await conn.execute(
                            "INSERT INTO analyzed_matches (match_id) VALUES ($1)", match_id)
                    processed_count += 1
                except Exception as e:
                    error_matches.append(match_id)
                    print(f"Error analyzing match {match_id}: {e}")
                    continue

            # 4. Respond with summary
            message = (
                f"✅ 내전 종료 완료!\n"
                f"분석한 내전 수: `{processed_count}`\n"
                f"이미 분석된 내전 수: `{already_analyzed}`"
            )
            if error_matches:
                message += f"\n분석 실패한 매치: {', '.join(error_matches)}"

            await interaction.followup.send(message, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Unexpected error: {e}", ephemeral=True)

    @app_commands.command(name="맵추첨", description="랜덤 맵을 뽑습니다.")
    async def slash_roll_map(self, interaction: discord.Interaction):
        if not current_custom_game:
            return await interaction.response.send_message("❌ 활성 내전이 없습니다.", ephemeral=True)
        if not is_privileged(interaction.user, current_custom_game.creator):
            return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
        choice = random.choice(MAPS)
        await interaction.response.send_message(f"🗺️ 오늘의 맵: **{choice}**!")

    @app_commands.command(name="내전대기", description="현재 내전의 대기열을 엽니다.")
    async def slash_open_waitlist(self, interaction: discord.Interaction):
        if not current_custom_game:
            return await interaction.response.send_message("❌ 활성 내전이 없습니다.", ephemeral=True)
        if not is_privileged(interaction.user, current_custom_game.creator):
            return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
        if current_custom_game.waitlist_open:
            return await interaction.response.send_message("❌ 이미 대기열이 열려 있습니다.", ephemeral=True)
        current_custom_game.waitlist_open = True
        current_custom_game.rebuild_buttons()
        await current_custom_game.update_embed()
        await interaction.response.send_message("✅ 대기열이 활성화되었습니다.", ephemeral=True)

    @app_commands.command(name="봇추가", description="빈 자리에 가짜 유저를 추가합니다.")
    @commands.has_permissions(administrator=True)
    async def slash_add_bots(self, interaction: discord.Interaction):
        current_custom_game = getattr(self.bot, "current_custom_game", None)
        if not current_custom_game:
            return await interaction.response.send_message("❌ 활성 내전이 없습니다.", ephemeral=True)
        needed = 10 - len(current_custom_game.participants)
        for i in range(1, needed):
            current_custom_game.participants.append(FakeUser(f"플레이어{i}"))
        current_custom_game.rebuild_buttons()
        await current_custom_game.update_embed()
        await interaction.response.send_message(f"✅ 가짜 유저 {needed - 2}명 추가되었습니다.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomGame(bot))
