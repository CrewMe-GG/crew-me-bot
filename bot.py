import os
import sqlite3
import re
from datetime import datetime, date, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'staffy.sqlite3')


def db():
    con = sqlite3.connect(DB, timeout=10)
    con.execute('CREATE TABLE IF NOT EXISTS settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, manager_role_id INTEGER NOT NULL)')
    con.execute('''CREATE TABLE IF NOT EXISTS absences (
        thread_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        start_date TEXT NOT NULL, return_date TEXT NOT NULL, reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
        ended_at TEXT, ended_by INTEGER)''')
    con.execute('''CREATE TABLE IF NOT EXISTS appearance (guild_id INTEGER PRIMARY KEY, embed_color INTEGER NOT NULL DEFAULT 10181046)''')
    return con


DEFAULT_EMBED_COLOR = 0x9B59B6


def embed_color(guild_id: int) -> discord.Color:
    with db() as con:
        row = con.execute('SELECT embed_color FROM appearance WHERE guild_id=?', (guild_id,)).fetchone()
    return discord.Color(row[0] if row else DEFAULT_EMBED_COLOR)


def display_date(value: str) -> str:
    try:
        return date.fromisoformat(value[:10]).strftime('%m-%d-%Y')
    except (ValueError, TypeError):
        return value or 'Unknown'


def is_manager(member: discord.Member, manager_role_id: int):
    return member.guild_permissions.administrator or any(r.id == manager_role_id for r in member.roles)


class EndAbsenceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='End Absence', style=discord.ButtonStyle.danger,
                       emoji='✅', custom_id='staffy:absence:end')
    async def end_absence(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message('This button only works in an absence thread.', ephemeral=True)
        with db() as con:
            record = con.execute('SELECT user_id, status FROM absences WHERE thread_id=? AND guild_id=?',
                                 (interaction.channel.id, interaction.guild.id)).fetchone()
            settings = con.execute('SELECT manager_role_id FROM settings WHERE guild_id=?',
                                   (interaction.guild.id,)).fetchone()
        if not record:
            return await interaction.response.send_message('This absence is not in Crew Me’s records.', ephemeral=True)
        if record[1] != 'active':
            return await interaction.response.send_message('This absence is already ended.', ephemeral=True)
        member = interaction.user
        if member.id != record[0] and not (isinstance(member, discord.Member) and settings and is_manager(member, settings[0])):
            return await interaction.response.send_message('Only the submitting staff member or management can end this absence.', ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            # Archive first; don't mark the database complete if Discord refuses the action.
            await interaction.channel.edit(archived=True, reason=f'Absence ended by {member.id}')
        except discord.HTTPException:
            return await interaction.followup.send('I could not archive the thread. Check my Manage Threads permission.', ephemeral=True)
        with db() as con:
            con.execute("UPDATE absences SET status='ended', ended_at=?, ended_by=? WHERE thread_id=? AND status='active'",
                        (datetime.now(timezone.utc).isoformat(), member.id, interaction.channel.id))
        await interaction.followup.send('Absence ended and thread archived.', ephemeral=True)


class AbsenceForm(discord.ui.Modal, title='Staff Absence'):
    start = discord.ui.TextInput(label='Start date (MM-DD-YYYY)', max_length=10)
    end = discord.ui.TextInput(label='Return date (MM-DD-YYYY)', max_length=10)
    reason = discord.ui.TextInput(label='Reason', style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message('Use this in a server.', ephemeral=True)
        try:
            start = datetime.strptime(str(self.start).strip(), '%m-%d-%Y').date()
            end = datetime.strptime(str(self.end).strip(), '%m-%d-%Y').date()
            if end < start:
                raise ValueError()
        except ValueError:
            return await interaction.response.send_message('Enter valid MM-DD-YYYY dates (example: 10-28-2026); return must not precede start.', ephemeral=True)
        with db() as con:
            row = con.execute('SELECT channel_id, manager_role_id FROM settings WHERE guild_id=?',
                              (interaction.guild.id,)).fetchone()
        if not row:
            return await interaction.response.send_message('Crew Me has not been configured here.', ephemeral=True)
        channel = interaction.guild.get_channel(row[0])
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message('Absence channel missing. Ask an admin to run /setup_absences.', ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        thread = None
        try:
            username = re.sub(r'[^a-z0-9_-]', '-', interaction.user.name.lower()).strip('-')[:75] or str(interaction.user.id)
            thread = await channel.create_thread(name=f'absence-{username}',
                                                  type=discord.ChannelType.private_thread,
                                                  invitable=False, auto_archive_duration=1440)
            await thread.add_user(interaction.user)
            embed = discord.Embed(title='Staff Absence', color=embed_color(interaction.guild.id))
            embed.add_field(name='Staff member', value=interaction.user.mention, inline=False)
            embed.add_field(name='Start', value=start.strftime('%m-%d-%Y'))
            embed.add_field(name='Return', value=end.strftime('%m-%d-%Y'))
            embed.add_field(name='Reason', value=str(self.reason), inline=False)
            embed.set_footer(text='Use End Absence when finished. Management can access using Manage Threads.')
            await thread.send(embed=embed, view=EndAbsenceView(),
                              allowed_mentions=discord.AllowedMentions.none())
            with db() as con:
                con.execute('''INSERT INTO absences
                    (thread_id, guild_id, user_id, start_date, return_date, reason, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'active', ?)''',
                    (thread.id, interaction.guild.id, interaction.user.id,
                     start.isoformat(), end.isoformat(), str(self.reason),
                     datetime.now(timezone.utc).isoformat()))
            await interaction.followup.send(f'Your absence was submitted: {thread.mention}', ephemeral=True)
        except (discord.HTTPException, sqlite3.Error):
            await interaction.followup.send('Could not finish the submission. Check bot permissions and try again.', ephemeral=True)


class AbsencePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Submit Absence', style=discord.ButtonStyle.primary,
                       custom_id='staffy:absence:submit', emoji='📅')
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AbsenceForm())


class Staffy(commands.Bot):
    async def setup_hook(self):
        self.add_view(AbsencePanel())
        self.add_view(EndAbsenceView())
        local_names = [command.name for command in self.tree.get_commands()]
        print(f"Crew Me local slash commands: {local_names}")
        try:
            synced = await self.tree.sync()  # Global commands: work in every server
            print(f"Discord accepted {len(synced)} global commands: {[cmd.name for cmd in synced]}")
            remote = await self.tree.fetch_commands()
            print(f"Discord currently lists these global commands: {[cmd.name for cmd in remote]}")
        except discord.HTTPException as exc:
            print(f"ERROR: Global command sync failed: {exc}")
            raise


intents = discord.Intents.default()
intents.members = True  # Enable Server Members Intent in Discord Developer Portal
intents.message_content = True
bot = Staffy(command_prefix='!', intents=intents)


@bot.tree.command(name='setup_absences', description='Configure the Crew Me absence system')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel='Channel for private absence threads', manager_role='Role allowed to manage absences')
async def setup_absences(interaction: discord.Interaction, channel: discord.TextChannel, manager_role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message('Only server administrators can configure Crew Me.', ephemeral=True)
    with db() as con:
        con.execute('INSERT OR REPLACE INTO settings VALUES (?, ?, ?)',
                    (interaction.guild.id, channel.id, manager_role.id))
    embed = discord.Embed(title='Staff Absence Center',
                          description='Click below to submit your absence dates and reason privately. Absences are notifications, not approval requests.',
                          color=embed_color(interaction.guild.id))
    try:
        await channel.send(embed=embed, view=AbsencePanel())
    except discord.Forbidden:
        return await interaction.response.send_message('I cannot post in that channel.', ephemeral=True)
    await interaction.response.send_message(
        f'Panel posted in {channel.mention}. Grant authorized management Manage Threads in this channel; role membership alone does not grant private-thread access.',
        ephemeral=True)


def absence_embeds(title: str, rows: list, guild_id: int, history: bool = False, filtered: bool = False):
    """Build public, readable embeds without exposing private absence reasons or threads."""
    entries = []
    for row in rows:
        if history:
            _, user_id, start, end, ended_at = row
            closed = display_date(ended_at) if ended_at else 'Unknown'
            entries.append(f'**<@{user_id}>**\n📅 {display_date(start)} → {display_date(end)}\nCompleted: {closed}')
        else:
            _, user_id, start, end = row
            entries.append(f'**<@{user_id}>**\n📅 {display_date(start)} → {display_date(end)}')

    if not entries:
        entries = ['No completed absences found.' if history else 'No active staff absences recorded.']

    # Keep embeds within Discord's 4096-character description limit.
    pages = []
    current = ''
    for entry in entries:
        addition = entry if not current else '\n\n' + entry
        if len(current) + len(addition) > 3800:
            pages.append(current)
            current = entry
        else:
            current += addition
    if current:
        pages.append(current)

    embeds = []
    for index, description in enumerate(pages, 1):
        embed = discord.Embed(title=title, description=description, color=embed_color(guild_id))
        embed.set_footer(text=f'Crew Me • Page {index}/{len(pages)}' + (' • Latest 50 records' if history else ''))
        embeds.append(embed)
    return embeds


@bot.tree.command(name='absences', description='View active staff absences (management only)')
@app_commands.guild_only()
async def absences(interaction: discord.Interaction):
    with db() as con:
        settings = con.execute('SELECT manager_role_id FROM settings WHERE guild_id=?',
                               (interaction.guild.id,)).fetchone()
        if not settings:
            return await interaction.response.send_message('Crew Me is not configured here.', ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not is_manager(interaction.user, settings[0]):
            return await interaction.response.send_message('This command is for authorized management only.', ephemeral=True)
        rows = con.execute("""SELECT thread_id, user_id, start_date, return_date FROM absences
                              WHERE guild_id=? AND status='active' ORDER BY return_date ASC""",
                           (interaction.guild.id,)).fetchall()
    embeds = absence_embeds('Active Staff Absences', rows, interaction.guild.id)
    await interaction.response.send_message(embed=embeds[0], ephemeral=False,
                                            allowed_mentions=discord.AllowedMentions.none())
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed, ephemeral=False,
                                        allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='absence_history', description='View completed staff absences (management only)')
@app_commands.guild_only()
@app_commands.describe(member='Optionally filter history by a staff member')
async def absence_history(interaction: discord.Interaction, member: discord.Member | None = None):
    with db() as con:
        settings = con.execute('SELECT manager_role_id FROM settings WHERE guild_id=?',
                               (interaction.guild.id,)).fetchone()
        if not settings:
            return await interaction.response.send_message('Crew Me is not configured here.', ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not is_manager(interaction.user, settings[0]):
            return await interaction.response.send_message('This command is for authorized management only.', ephemeral=True)
        if member:
            rows = con.execute("""SELECT thread_id, user_id, start_date, return_date, ended_at
                FROM absences WHERE guild_id=? AND status='ended' AND user_id=?
                ORDER BY ended_at DESC LIMIT 50""", (interaction.guild.id, member.id)).fetchall()
        else:
            rows = con.execute("""SELECT thread_id, user_id, start_date, return_date, ended_at
                FROM absences WHERE guild_id=? AND status='ended'
                ORDER BY ended_at DESC LIMIT 50""", (interaction.guild.id,)).fetchall()
    title = 'Staff Absence History' + (f' — {member.display_name}' if member else '')
    embeds = absence_embeds(title[:256], rows, interaction.guild.id, history=True)
    await interaction.response.send_message(embed=embeds[0], ephemeral=False,
                                            allowed_mentions=discord.AllowedMentions.none())
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed, ephemeral=False,
                                        allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='set_embed_color', description='Set Crew Me embed color for this server')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(hex_color='Hex color, for example #FF69B4')
async def set_embed_color(interaction: discord.Interaction, hex_color: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message('Only server administrators can change Crew Me’s appearance.', ephemeral=True)
    value = hex_color.strip().lstrip('#')
    if not re.fullmatch(r'[0-9a-fA-F]{6}', value):
        return await interaction.response.send_message('Use a six-digit hex color, such as `#FF69B4`.', ephemeral=True)
    color_number = int(value, 16)
    with db() as con:
        con.execute('INSERT INTO appearance (guild_id, embed_color) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET embed_color=excluded.embed_color',
                    (interaction.guild.id, color_number))
    embed = discord.Embed(title='Crew Me Appearance Updated', description=f'Embed color set to `#{value.upper()}` for this server. New Crew Me embeds will use this color.', color=discord.Color(color_number))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name='set_server_avatar', description='Set Crew Me’s server-specific profile picture')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(image='Upload a PNG, JPEG, GIF, or WebP image')
async def set_server_avatar(interaction: discord.Interaction, image: discord.Attachment):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message('Only server administrators can change Crew Me’s appearance.', ephemeral=True)
    allowed = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
    if image.content_type not in allowed:
        return await interaction.response.send_message('Upload a PNG, JPEG, GIF, or WebP image.', ephemeral=True)
    if image.size > 8 * 1024 * 1024:
        return await interaction.response.send_message('Please use an image smaller than 8 MB.', ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        me = interaction.guild.me or await interaction.guild.fetch_member(bot.user.id)
        image_bytes = await image.read()
        await me.edit(avatar=image_bytes, reason=f'Crew Me server avatar set by {interaction.user.id}')
    except TypeError:
        return await interaction.followup.send('Your installed discord.py version does not support server-specific bot avatars. Update it with `python -m pip install -U discord.py` (version 2.7 or newer), then restart Crew Me.', ephemeral=True)
    except (discord.HTTPException, ValueError) as exc:
        return await interaction.followup.send(f'Discord could not change the server avatar: {exc}', ephemeral=True)
    await interaction.followup.send('Crew Me’s server-specific profile picture has been updated! Other servers are unaffected.', ephemeral=True)


@bot.tree.command(name='reset_appearance', description='Reset Crew Me’s color and server-specific avatar')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def reset_appearance(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message('Only server administrators can reset Crew Me’s appearance.', ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        me = interaction.guild.me or await interaction.guild.fetch_member(bot.user.id)
        await me.edit(avatar=None, reason=f'Crew Me appearance reset by {interaction.user.id}')
    except TypeError:
        return await interaction.followup.send('Your installed discord.py version does not support resetting server avatars. Update with `python -m pip install -U discord.py` (version 2.7 or newer). No settings were changed.', ephemeral=True)
    except (discord.HTTPException, ValueError) as exc:
        return await interaction.followup.send(f'Could not reset the server avatar: {exc}. No settings were changed.', ephemeral=True)
    with db() as con:
        con.execute('DELETE FROM appearance WHERE guild_id=?', (interaction.guild.id,))
    await interaction.followup.send('Crew Me’s embed color and server-specific avatar have been reset for this server.', ephemeral=True)



# ---- Crew Me public-server management features ----
from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import calendar


def ensure_extended_tables():
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS guild_config (
          guild_id INTEGER PRIMARY KEY, manager_role_id INTEGER, staff_role_id INTEGER,
          timezone_name TEXT DEFAULT 'UTC', voice_channels TEXT DEFAULT '',
          meeting_channel_id INTEGER, ticket_category_id INTEGER, ticket_log_channel_id INTEGER,
          warning_channel_id INTEGER);
        CREATE TABLE IF NOT EXISTS activity (
          guild_id INTEGER, user_id INTEGER, week TEXT, messages INTEGER DEFAULT 0,
          voice_seconds INTEGER DEFAULT 0, event_credits INTEGER DEFAULT 0,
          active_days TEXT DEFAULT '', PRIMARY KEY(guild_id,user_id,week));
        CREATE TABLE IF NOT EXISTS warnings (
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
          issuer_id INTEGER NOT NULL, level TEXT NOT NULL, reason TEXT NOT NULL,
          created_at TEXT NOT NULL, notified_at TEXT, notified_by INTEGER,
          revoked_at TEXT, revoked_by INTEGER, ticket_id INTEGER);
        CREATE TABLE IF NOT EXISTS staff_tickets (
          channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
          opener_id INTEGER NOT NULL, topic TEXT NOT NULL, warning_id INTEGER,
          status TEXT DEFAULT 'open', created_at TEXT NOT NULL, closed_at TEXT);
        CREATE TABLE IF NOT EXISTS meetings (
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
          title TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
          voice_channel_id INTEGER NOT NULL, post_channel_id INTEGER NOT NULL,
          message_id INTEGER, status TEXT DEFAULT 'scheduled');
        CREATE TABLE IF NOT EXISTS meeting_attendance (
          meeting_id INTEGER, user_id INTEGER, seconds INTEGER DEFAULT 0,
          manually_added INTEGER DEFAULT 0, PRIMARY KEY(meeting_id,user_id));
        CREATE TABLE IF NOT EXISTS meeting_rsvp (
          meeting_id INTEGER, user_id INTEGER, response TEXT,
          PRIMARY KEY(meeting_id,user_id));
        ''')


def cfg(guild_id):
    with db() as con:
        return con.execute('SELECT manager_role_id, staff_role_id, timezone_name, voice_channels, meeting_channel_id, ticket_category_id, ticket_log_channel_id, warning_channel_id FROM guild_config WHERE guild_id=?', (guild_id,)).fetchone()


def manager_allowed(interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    c = cfg(interaction.guild.id)
    if c and c[0] and any(r.id == c[0] for r in interaction.user.roles):
        return True
    with db() as con:
        old = con.execute('SELECT manager_role_id FROM settings WHERE guild_id=?',(interaction.guild.id,)).fetchone()
    return bool(old and any(r.id == old[0] for r in interaction.user.roles))


async def require_manager(interaction):
    if not manager_allowed(interaction):
        await interaction.response.send_message('Management only. Configure your management role with /crewme_setup.', ephemeral=True)
        return False
    return True


def is_staff(member):
    c = cfg(member.guild.id)
    return bool(c and c[1] and any(r.id == c[1] for r in member.roles))


def guild_now(guild_id):
    c = cfg(guild_id)
    try:
        tz = ZoneInfo(c[2] if c and c[2] else 'UTC')
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    return datetime.now(tz)


def week_start(dt):
    # Sunday is the start of the weekly activity period.
    return (dt.date() - timedelta(days=(dt.weekday()+1)%7)).isoformat()


def activity_update(guild_id, user_id, *, messages=0, seconds=0, credits=0, active_day=None, when=None):
    local = when or guild_now(guild_id)
    week = week_start(local)
    with db() as con:
        con.execute('INSERT OR IGNORE INTO activity(guild_id,user_id,week) VALUES(?,?,?)',(guild_id,user_id,week))
        row = con.execute('SELECT active_days FROM activity WHERE guild_id=? AND user_id=? AND week=?',(guild_id,user_id,week)).fetchone()
        days = set(filter(None, (row[0] or '').split(',')))
        if active_day:
            days.add(active_day)
        con.execute('''UPDATE activity SET messages=messages+?, voice_seconds=voice_seconds+?,
            event_credits=event_credits+?, active_days=? WHERE guild_id=? AND user_id=? AND week=?''',
            (messages,seconds,credits,','.join(sorted(days)),guild_id,user_id,week))


def credit_total(row):
    # Every full three hours earns 100 credits; leftovers stay in voice_seconds.
    return row[0] + (row[1] // 10800)*100 + row[2]


def evaluate_staff_week(guild_id, user_id, week):
    from datetime import date, timedelta

    week_date = date.fromisoformat(week)
    week_end = week_date + timedelta(days=6)

    # Never evaluate an unfinished week.
    if guild_now(guild_id).date() <= week_end:
        return 'ineligible', 0, 0

    # Only evaluate full weeks of staff membership.
    with db() as con:
        membership = con.execute(
            "SELECT staff_since FROM staff_memberships WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ).fetchone()

    if not membership or date.fromisoformat(membership[0]) > week_date:
        return 'ineligible', 0, 0

    with db() as con:
        requirements = con.execute("""
            SELECT weekly_credits, minimum_active_days
            FROM activity_requirements
            WHERE guild_id=?
        """, (guild_id,)).fetchone()

        required_credits, required_days = requirements or (500, 4)

        activity = con.execute("""
            SELECT messages, voice_seconds, event_credits, active_days
            FROM activity
            WHERE guild_id=? AND user_id=? AND week=?
        """, (guild_id, user_id, week)).fetchone()

        activity = activity or (0, 0, 0, '')

        total = credit_total(activity)
        days = len(set(filter(None, activity[3].split(','))))

        absence = con.execute("""
            SELECT 1 FROM absences
            WHERE guild_id=? AND user_id=?
              AND start_date<=?
              AND return_date>=?
            LIMIT 1
        """, (
            guild_id, user_id,
            week_end.strftime('%Y-%m-%d'),
            week_date.strftime('%Y-%m-%d')
        )).fetchone()

        if absence:
            status = 'exempt'
        elif total >= required_credits and days >= required_days:
            status = 'passed'
        else:
            status = 'failed'

        con.execute("""
            INSERT INTO weekly_evaluations
                (guild_id, user_id, week, total_credits,
                 active_days, status, evaluated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, week)
            DO UPDATE SET
                total_credits=excluded.total_credits,
                active_days=excluded.active_days,
                status=excluded.status,
                evaluated_at=excluded.evaluated_at
        """, (
            guild_id, user_id, week, total, days,
            status, datetime.now(timezone.utc).isoformat()
        ))

    return status, total, days



def check_consecutive_failures(guild_id, user_id, review_week):
    from datetime import date, timedelta

    with db() as con:
        requirements = con.execute("""
            SELECT consecutive_weeks
            FROM activity_requirements
            WHERE guild_id=?
        """, (guild_id,)).fetchone()

        required = requirements[0] if requirements else 3

        if required < 1:
            raise ValueError("consecutive_weeks must be at least 1")

        review_date = date.fromisoformat(review_week)
        failures = 0

        for offset in range(required):
            week = (
                review_date - timedelta(weeks=offset)
            ).isoformat()

            row = con.execute("""
                SELECT status
                FROM weekly_evaluations
                WHERE guild_id=? AND user_id=? AND week=?
            """, (guild_id, user_id, week)).fetchone()

            if not row or row[0] != 'failed':
                break

            failures += 1

        if failures >= required:
            con.execute("""
                INSERT OR IGNORE INTO activity_reviews
                    (guild_id, user_id, review_week,
                     consecutive_failures, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
            """, (
                guild_id, user_id, review_week, failures,
                datetime.now(timezone.utc).isoformat()
            ))

            return True, failures

    return False, failures


@bot.tree.command(name='crewme_setup', description='Configure Crew Me management and staff roles')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.choices(timezone_name=[
    app_commands.Choice(name='EST / EDT — Eastern', value='America/New_York'),
    app_commands.Choice(name='CST / CDT — Central', value='America/Chicago'),
    app_commands.Choice(name='MST / MDT — Mountain', value='America/Denver'),
    app_commands.Choice(name='PST / PDT — Pacific', value='America/Los_Angeles'),
    app_commands.Choice(name='AKST / AKDT — Alaska', value='America/Anchorage'),
    app_commands.Choice(name='HST — Hawaii', value='Pacific/Honolulu'),
    app_commands.Choice(name='UTC', value='UTC'),
])
async def staffy_setup(interaction: discord.Interaction, management_role: discord.Role, staff_role: discord.Role, timezone_name: app_commands.Choice[str]):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message('Administrator only.', ephemeral=True)
    try:
        ZoneInfo(timezone_name.value)
    except ZoneInfoNotFoundError:
        return await interaction.response.send_message('Invalid IANA timezone. Example: America/New_York.', ephemeral=True)
    with db() as con:
        con.execute('''INSERT INTO guild_config(guild_id,manager_role_id,staff_role_id,timezone_name) VALUES(?,?,?,?)
            ON CONFLICT(guild_id) DO UPDATE SET manager_role_id=excluded.manager_role_id,
            staff_role_id=excluded.staff_role_id, timezone_name=excluded.timezone_name''',
            (interaction.guild.id,management_role.id,staff_role.id,timezone_name.value))
    await interaction.response.send_message(f'Crew Me configured: management {management_role.mention}, staff {staff_role.mention}, timezone `{timezone_name.name}`.',ephemeral=True)


class VoiceChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(channel_types=[discord.ChannelType.voice, discord.ChannelType.stage_voice],
                         placeholder='Choose voice channels that count', min_values=1, max_values=25)

    async def callback(self, interaction):
        if not manager_allowed(interaction):
            return await interaction.response.send_message('Management only.',ephemeral=True)
        ids = [str(ch.id) for ch in self.values]
        with db() as con:
            con.execute('UPDATE guild_config SET voice_channels=? WHERE guild_id=?',(','.join(ids),interaction.guild.id))
        await interaction.response.send_message(f'Saved {len(ids)} eligible voice channels. At least 2 non-bot members must be present for time to count.',ephemeral=True)


class VoiceConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(VoiceChannelSelect())


@bot.tree.command(name='voice_settings', description='Choose voice channels eligible for staff activity credits')
@app_commands.guild_only()
async def voice_settings(interaction: discord.Interaction):
    if not await require_manager(interaction): return
    if not cfg(interaction.guild.id):
        return await interaction.response.send_message('Run /crewme_setup first.',ephemeral=True)
    await interaction.response.send_message('Select eligible voice channels. Counting requires **2 non-bot members** in the same channel. 3 hours = 100 credits.',view=VoiceConfigView(),ephemeral=True)


@bot.tree.command(name='activity', description='View weekly staff activity')
@app_commands.guild_only()
async def activity(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    if member.id != interaction.user.id and not manager_allowed(interaction):
        return await interaction.response.send_message('You can only view your own activity.',ephemeral=True)
    week = week_start(guild_now(interaction.guild.id))
    with db() as con:
        row = con.execute('SELECT messages,voice_seconds,event_credits,active_days FROM activity WHERE guild_id=? AND user_id=? AND week=?',(interaction.guild.id,member.id,week)).fetchone() or (0,0,0,'')
    e = discord.Embed(title='Weekly Staff Activity',color=embed_color(interaction.guild.id))
    e.description = f'{member.mention}\nWeek starting {display_date(week)}'
    e.add_field(name='Messages',value=str(row[0]))
    e.add_field(name='Eligible voice time',value=f'{row[1]/3600:.1f} hours')
    e.add_field(name='Event credits',value=str(row[2]))
    e.add_field(name='Total credits',value=f'{credit_total(row)} / 500')
    e.add_field(name='Active days',value=f'{len(set(filter(None,row[3].split(","))))} / 4')
    await interaction.response.send_message(embed=e,ephemeral=True)


@bot.tree.command(name='leaderboard', description='Post the current staff activity leaderboard')
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    if not await require_manager(interaction): return
    week = week_start(guild_now(interaction.guild.id))
    with db() as con:
        rows = con.execute('SELECT user_id,messages,voice_seconds,event_credits FROM activity WHERE guild_id=? AND week=?',(interaction.guild.id,week)).fetchall()
    rows = sorted(rows,key=lambda r:credit_total(r[1:]),reverse=True)[:20]
    lines = [f'**{i}.** <@{r[0]}> — **{credit_total(r[1:])}** credits' for i,r in enumerate(rows,1)]
    await interaction.response.send_message(embed=discord.Embed(title='Staff Activity Leaderboard',description='\n'.join(lines) or 'No activity recorded this week.',color=embed_color(interaction.guild.id)),allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='log_event', description='Award activity credits for hosting a staff event')
@app_commands.guild_only()
@app_commands.choices(event_type=[app_commands.Choice(name='Movie (50 credits)',value='movie'),app_commands.Choice(name='Game (100 credits)',value='game')])
async def log_event(interaction: discord.Interaction, member: discord.Member, event_type: app_commands.Choice[str]):
    if not await require_manager(interaction): return
    credits = 50 if event_type.value=='movie' else 100
    activity_update(interaction.guild.id,member.id,credits=credits)
    await interaction.response.send_message(f'Added {credits} {event_type.value} event credits to {member.mention}.',ephemeral=True)


@bot.tree.command(name='staff_profile', description='View a staff member profile')
@app_commands.guild_only()
async def staff_profile(interaction: discord.Interaction, member: discord.Member):
    if not manager_allowed(interaction) and member.id != interaction.user.id:
        return await interaction.response.send_message('Only management can view other staff profiles.',ephemeral=True)
    week = week_start(guild_now(interaction.guild.id))
    with db() as con:
        row = con.execute('SELECT messages,voice_seconds,event_credits,active_days FROM activity WHERE guild_id=? AND user_id=? AND week=?',(interaction.guild.id,member.id,week)).fetchone() or (0,0,0,'')
        meetings = con.execute('SELECT COUNT(*) FROM meeting_attendance a JOIN meetings m ON m.id=a.meeting_id WHERE m.guild_id=? AND a.user_id=? AND (a.manually_added=1 OR a.seconds>=600)',(interaction.guild.id,member.id)).fetchone()[0]
        warnings = con.execute('SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=? AND revoked_at IS NULL',(interaction.guild.id,member.id)).fetchone()[0]
    e = discord.Embed(title='Staff Profile',description=member.mention,color=embed_color(interaction.guild.id))
    e.add_field(name='Current rank',value=', '.join(r.mention for r in member.roles[1:] if r.name!='@everyone')[:1000] or 'None',inline=False)
    e.add_field(name='Joined server',value=discord.utils.format_dt(member.joined_at,'D') if member.joined_at else 'Unknown')
    e.add_field(name='Weekly credits',value=f'{credit_total(row)} / 500')
    e.add_field(name='Active days',value=str(len(set(filter(None,row[3].split(','))))))
    e.add_field(name='Meetings attended',value=str(meetings))
    if manager_allowed(interaction):
        e.add_field(name='Active warnings (management only)',value=str(warnings))
    await interaction.response.send_message(embed=e,ephemeral=True)


@bot.tree.command(name='warning_channel', description='Set the private management channel for warning records')
@app_commands.guild_only()
async def warning_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await require_manager(interaction): return
    if not cfg(interaction.guild.id): return await interaction.response.send_message('Run /crewme_setup first.',ephemeral=True)
    with db() as con:
        con.execute('UPDATE guild_config SET warning_channel_id=? WHERE guild_id=?',(channel.id,interaction.guild.id))
    await interaction.response.send_message(f'Warning records will be posted in {channel.mention}. Ensure staff cannot view this channel.',ephemeral=True)



class ReviewReasonModal(discord.ui.Modal):
    def __init__(self, guild_id, user_id, review_week, resolution):
        super().__init__(title=f'{resolution.title()} Staff Review')
        self.guild_id = guild_id
        self.user_id = user_id
        self.review_week = review_week
        self.resolution = resolution

        self.reason = discord.ui.TextInput(
            label='Reason (optional)',
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_manager(interaction):
            return

        reason = str(self.reason.value).strip()
        resolved_at = datetime.now(timezone.utc).isoformat()

        with db() as con:
            cursor = con.execute(
                """UPDATE activity_reviews
                   SET status=?, resolved_by=?, resolved_at=?,
                       resolution_reason=?
                   WHERE guild_id=? AND user_id=?
                     AND review_week=? AND status='pending'""",
                (
                    self.resolution,
                    interaction.user.id,
                    resolved_at,
                    reason or None,
                    self.guild_id,
                    self.user_id,
                    self.review_week
                )
            )

        if cursor.rowcount != 1:
            await interaction.response.send_message(
                'This review has already been resolved.',
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title='Crew Me | Demotion Review',
            color=embed_color(self.guild_id)
        )
        embed.add_field(
            name='Staff Member',
            value=f'<@{self.user_id}>',
            inline=False
        )
        embed.add_field(
            name='Review Week',
            value=self.review_week
        )
        embed.add_field(
            name='Status',
            value=self.resolution.title()
        )
        embed.add_field(
            name='Resolved By',
            value=interaction.user.mention,
            inline=False
        )
        embed.add_field(
            name='Reason',
            value=reason or 'No reason provided',
            inline=False
        )
        embed.set_footer(text='Management decision recorded | No roles changed')

        with db() as con:
            row = con.execute(
                """SELECT review_message_id
                   FROM activity_reviews
                   WHERE guild_id=? AND user_id=? AND review_week=?""",
                (self.guild_id, self.user_id, self.review_week)
            ).fetchone()

        if row and row[0]:
            channel = interaction.guild.get_channel(interaction.channel_id)
            if channel:
                try:
                    message = await channel.fetch_message(row[0])
                    await message.edit(embed=embed, view=None)
                except discord.HTTPException:
                    pass

        await interaction.response.send_message(
            f'Review marked as **{self.resolution.title()}**.',
            ephemeral=True
        )


class DemotionReviewView(discord.ui.View):
    def __init__(self, guild_id, user_id, review_week):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.review_week = review_week

        key = f'{guild_id}:{user_id}:{review_week}'
        self.children[0].custom_id = f'crewme:review:reviewed:{key}'
        self.children[1].custom_id = f'crewme:review:dismissed:{key}'

    async def handle(self, interaction, resolution):
        if not await require_manager(interaction):
            return

        with db() as con:
            row = con.execute(
                """SELECT status FROM activity_reviews
                   WHERE guild_id=? AND user_id=? AND review_week=?""",
                (self.guild_id, self.user_id, self.review_week)
            ).fetchone()

        if not row or row[0] != 'pending':
            await interaction.response.send_message(
                'This review is no longer pending.',
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            ReviewReasonModal(
                self.guild_id,
                self.user_id,
                self.review_week,
                resolution
            )
        )

    @discord.ui.button(
        label='Reviewed',
        style=discord.ButtonStyle.success
    )
    async def reviewed(self, interaction, button):
        await self.handle(interaction, 'reviewed')

    @discord.ui.button(
        label='Dismiss',
        style=discord.ButtonStyle.danger
    )
    async def dismissed(self, interaction, button):
        await self.handle(interaction, 'dismissed')


class WarningNotifyView(discord.ui.View):
    def __init__(self, warning_id):
        super().__init__(timeout=None)
        self.warning_id = warning_id
        self.children[0].custom_id = f'staffy:warning:notify:{warning_id}'
        self.children[1].custom_id = f'staffy:warning:ticket:{warning_id}'

    @discord.ui.button(label='Notify Staff',style=discord.ButtonStyle.primary)
    async def notify(self,interaction,button):
        if not await require_manager(interaction): return
        with db() as con:
            w=con.execute('SELECT guild_id,user_id,level,reason,notified_at,revoked_at FROM warnings WHERE id=?',(self.warning_id,)).fetchone()
        if not w or w[0]!=interaction.guild.id or w[5]:
            return await interaction.response.send_message('Warning not found or revoked.',ephemeral=True)
        if w[4]: return await interaction.response.send_message('Already notified.',ephemeral=True)
        try:
            user=await bot.fetch_user(w[1])
            await user.send(f'Staff warning from **{interaction.guild.name}**\n**Level:** {w[2]}\n**Reason:** {w[3]}')
        except discord.HTTPException:
            return await interaction.response.send_message('DM failed; warning remains unnotified.',ephemeral=True)
        with db() as con:
            con.execute('UPDATE warnings SET notified_at=?,notified_by=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),interaction.user.id,self.warning_id))
        await interaction.response.send_message('Warning delivered by DM.',ephemeral=True)

    @discord.ui.button(label='Open Staff Ticket',style=discord.ButtonStyle.secondary)
    async def ticket(self,interaction,button):
        if not await require_manager(interaction): return
        with db() as con:
            w=con.execute('SELECT guild_id,user_id,level,reason,ticket_id FROM warnings WHERE id=?',(self.warning_id,)).fetchone()
        if not w or w[0]!=interaction.guild.id: return await interaction.response.send_message('Warning not found.',ephemeral=True)
        if w[4]: return await interaction.response.send_message(f'Ticket already exists: <#{w[4]}>',ephemeral=True)
        await interaction.response.send_message('Opening this ticket will reveal the warning to the staff member. Confirm?',view=ConfirmWarningTicketView(self.warning_id,w[1]),ephemeral=True)



class ConfirmWarningTicketView(discord.ui.View):
    def __init__(self, warning_id, user_id):
        super().__init__(timeout=90)
        self.warning_id=warning_id
        self.user_id=user_id

    @discord.ui.button(label='Yes, Open Private Ticket',style=discord.ButtonStyle.danger)
    async def confirm(self,interaction,button):
        if not await require_manager(interaction): return
        member=interaction.guild.get_member(self.user_id)
        if not member:
            return await interaction.response.send_message('Staff member is no longer in this server.',ephemeral=True)
        await staff_ticket.callback(interaction,member,f'Disciplinary warning #{self.warning_id}',self.warning_id)
        self.stop()

    @discord.ui.button(label='Cancel',style=discord.ButtonStyle.secondary)
    async def cancel(self,interaction,button):
        self.stop()
        await interaction.response.send_message('Ticket creation cancelled.',ephemeral=True)

@bot.tree.command(name='staff_warn',description='Create a private disciplinary warning (does not notify staff)')
@app_commands.guild_only()
@app_commands.choices(level=[app_commands.Choice(name=n,value=n) for n in ['Verbal','Written','Final','Review']])
async def staff_warn(interaction: discord.Interaction, member: discord.Member, level: app_commands.Choice[str], reason: str):
    if not await require_manager(interaction): return
    if len(reason)>1500: return await interaction.response.send_message('Reason must be 1500 characters or less.',ephemeral=True)
    with db() as con:
        cursor=con.execute('INSERT INTO warnings(guild_id,user_id,issuer_id,level,reason,created_at) VALUES(?,?,?,?,?,?)',(interaction.guild.id,member.id,interaction.user.id,level.value,reason,datetime.now(timezone.utc).isoformat()))
        wid=cursor.lastrowid
    e=discord.Embed(title=f'Confidential Staff Warning #{wid}',description=f'**Staff:** {member.mention}\n**Level:** {level.value}\n**Reason:** {reason}\n**Status:** Not notified',color=embed_color(interaction.guild.id))
    c=cfg(interaction.guild.id)
    channel=interaction.guild.get_channel(c[7]) if c and c[7] else None
    if isinstance(channel,discord.TextChannel):
        try:
            await channel.send(embed=e,view=WarningNotifyView(wid),allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
    await interaction.response.send_message(f'Warning #{wid} saved privately. Use `/staff_warning_notify` to notify later. Configure `/warning_channel` for management buttons.',ephemeral=True)


@bot.tree.command(name='staff_warnings',description='View a staff member disciplinary history')
@app_commands.guild_only()
async def staff_warnings(interaction: discord.Interaction, member: discord.Member):
    if not await require_manager(interaction): return
    with db() as con:
        rows=con.execute('SELECT id,level,reason,created_at,notified_at,revoked_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 15',(interaction.guild.id,member.id)).fetchall()
    lines=[f'**#{r[0]} — {r[1]}** ({display_date(r[3])}) — {"Revoked" if r[5] else "Notified" if r[4] else "Private"}\n{discord.utils.escape_markdown(r[2])[:200]}' for r in rows]
    await interaction.response.send_message(embed=discord.Embed(title=f'Warnings: {member.display_name}',description='\n\n'.join(lines)[:4000] or 'No warnings.',color=embed_color(interaction.guild.id)),ephemeral=True)


@bot.tree.command(name='staff_warning_notify',description='Send a saved warning to the staff member by DM')
@app_commands.guild_only()
async def staff_warning_notify(interaction: discord.Interaction, warning_id: int):
    if not await require_manager(interaction): return
    with db() as con:
        w=con.execute('SELECT guild_id,user_id,level,reason,notified_at,revoked_at FROM warnings WHERE id=?',(warning_id,)).fetchone()
    if not w or w[0]!=interaction.guild.id or w[5]: return await interaction.response.send_message('Active warning not found.',ephemeral=True)
    if w[4]: return await interaction.response.send_message('This warning was already notified.',ephemeral=True)
    try:
        user=await bot.fetch_user(w[1]); await user.send(f'Staff warning from **{interaction.guild.name}**\n**Level:** {w[2]}\n**Reason:** {w[3]}')
    except discord.HTTPException: return await interaction.response.send_message('Could not deliver DM. Warning remains private.',ephemeral=True)
    with db() as con:
        con.execute('UPDATE warnings SET notified_at=?,notified_by=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),interaction.user.id,warning_id))
    await interaction.response.send_message('Warning delivered by DM.',ephemeral=True)


@bot.tree.command(name='staff_warning_revoke',description='Revoke a warning without deleting its audit record')
@app_commands.guild_only()
async def staff_warning_revoke(interaction: discord.Interaction, warning_id: int):
    if not await require_manager(interaction): return
    with db() as con:
        cur=con.execute('UPDATE warnings SET revoked_at=?,revoked_by=? WHERE id=? AND guild_id=? AND revoked_at IS NULL',(datetime.now(timezone.utc).isoformat(),interaction.user.id,warning_id,interaction.guild.id))
    await interaction.response.send_message('Warning revoked.' if cur.rowcount else 'Active warning not found.',ephemeral=True)


@bot.tree.command(name='staff_ticket_setup',description='Choose private staff ticket category and management log channel')
@app_commands.guild_only()
async def staff_ticket_setup(interaction: discord.Interaction, category: discord.CategoryChannel, log_channel: discord.TextChannel):
    if not await require_manager(interaction): return
    if not cfg(interaction.guild.id): return await interaction.response.send_message('Run /crewme_setup first.',ephemeral=True)
    with db() as con:
        con.execute('UPDATE guild_config SET ticket_category_id=?,ticket_log_channel_id=? WHERE guild_id=?',(category.id,log_channel.id,interaction.guild.id))
    await interaction.response.send_message('Staff ticket category and log channel configured. Make sure management can access the category.',ephemeral=True)


@bot.tree.command(name='staff_ticket',description='Open a private management-initiated discussion with staff')
@app_commands.guild_only()
async def staff_ticket(interaction: discord.Interaction, member: discord.Member, topic: str, warning_id: int | None = None):
    if not await require_manager(interaction): return
    c=cfg(interaction.guild.id)
    category=interaction.guild.get_channel(c[5]) if c and c[5] else None
    if not isinstance(category,discord.CategoryChannel):
        return await interaction.response.send_message('Configure /staff_ticket_setup first.',ephemeral=True)
    if warning_id:
        with db() as con:
            w=con.execute('SELECT user_id,ticket_id,reason FROM warnings WHERE id=? AND guild_id=?',(warning_id,interaction.guild.id)).fetchone()
        if not w or w[0]!=member.id or w[1]: return await interaction.response.send_message('Warning not found for this member, or already linked to a ticket.',ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    overwrites={interaction.guild.default_role:discord.PermissionOverwrite(view_channel=False),
        member:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True),
        interaction.user:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)}
    if c and c[0]:
        role=interaction.guild.get_role(c[0])
        if role: overwrites[role]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
    try:
        channel=await category.create_text_channel(name=f'staff-{member.name[:45]}',overwrites=overwrites,reason=f'Private staff discussion opened by {interaction.user.id}')
        with db() as con:
            con.execute('INSERT INTO staff_tickets(channel_id,guild_id,user_id,opener_id,topic,warning_id,created_at) VALUES(?,?,?,?,?,?,?)',(channel.id,interaction.guild.id,member.id,interaction.user.id,topic,warning_id,datetime.now(timezone.utc).isoformat()))
            if warning_id: con.execute('UPDATE warnings SET ticket_id=? WHERE id=? AND guild_id=?',(channel.id,warning_id,interaction.guild.id))
        desc=f'**Staff member:** {member.mention}\n**Opened by:** {interaction.user.mention}\n**Topic:** {topic}'
        if warning_id: desc+=f'\n**Linked warning:** #{warning_id}\n**Reason:** {w[2]}'
        await channel.send(content=member.mention,embed=discord.Embed(title='Private Staff Discussion',description=desc,color=embed_color(interaction.guild.id)))
        await interaction.followup.send(f'Ticket created: {channel.mention}. The staff member can see this ticket.',ephemeral=True)
    except discord.HTTPException as exc:
        await interaction.followup.send(f'Could not create ticket: {exc}',ephemeral=True)


@bot.tree.command(name='staff_ticket_close',description='Close and lock a staff discussion ticket')
@app_commands.guild_only()
async def staff_ticket_close(interaction: discord.Interaction):
    if not await require_manager(interaction): return
    with db() as con:
        row=con.execute('SELECT status FROM staff_tickets WHERE guild_id=? AND channel_id=?',(interaction.guild.id,interaction.channel.id)).fetchone()
    if not row: return await interaction.response.send_message('Use this inside a Crew Me staff ticket.',ephemeral=True)
    if row[0]=='closed': return await interaction.response.send_message('Already closed.',ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.channel.edit(name=('closed-'+interaction.channel.name)[:100],reason='Crew Me ticket closed')
        await interaction.channel.set_permissions(interaction.guild.default_role,view_channel=False)
        with db() as con:
            con.execute('UPDATE staff_tickets SET status=?,closed_at=? WHERE channel_id=?',('closed',datetime.now(timezone.utc).isoformat(),interaction.channel.id))
        await interaction.followup.send('Ticket marked closed. Channel retained for authorized management.',ephemeral=True)
    except discord.HTTPException: await interaction.followup.send('Could not close the ticket.',ephemeral=True)


@bot.tree.command(name='meeting_create',description='Schedule a meeting with automatic VC attendance')
@app_commands.guild_only()
async def meeting_create(interaction: discord.Interaction, title: str, start: str, duration_minutes: app_commands.Range[int,10,720], voice_channel: discord.VoiceChannel, post_channel: discord.TextChannel):
    if not await require_manager(interaction): return
    try:
        local=datetime.strptime(start,'%m-%d-%Y %H:%M')
        tz=ZoneInfo(cfg(interaction.guild.id)[2] or 'UTC')
        start_dt=local.replace(tzinfo=tz).astimezone(timezone.utc)
        if start_dt<=datetime.now(timezone.utc): raise ValueError()
    except (ValueError,TypeError,ZoneInfoNotFoundError):
        return await interaction.response.send_message('Use a future `MM-DD-YYYY HH:MM` in the server timezone (24-hour clock).',ephemeral=True)
    end_dt=start_dt+timedelta(minutes=duration_minutes)
    with db() as con:
        mid=con.execute('INSERT INTO meetings(guild_id,title,start_at,end_at,voice_channel_id,post_channel_id) VALUES(?,?,?,?,?,?)',(interaction.guild.id,title,start_dt.isoformat(),end_dt.isoformat(),voice_channel.id,post_channel.id)).lastrowid
    e=discord.Embed(title=f'Staff Meeting #{mid}: {title}',description=f'**When:** {discord.utils.format_dt(start_dt,"F")}\n**Duration:** {duration_minutes} minutes\n**Voice:** {voice_channel.mention}\nAttendance automatically tracked; 10 minutes required.',color=embed_color(interaction.guild.id))
    try:
        msg=await post_channel.send(embed=e,view=MeetingRSVPView(mid))
        with db() as con: con.execute('UPDATE meetings SET message_id=? WHERE id=?',(msg.id,mid))
    except discord.HTTPException:
        return await interaction.response.send_message(f'Meeting #{mid} saved, but I could not post in {post_channel.mention}.',ephemeral=True)
    await interaction.response.send_message(f'Meeting #{mid} scheduled and posted in {post_channel.mention}.',ephemeral=True)


class MeetingRSVPView(discord.ui.View):
    def __init__(self, meeting_id):
        super().__init__(timeout=None)
        self.meeting_id=meeting_id
        for i, item in enumerate(self.children):
            item.custom_id = f'staffy:meeting:{meeting_id}:{i}'

    async def record(self,interaction,response):
        with db() as con:
            m=con.execute('SELECT guild_id,status FROM meetings WHERE id=?',(self.meeting_id,)).fetchone()
            if not m or m[0]!=interaction.guild.id or m[1]!='scheduled':
                return await interaction.response.send_message('Meeting unavailable.',ephemeral=True)
            con.execute('INSERT INTO meeting_rsvp(meeting_id,user_id,response) VALUES(?,?,?) ON CONFLICT(meeting_id,user_id) DO UPDATE SET response=excluded.response',(self.meeting_id,interaction.user.id,response))
        await interaction.response.send_message(f'RSVP saved: {response}.',ephemeral=True)

    @discord.ui.button(label='Attending',style=discord.ButtonStyle.success)
    async def yes(self,interaction,button): await self.record(interaction,'Attending')
    @discord.ui.button(label='Maybe',style=discord.ButtonStyle.secondary)
    async def maybe(self,interaction,button): await self.record(interaction,'Maybe')
    @discord.ui.button(label="Can't Attend",style=discord.ButtonStyle.danger)
    async def no(self,interaction,button): await self.record(interaction,"Can't Attend")


@bot.tree.command(name='meeting_add_attendee',description='Manually mark staff as attended')
@app_commands.guild_only()
async def meeting_add_attendee(interaction: discord.Interaction, meeting_id: int, member: discord.Member):
    if not await require_manager(interaction): return
    with db() as con:
        m=con.execute('SELECT id FROM meetings WHERE id=? AND guild_id=?',(meeting_id,interaction.guild.id)).fetchone()
        if not m: return await interaction.response.send_message('Meeting not found.',ephemeral=True)
        con.execute('INSERT INTO meeting_attendance(meeting_id,user_id,manually_added) VALUES(?,?,1) ON CONFLICT(meeting_id,user_id) DO UPDATE SET manually_added=1',(meeting_id,member.id))
    await interaction.response.send_message(f'Manually marked {member.mention} as attended for meeting #{meeting_id}.',ephemeral=True)


@bot.tree.command(name='meeting_remove_attendee',description='Remove a staff meeting attendance record')
@app_commands.guild_only()
async def meeting_remove_attendee(interaction: discord.Interaction, meeting_id: int, member: discord.Member):
    if not await require_manager(interaction): return
    with db() as con:
        con.execute('DELETE FROM meeting_attendance WHERE meeting_id=? AND user_id=? AND meeting_id IN (SELECT id FROM meetings WHERE guild_id=?)',(meeting_id,member.id,interaction.guild.id))
    await interaction.response.send_message('Attendance record removed.',ephemeral=True)


@bot.tree.command(name='meeting_attendance',description='View recorded meeting attendance')
@app_commands.guild_only()
async def meeting_attendance(interaction: discord.Interaction, meeting_id: int):
    if not await require_manager(interaction): return
    with db() as con:
        m=con.execute('SELECT title FROM meetings WHERE id=? AND guild_id=?',(meeting_id,interaction.guild.id)).fetchone()
        if not m: return await interaction.response.send_message('Meeting not found.',ephemeral=True)
        rows=con.execute('SELECT user_id,seconds,manually_added FROM meeting_attendance WHERE meeting_id=?',(meeting_id,)).fetchall()
    lines=[f'<@{r[0]}> — {r[1]//60} min'+(' (manual)' if r[2] else '')+(' ✓' if r[2] or r[1]>=600 else ' (below 10 min)') for r in rows]
    await interaction.response.send_message(embed=discord.Embed(title=f'Attendance: {m[0]}',description='\n'.join(lines)[:4000] or 'No attendance yet.',color=embed_color(interaction.guild.id)),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='monthly_report',description='View monthly staff performance')
@app_commands.guild_only()
async def monthly_report(interaction: discord.Interaction, member: discord.Member, year: int, month: app_commands.Range[int,1,12]):
    if not await require_manager(interaction): return
    first=date(year,month,1)
    last=date(year,month,calendar.monthrange(year,month)[1])
    with db() as con:
        # Weeks are Sunday-based. Include weeks whose start falls in month; weekly summaries are approximate at boundaries.
        rows=con.execute('SELECT messages,voice_seconds,event_credits,active_days,week FROM activity WHERE guild_id=? AND user_id=? AND week>=? AND week<=?',(interaction.guild.id,member.id,(first-timedelta(days=6)).isoformat(),last.isoformat())).fetchall()
        meetings=con.execute('''SELECT COUNT(*) FROM meeting_attendance a JOIN meetings m ON m.id=a.meeting_id WHERE m.guild_id=? AND a.user_id=? AND m.start_at>=? AND m.start_at<? AND (a.manually_added=1 OR a.seconds>=600)''',(interaction.guild.id,member.id,first.isoformat(),(last+timedelta(days=1)).isoformat())).fetchone()[0]
    e=discord.Embed(title=f'Monthly Staff Report — {first.strftime("%B %Y")}',description=member.mention,color=embed_color(interaction.guild.id))
    e.add_field(name='Messages (overlapping weeks)',value=str(sum(r[0] for r in rows)))
    e.add_field(name='Voice hours (overlapping weeks)',value=f'{sum(r[1] for r in rows)/3600:.1f}')
    e.add_field(name='Activity credits (overlapping weeks)',value=str(sum(credit_total(r[:3]) for r in rows)))
    e.add_field(name='Meetings attended',value=str(meetings))
    e.set_footer(text='Activity is stored weekly: month-boundary totals may include adjacent days.')
    await interaction.response.send_message(embed=e,ephemeral=True)


@bot.tree.command(name='demotion_reviews', description='View pending staff demotion reviews')
@app_commands.guild_only()
async def demotion_reviews(interaction: discord.Interaction):
    if not await require_manager(interaction):
        return

    with db() as con:
        rows = con.execute(
            """SELECT user_id, review_week, consecutive_failures
               FROM activity_reviews
               WHERE guild_id=? AND status='pending'
               ORDER BY review_week DESC, user_id
               LIMIT 20""",
            (interaction.guild.id,)
        ).fetchall()

        total = con.execute(
            """SELECT COUNT(*) FROM activity_reviews
               WHERE guild_id=? AND status='pending'""",
            (interaction.guild.id,)
        ).fetchone()[0]

    embed = discord.Embed(
        title='Crew Me | Pending Demotion Reviews',
        color=embed_color(interaction.guild.id)
    )

    if not rows:
        embed.description = 'No pending demotion reviews.'
    else:
        lines = []
        for user_id, week, failures in rows:
            lines.append(
                f'<@{user_id}>\\n'
                f'Consecutive failed weeks: **{failures}**\\n'
                f'Latest review week: **{week}**'
            )

        embed.description = '\\n\\n'.join(lines)
        embed.set_footer(
            text=f'Showing {len(rows)} of {total} pending reviews | Management decision required'
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none()
    )


@bot.tree.command(name='staff_dashboard',description='View management overview of staff activity and records')
@app_commands.guild_only()
async def staff_dashboard(interaction: discord.Interaction):
    if not await require_manager(interaction): return
    c=cfg(interaction.guild.id)
    role=interaction.guild.get_role(c[1]) if c and c[1] else None
    week=week_start(guild_now(interaction.guild.id))
    with db() as con:
        active_absences=con.execute("SELECT COUNT(*) FROM absences WHERE guild_id=? AND status='active'",(interaction.guild.id,)).fetchone()[0]
        warnings=con.execute('SELECT COUNT(*) FROM warnings WHERE guild_id=? AND revoked_at IS NULL',(interaction.guild.id,)).fetchone()[0]
        tickets=con.execute("SELECT COUNT(*) FROM staff_tickets WHERE guild_id=? AND status='open'",(interaction.guild.id,)).fetchone()[0]
        meetings=con.execute("SELECT COUNT(*) FROM meetings WHERE guild_id=? AND status='scheduled' AND end_at>=?",(interaction.guild.id,datetime.now(timezone.utc).isoformat())).fetchone()[0]
        active=con.execute('SELECT COUNT(*) FROM activity WHERE guild_id=? AND week=? AND messages+(voice_seconds/10800)*100+event_credits>=500',(interaction.guild.id,week)).fetchone()[0]
    e=discord.Embed(title='Crew Me Management Dashboard',color=embed_color(interaction.guild.id))
    for k,v in [('Staff role members',len(role.members) if role else 'Not configured'),('Weekly activity goals met',active),('Active absences',active_absences),('Active warnings',warnings),('Open staff tickets',tickets),('Upcoming / ongoing meetings',meetings)]: e.add_field(name=k,value=str(v))
    await interaction.response.send_message(embed=e,ephemeral=True)



@bot.tree.command(name='meeting_cancel',description='Cancel a scheduled staff meeting')
@app_commands.guild_only()
async def meeting_cancel(interaction: discord.Interaction, meeting_id: int):
    if not await require_manager(interaction): return
    with db() as con:
        cur=con.execute("UPDATE meetings SET status='cancelled' WHERE id=? AND guild_id=? AND status='scheduled'",(meeting_id,interaction.guild.id))
    await interaction.response.send_message('Meeting cancelled.' if cur.rowcount else 'Scheduled meeting not found.',ephemeral=True)


@bot.tree.command(name='meetings',description='List upcoming staff meetings')
@app_commands.guild_only()
async def meetings(interaction: discord.Interaction):
    if not await require_manager(interaction): return
    with db() as con:
        rows=con.execute("SELECT id,title,start_at,voice_channel_id FROM meetings WHERE guild_id=? AND status='scheduled' AND end_at>? ORDER BY start_at LIMIT 15",(interaction.guild.id,datetime.now(timezone.utc).isoformat())).fetchall()
    lines=[f'**#{r[0]}** {r[1]} — {discord.utils.format_dt(datetime.fromisoformat(r[2]),"f")} — <#{r[3]}>' for r in rows]
    await interaction.response.send_message(embed=discord.Embed(title='Upcoming Staff Meetings',description='\n'.join(lines)[:4000] or 'No upcoming meetings.',color=embed_color(interaction.guild.id)),ephemeral=True)


@bot.tree.command(name='staff_ticket_add',description='Give a participant access to this staff ticket')
@app_commands.guild_only()
async def staff_ticket_add(interaction: discord.Interaction, member: discord.Member):
    if not await require_manager(interaction): return
    with db() as con:
        row=con.execute('SELECT status FROM staff_tickets WHERE guild_id=? AND channel_id=?',(interaction.guild.id,interaction.channel.id)).fetchone()
    if not row or row[0]!='open': return await interaction.response.send_message('Run inside an open staff ticket.',ephemeral=True)
    try:
        await interaction.channel.set_permissions(member,view_channel=True,send_messages=True,read_message_history=True)
        await interaction.response.send_message(f'Added {member.mention} to this private ticket.',allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException: await interaction.response.send_message('Unable to add participant; check permissions.',ephemeral=True)


@bot.tree.command(name='staff_ticket_remove',description='Remove a participant from this staff ticket')
@app_commands.guild_only()
async def staff_ticket_remove(interaction: discord.Interaction, member: discord.Member):
    if not await require_manager(interaction): return
    with db() as con:
        row=con.execute('SELECT status FROM staff_tickets WHERE guild_id=? AND channel_id=?',(interaction.guild.id,interaction.channel.id)).fetchone()
    if not row: return await interaction.response.send_message('Run inside a staff ticket.',ephemeral=True)
    try:
        await interaction.channel.set_permissions(member,view_channel=False,send_messages=False)
        await interaction.response.send_message(f'Removed access for {member.mention}.',ephemeral=True)
    except discord.HTTPException: await interaction.response.send_message('Unable to remove participant.',ephemeral=True)


@bot.tree.command(name='staff_ticket_history',description='Review private staff discussion ticket history')
@app_commands.guild_only()
async def staff_ticket_history(interaction: discord.Interaction, member: discord.Member | None = None):
    if not await require_manager(interaction): return
    with db() as con:
        if member:
            rows=con.execute('SELECT channel_id,user_id,topic,status FROM staff_tickets WHERE guild_id=? AND user_id=? ORDER BY created_at DESC LIMIT 20',(interaction.guild.id,member.id)).fetchall()
        else:
            rows=con.execute('SELECT channel_id,user_id,topic,status FROM staff_tickets WHERE guild_id=? ORDER BY created_at DESC LIMIT 20',(interaction.guild.id,)).fetchall()
    lines=[f'<#{r[0]}> • <@{r[1]}> • **{r[3]}** — {r[2][:80]}' for r in rows]
    await interaction.response.send_message(embed=discord.Embed(title='Staff Ticket History',description='\n'.join(lines)[:4000] or 'No tickets.',color=embed_color(interaction.guild.id)),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='crewme_info',description='Show Crew Me public bot information')
@app_commands.guild_only()
async def staffy_info(interaction: discord.Interaction):
    e=discord.Embed(title='Crew Me',description='Public Discord staff management bot • Free to use',color=embed_color(interaction.guild.id))
    e.add_field(name='Version',value='0.2 beta')
    e.add_field(name='Status',value='Online')
    e.add_field(name='Getting started',value='An administrator can run `/crewme_setup`.')
    await interaction.response.send_message(embed=e,ephemeral=True)

# Voice sessions are persisted in weekly aggregates, with periodic flushing. A short
# disconnect or crash can lose only the unflushed interval, not the entire session.
_voice_sessions = {}
_meeting_sessions = {}


def voice_eligible(member):
    if member.bot or not is_staff(member) or not member.voice or not member.voice.channel:
        return False
    c=cfg(member.guild.id)
    allowed=set(filter(None,(c[3] or '').split(','))) if c else set()
    channel=member.voice.channel
    if str(channel.id) not in allowed or member.voice.self_deaf or member.voice.deaf:
        return False
    return sum(not m.bot for m in channel.members)>=2


def meeting_ids_for_voice(guild_id,channel_id,now):
    with db() as con:
        return [r[0] for r in con.execute('SELECT id FROM meetings WHERE guild_id=? AND voice_channel_id=? AND status=? AND start_at<=? AND end_at>?',(guild_id,channel_id,'scheduled',now.isoformat(),now.isoformat())).fetchall()]


def flush_voice():
    now=datetime.now(timezone.utc)
    for key,started in list(_voice_sessions.items()):
        guild=bot.get_guild(key[0]); member=guild.get_member(key[1]) if guild else None
        if not member or not voice_eligible(member):
            end=now
            _voice_sessions.pop(key,None)
        else:
            end=now
            _voice_sessions[key]=now
        elapsed=max(0,int((end-started).total_seconds()))
        if elapsed:
            # A session segment is attributed to the guild-local date/week when it ended.
            activity_update(key[0],key[1],seconds=elapsed,active_day=guild_now(key[0]).date().isoformat())
    for key,started in list(_meeting_sessions.items()):
        guild=bot.get_guild(key[0]); member=guild.get_member(key[2]) if guild else None
        meeting_id=key[1]
        eligible=False
        if member and member.voice and member.voice.channel and is_staff(member):
            eligible=meeting_id in meeting_ids_for_voice(key[0],member.voice.channel.id,now)
        if not eligible: _meeting_sessions.pop(key,None)
        else: _meeting_sessions[key]=now
        elapsed=max(0,int((now-started).total_seconds()))
        if elapsed:
            with db() as con:
                con.execute('INSERT INTO meeting_attendance(meeting_id,user_id,seconds) VALUES(?,?,?) ON CONFLICT(meeting_id,user_id) DO UPDATE SET seconds=seconds+excluded.seconds',(meeting_id,key[2],elapsed))


def refresh_voice_sessions():
    now=datetime.now(timezone.utc)
    for guild in bot.guilds:
        for channel in guild.voice_channels:
            for member in channel.members:
                key=(guild.id,member.id)
                if voice_eligible(member): _voice_sessions.setdefault(key,now)
                else: _voice_sessions.pop(key,None)
                ids=meeting_ids_for_voice(guild.id,channel.id,now) if is_staff(member) else []
                for mid in ids: _meeting_sessions.setdefault((guild.id,mid,member.id),now)
    for key in list(_meeting_sessions):
        guild=bot.get_guild(key[0]); member=guild.get_member(key[2]) if guild else None
        if not member or not member.voice or not member.voice.channel or key[1] not in meeting_ids_for_voice(key[0],member.voice.channel.id,now):
            _meeting_sessions.pop(key,None)


from discord.ext import tasks

def run_weekly_staff_evaluations():
    from datetime import date, timedelta

    with db() as con:
        guild_ids = [
            row[0] for row in con.execute(
                "SELECT guild_id FROM guild_config"
            ).fetchall()
        ]

    for guild_id in guild_ids:
        today = guild_now(guild_id).date()
        current_week = date.fromisoformat(week_start(guild_now(guild_id)))
        previous_week = (current_week - timedelta(days=7)).isoformat()

        with db() as con:
            staff_ids = [
                row[0] for row in con.execute(
                    "SELECT user_id FROM staff_memberships WHERE guild_id=?",
                    (guild_id,)
                ).fetchall()
            ]

        for user_id in staff_ids:
            with db() as con:
                existing = con.execute(
                    """SELECT 1 FROM weekly_evaluations
                       WHERE guild_id=? AND user_id=? AND week=?""",
                    (guild_id, user_id, previous_week)
                ).fetchone()

            if existing:
                continue

            status, total, days = evaluate_staff_week(
                guild_id, user_id, previous_week
            )

            if status == 'ineligible':
                continue

            if status == 'failed':
                with db() as con:
                    pending = con.execute(
                        """SELECT 1 FROM activity_reviews
                           WHERE guild_id=? AND user_id=? AND status='pending'
                           LIMIT 1""",
                        (guild_id, user_id)
                    ).fetchone()

                if not pending:
                    check_consecutive_failures(
                        guild_id, user_id, previous_week
                    )

            print(
                f'Weekly evaluation: guild={guild_id} '
                f'user={user_id} week={previous_week} '
                f'status={status} credits={total} days={days}',
                flush=True
            )


async def post_pending_review_notifications():
    with db() as con:
        rows = con.execute(
            """SELECT r.guild_id, r.user_id, r.review_week,
                      r.consecutive_failures, c.review_channel_id
               FROM activity_reviews r
               JOIN guild_config c ON c.guild_id = r.guild_id
               WHERE r.status='pending'
                 AND r.review_message_id IS NULL
                 AND c.review_channel_id IS NOT NULL"""
        ).fetchall()

    for guild_id, user_id, review_week, failures, channel_id in rows:
        guild = bot.get_guild(guild_id)
        if not guild:
            continue

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            print(f'Review channel unavailable: {channel_id}', flush=True)
            continue

        embed = discord.Embed(
            title='Crew Me | Demotion Review',
            description='Staff activity requires management review.',
            color=embed_color(guild_id)
        )
        embed.add_field(
            name='Staff Member',
            value=f'<@{user_id}>',
            inline=False
        )
        embed.add_field(
            name='Consecutive Failed Weeks',
            value=str(failures)
        )
        embed.add_field(
            name='Review Week',
            value=review_week
        )
        embed.add_field(
            name='Status',
            value='Pending',
            inline=False
        )
        embed.set_footer(
            text='Management action required | No automatic demotion'
        )

        try:
            message = await channel.send(
                embed=embed,
                view=DemotionReviewView(guild_id, user_id, review_week),
                allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException as exc:
            print(f'Unable to post review: {exc}', flush=True)
            continue

        with db() as con:
            con.execute(
                """UPDATE activity_reviews
                   SET review_message_id=?, review_message_sent_at=?
                   WHERE guild_id=? AND user_id=?
                     AND review_week=? AND status='pending'
                     AND review_message_id IS NULL""",
                (
                    message.id,
                    datetime.now(timezone.utc).isoformat(),
                    guild_id,
                    user_id,
                    review_week
                )
            )

        print(
            f'Posted review notification: guild={guild_id} '
            f'user={user_id} message={message.id}',
            flush=True
        )


@tasks.loop(seconds=60)
async def staffy_tracking_loop():
    flush_voice()
    refresh_voice_sessions()
    run_weekly_staff_evaluations()
    await post_pending_review_notifications()

@staffy_tracking_loop.before_loop
async def before_staffy_tracking():
    await bot.wait_until_ready()
    refresh_voice_sessions()

@bot.event
async def on_voice_state_update(member,before,after):
    # Re-evaluate all channel members when a user joins, leaves, moves or deafens.
    flush_voice()
    refresh_voice_sessions()

@bot.event
async def on_message(message):
    if message.guild and isinstance(message.author,discord.Member) and not message.author.bot and is_staff(message.author):
        activity_update(message.guild.id,message.author.id,messages=1,active_day=guild_now(message.guild.id).date().isoformat())
    await bot.process_commands(message)

# Start tracking on startup. Views for saved warnings/meetings are restored
# so buttons keep working across restarts.
_original_setup_hook = bot.setup_hook
async def extended_setup_hook():
    ensure_extended_tables()
    with db() as con:
        for (wid,) in con.execute('SELECT id FROM warnings WHERE revoked_at IS NULL').fetchall():
            bot.add_view(WarningNotifyView(wid))
        for (mid,) in con.execute("SELECT id FROM meetings WHERE status='scheduled'").fetchall():
            bot.add_view(MeetingRSVPView(mid))
    with db() as con:
        pending_reviews = con.execute(
            """SELECT guild_id, user_id, review_week, review_message_id
               FROM activity_reviews
               WHERE status='pending'
                 AND review_message_id IS NOT NULL"""
        ).fetchall()

    for guild_id, user_id, review_week, message_id in pending_reviews:
        bot.add_view(
            DemotionReviewView(guild_id, user_id, review_week),
            message_id=message_id
        )

    await _original_setup_hook()
    if not staffy_tracking_loop.is_running(): staffy_tracking_loop.start()
bot.setup_hook=extended_setup_hook



@bot.event
async def on_member_update(before, after):
    config = cfg(after.guild.id)

    if not config or not config[1]:
        return

    staff_role_id = config[1]

    had_staff_role = any(
        role.id == staff_role_id for role in before.roles
    )
    has_staff_role = any(
        role.id == staff_role_id for role in after.roles
    )

    if not had_staff_role and has_staff_role:
        staff_since = guild_now(after.guild.id).date().isoformat()

        with db() as con:
            con.execute("""
                INSERT INTO staff_memberships
                    (guild_id, user_id, staff_since)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET staff_since=excluded.staff_since
            """, (
                after.guild.id,
                after.id,
                staff_since
            ))

    elif had_staff_role and not has_staff_role:
        with db() as con:
            con.execute("""
                DELETE FROM staff_memberships
                WHERE guild_id=? AND user_id=?
            """, (after.guild.id, after.id))

async def sync_existing_staff():
    await bot.wait_until_ready()

    with db() as con:
        configs = con.execute(
            'SELECT guild_id, staff_role_id FROM guild_config'
        ).fetchall()

    for guild_id, staff_role_id in configs:
        guild = bot.get_guild(guild_id)
        if guild is None:
            print(f'Staff sync: server {guild_id} unavailable')
            continue

        role = guild.get_role(staff_role_id)
        if role is None:
            print(f'Staff sync: staff role missing in {guild.name}')
            continue

        try:
            members = [member async for member in guild.fetch_members(limit=None)]
        except Exception as exc:
            print(f'Staff sync failed for {guild.name}: {exc}')
            continue

        staff_ids = {
            member.id for member in members
            if not member.bot and role in member.roles
        }

        start_date = guild_now(guild_id).date().isoformat()

        with db() as con:
            for user_id in staff_ids:
                con.execute(
                    '''INSERT OR IGNORE INTO staff_memberships
                       (guild_id, user_id, staff_since)
                       VALUES (?, ?, ?)''',
                    (guild_id, user_id, start_date)
                )

            existing = con.execute(
                'SELECT user_id FROM staff_memberships WHERE guild_id=?',
                (guild_id,)
            ).fetchall()

            for (user_id,) in existing:
                if user_id not in staff_ids:
                    con.execute(
                        '''DELETE FROM staff_memberships
                           WHERE guild_id=? AND user_id=?''',
                        (guild_id, user_id)
                    )

        print(f'Staff sync complete: {guild.name} — {len(staff_ids)} staff')


@bot.event
async def on_ready():
    print(f'Crew Me online as {bot.user}')
    await sync_existing_staff()


if __name__ == '__main__':
    if not TOKEN or TOKEN == 'put_your_bot_token_here':
        raise SystemExit('Set DISCORD_TOKEN in your .env file first.')
    bot.run(TOKEN)
