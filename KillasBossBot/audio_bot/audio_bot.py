"""
City Of Boombox -- audio verification bot (KILLA'S BOSS).

Checks whether a Roblox audio asset is actually usable from the game and reports
a plain reason when it isn't, so you can catch bad ids before you wire them into
the audio player.

  /verify <asset id or url>   -- validate a Roblox AUDIO asset.
                                 Accepts a plain number, a rbxassetid:// url, or
                                 a roblox.com asset link. Replies with a green
                                 embed on success, a red one with a concrete
                                 reason on failure.

  /verifygroup add|remove|unlock|delete [<group id>] -- manage which groups'
    audio is grantable. `remove` marks a group LOCKED (stays listed, can't grant).

Killa feature commands (forked from typicaalusername/scope, GPL-3.0), gated to
the server Owner, Head Mods and Mods:
  /analyze <file>             -- loudness (LUFS/peak) + waveform PNG + playable file.
  /cr <file> [preset]       -- apply a key/speed shift preset, or Auto-Fit to find
                                a shift the recogniser can't identify -> mp3 + spectrogram.
  /roblox <file>              -- two-pass ogg compression simulation.
  /download <url> [kind]      -- download a YouTube/web video (yt-dlp): audio mp3 or <=720p mp4.
  /monitor <asset id>         -- watch a Roblox asset's moderation status.
  /ping                       -- host CPU/RAM/GPU/OS health check.
  /reload                     -- re-sync the command tree (owner only).
  /whitelist add/remove/list  -- manage the extra staff allow-list (owner only).

Run it:

  py -3.10 audio_bot.py        (in the folder with a filled-in .env)
  or double-click run_audio_bot.bat   (pins the 3.10 that has the auto-fit deps)

Env (same file as the main bot, or its own):
  DISCORD_TOKEN   -- token of the bot app you make for THIS bot
  GUILD_ID        -- server id for instant slash-command registration
  ALLOWED_IDS     -- comma-separated user ids allowed to run /verify (empty = everyone)
"""

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import discord
from discord import app_commands
from dotenv import load_dotenv

# This is a SEPARATE Discord application ("KILLA'S BOSS") with its OWN token in THIS
# folder's .env. Load it first (overriding) so the KILLA'S BOSS token is authoritative,
# then let the parent video bot .env (VidBot) fill in any shared vars without overriding ours.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # so `import verify_audio` works no matter how we're run

load_dotenv(os.path.join(HERE, ".env"), override=True)
load_dotenv(os.path.join(os.path.dirname(HERE), ".env"))

import verify_audio  # noqa: E402
import grant_audio  # noqa: E402
import killa_tools  # noqa: E402

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID", "")
# The single server whose Head Mod / Mod roles are trusted for the ADMIN commands.
# A member with a matching role in a DIFFERENT server doesn't count. The owner and
# whitelisted staff (/whitelist) CAN use the base commands from anywhere (other
# guilds, DMs); only the owner can run the owner-only commands.
PRIMARY_GUILD_ID = int(GUILD_ID) if GUILD_ID.strip().isdigit() else None
ALLOWED = {
    int(x) for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip().isdigit()
}
# /verify may only be used in this channel (name or id). Default: bot-commands.
COMMAND_CHANNEL = os.getenv("COMMANDS_CHANNEL", "bot-commands").strip().lower()
# Roles allowed to run /verifygroup (substring match on role name). Default covers
# Owner, Head Mods, Mods.
VERIFYGROUP_ROLES = {
    r.strip().lower() for r in os.getenv("VERIFYGROUP_ROLES", "owner,head mod,mod").split(",")
    if r.strip()
}
# Exact Discord role IDs treated as staff (Mods / Head Mods), matched by role.id.
# More reliable than the name-substring above when role names don't contain "mod".
STAFF_ROLE_IDS = {
    int(x) for x in os.getenv("STAFF_ROLE_IDS", "").split(",") if x.strip().isdigit()
}
# Discord user ids treated as the owner for the owner-only Killa commands
# (/reload, /whitelist). The server owner always counts; this list is extra.
OWNER_IDS = {
    int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()
}
# Persisted allow-list of extra user ids granted staff access (managed by
# /whitelist). It is OR'd into is_authorized, on top of the role check, so a
# whitelisted user can use the base commands without holding a mod role.
STAFF_WHITELIST_FILE = os.path.join(HERE, "staff_whitelist.json")
# The Discord id of the application owner (detected at connect via
# bot.application.owner). Lets the owner run staff commands from a DM, where
# there are no guild roles to check.
APP_OWNER_ID = None

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Fill in .env first.")

GREEN = 0x4CAF50
RED = 0xE53935
PURPLE = 0x4E2A6E   # KILLA'S BOSS brand accent (deep violet, slightly brightened from #3A194C)

# Where command usage is appended (next to the scripts) so the console log is
# also a persistent file. Line format: "[YYYY-MM-DD HH:MM:SS] user Name (id) used /cmd ...".
USAGE_LOG = os.path.join(HERE, "usage.log")
# Cooldown (seconds) between /cr auto-fit runs, per user. Auto-fit contacts
# Shazam repeatedly (rate-limited by device id) and takes a while, so it is
# throttled to stop people hammering it.
CR_COOLDOWN_SECONDS = int(os.getenv("CR_COOLDOWN_SECONDS", "300") or 300)

# /monitor polling. Polls each watched asset every MONITOR_INTERVAL_SECONDS, and
# gives up after MONITOR_TIMEOUT_SECONDS (a Roblox moderation review rarely takes
# longer; the interaction token also decays, so a short window is the honest cap).
MONITOR_INTERVAL_SECONDS = float(os.getenv("MONITOR_INTERVAL_SECONDS", "6") or 6)
MONITOR_TIMEOUT_SECONDS = float(os.getenv("MONITOR_TIMEOUT_SECONDS", "900") or 900)


def _log(line):
    """Write a log line to the command-prompt console and append it to the usage log.

    The console line is what you see when you run the bot (``python audio_bot.py``);
    the same line is appended to usage.log so it survives the session too.
    """
    full = time.strftime("[%Y-%m-%d %H:%M:%S] ") + line
    print(full, flush=True)
    try:
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(full + "\n")
    except OSError:
        pass


# Session bookkeeping. Everything writes to the ONE file (usage.log) -- no per-session
# files -- and each command is logged exactly once (in on_interaction). _cmd_count
# feeds the end-of-session summary so the SESSION ENDED line is never duplicated.
_session_start = time.time()
_cmd_count = 0
_session_end_logged = False


def _log_session_end():
    """Append a single end-of-session summary line to usage.log (idempotent).

    Guarded so a shutdown that calls this more than once (e.g. an error path plus
    a clean exit) can't write two 'SESSION ENDED' lines for one run.
    """
    global _session_end_logged
    if _session_end_logged:
        return
    _session_end_logged = True
    dur = int(time.time() - _session_start)
    _log(f"SESSION ENDED - {_cmd_count} command(s) over {dur}s")


class AudioBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        # Mirror the owner's presence: the bot shows online only while the owner is
        # Online or on Do Not Disturb, and appears offline (invisible) when the owner
        # is Idle or Offline. Watching another user's status needs the two PRIVILEGED
        # intents below, which must ALSO be enabled in the Developer Portal (Bot ->
        # Privileged Gateway Intents, "Presence Intent" + "Server Members Intent").
        # Off by default so the bot always starts and stays up; set MIRROR_PRESENCE=1
        # AFTER enabling those intents. Without the intents, Discord refuses the login
        # and the process would exit (which closes the run window) -- so we default off.
        self._mirror_presence = os.getenv(
            "MIRROR_PRESENCE", "0").strip().lower() in ("1", "true", "yes", "on")
        self._presence_poll_s = max(15, int(os.getenv("MIRROR_PRESENCE_POLL", "30") or 30))
        if self._mirror_presence:
            intents.members = True
            intents.presences = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._presence_task = None
        self._applied_status = None

    # -- presence mirroring ------------------------------------------------- #
    def _owner_ids(self):
        ids = set(OWNER_IDS)
        if APP_OWNER_ID:
            ids.add(APP_OWNER_ID)
        return ids

    def _owner_member(self):
        """A guild member object for the owner (app owner or OWNER_IDS), or None."""
        for uid in self._owner_ids():
            for guild in self.guilds:
                member = guild.get_member(uid)
                if member:
                    return member
        return None

    @staticmethod
    def _target_status(owner_status):
        # Online and DND count as active -> the bot is online. Idle/Offline turn the
        # bot invisible (it looks offline, and off-limits while the owner is away).
        if owner_status in (discord.Status.online, discord.Status.dnd):
            return discord.Status.online
        return discord.Status.invisible

    async def _apply_owner_presence(self):
        """One pass: read the owner's status and flip this bot's own status to match.

        Fail-open: if the owner can't be resolved (no shared guild, or the owner id
        isn't a member anywhere), we leave the bot as-is rather than accidentally
        hiding it. Logs what it's doing so the behaviour is visible in the console.
        """
        if not self._mirror_presence:
            return
        try:
            member = self._owner_member()
            if member is None:
                self._log_once_mirror(
                    f"presence: can't see owner ids {sorted(self._owner_ids()) or 'unknown'} "
                    "(no shared guild) -- leaving bot visible")
                return
            target = self._target_status(member.status)
            if target != self._applied_status:
                _log(f"presence: owner {member.id} is {member.status.name} -> bot "
                     f"{'online' if target == discord.Status.online else 'offline'}")
                await self.change_presence(status=target)
                self._applied_status = target
        except Exception as e:  # noqa: BLE001
            _log(f"mirror presence error: {e}")

    async def _mirror_owner_presence(self):
        """Poll the owner's status and flip this bot's own presence to match.

        The on_presence_update handler reacts instantly; this loop is a safety net
        that catches a missed update within _presence_poll_s. Runs until the client
        closes. Started in setup_hook so it can't be skipped by a failed sync.
        """
        await self.wait_until_ready()
        while not self.is_closed():
            await self._apply_owner_presence()
            await asyncio.sleep(self._presence_poll_s)

    async def on_presence_update(self, before, after):
        """React instantly to a member's status change (fires for the owner too)."""
        if self._mirror_presence and after.id in self._owner_ids():
            await self._apply_owner_presence()

    def _log_once_mirror(self, msg):
        if not getattr(self, "_mirror_warned", False):
            self._mirror_warned = True
            _log(msg)

    async def setup_hook(self):
        # Make every command usable in a guild, in the bot's DMs, and in private
        # channels. It is registered GLOBALLY only -- no guild-scoped copy. A
        # global command shows exactly once in every server the bot is in, so
        # also registering a guild-scoped copy of the same name would make it
        # appear TWICE in a server (that was the "doubled commands" bug).
        from discord.app_commands import AppCommandContext, AppInstallationType

        for cmd in self.tree.get_commands():
            cmd.allowed_contexts = AppCommandContext(
                guild=True, dm_channel=True, private_channel=True)
            # Both guild and user install. The commands are registered globally
            # only (no guild-scoped copy), so user install does NOT create a
            # second entry -- it just lets the same global command be used as a
            # user app (DMs / private) as well as in a server. See the "doubled
            # commands" note below: that was global + guild registrations, not
            # the install type.
            cmd.allowed_installs = AppInstallationType(guild=True, user=True)

        # Start the presence watcher HERE (setup_hook runs right after login, before
        # on_ready / command sync), so a slow or failing command sync can never leave
        # the bot running without presence mirroring.
        if self._mirror_presence and self._presence_task is None:
            self._presence_task = asyncio.create_task(self._mirror_owner_presence())

        # The single global sync is done after 'ready' (see _sync_all_guilds),
        # and any stale guild-scoped commands left over from an older config are
        # deleted there (_remove_guild_command_duplicates) so the server shows
        # one clean set.


bot = AudioBot()


def _brand_footer(embed, extra=None):
    """Apply the branded KILLA'S BOSS footer (bot name + avatar icon)."""
    icon = None
    try:
        icon = bot.user.display_avatar.url if bot.user else None
    except Exception:
        icon = None
    text = "KILLA'S BOSS" + (f" • {extra}" if extra else "")
    embed.set_footer(text=text, icon_url=icon)
    return embed


# Promotional links shown on the branded cards (same as /ping advertises).
_ADVERT_DISCORD = "https://discord.gg/AWFAFSCbXQ"
_ADVERT_GROUP = ("https://www.roblox.com/communities/101224973/"
                 "KILLAS-HUB-3-0#!/about")


def _advertise_fields(embed):
    """Add the KILLA'S BOSS promotion fields (Discord invite + Roblox group)."""
    embed.add_field(name="Discord",
                    value=f"[Join the server]({_ADVERT_DISCORD})", inline=False)
    embed.add_field(name="Roblox Group",
                    value=f"[KILLA'S HUB 3.0]({_ADVERT_GROUP})", inline=False)
    return embed


def _whitelist_dm_embed():
    """Private message sent to a user who is newly whitelisted."""
    embed = discord.Embed(
        title="You've been verified",
        description=(
            "You can now use **KILLA'S BOSS** as an app on your user account — "
            "add it under Discord → User Settings → Apps, then run the slash "
            "commands in a DM with the bot or in your own server."
        ),
        color=GREEN,
    )
    _advertise_fields(embed)
    _brand_footer(embed, "staff whitelist")
    return embed


def _build_groups_embed():
    """Build the /groups card from the currently supported (verified) groups.

    Names come from the runtime JSON store where available; any group id with no
    stored name is resolved via a public group lookup (best effort). Runs off the
    event loop because a lookup can hit the network. A group shows a LOCKED badge
    when it's marked locked (via /verifygroup remove) or Roblox reports it locked.
    """
    ids = grant_audio.all_supported_ids()
    named = {g["id"]: (g.get("name") or "?")
             for g in grant_audio._load_dynamic_groups()}
    locks = grant_audio.effective_locks()
    entries = []
    for gid in sorted(ids):
        name = named.get(gid)
        if not name:
            try:
                name = grant_audio.get_group_info(gid).get("name")
            except Exception:
                name = None
        entries.append((gid, name, bool(locks.get(gid))))

    if entries:
        lines = []
        for gid, name, locked in entries:
            line = f"**{name}** — `{gid}`" if name else f"`{gid}`"
            if locked:
                line += " — **LOCKED**"
            lines.append(line)
        embed = discord.Embed(
            title="Verified Groups",
            description=(
                "These Roblox groups are supported, so this bot can grant their "
                "audio to your experience. Groups marked **LOCKED** can no longer "
                "be granted (a locked group can't have new audio)."
            ),
            color=PURPLE,
        )
        embed.add_field(name="Groups", value="\n".join(lines), inline=False)
    else:
        embed = discord.Embed(
            title="Verified Groups",
            description=(
                "No groups are supported yet. Run `/verifygroup add <group id>` to "
                "verify a Roblox group so the bot can grant its audio."
            ),
            color=PURPLE,
        )
    embed.add_field(
        name="How to add",
        value=("Ask an admin to run `/verifygroup add <group id>` to support a new "
               "group."),
        inline=False,
    )
    _advertise_fields(embed)
    _brand_footer(embed, "verified groups")
    return embed


def _in_primary_guild(interaction: discord.Interaction) -> bool:
    """True when the command ran in the single trusted server (GUILD_ID)."""
    return interaction.guild is not None and interaction.guild.id == PRIMARY_GUILD_ID


def is_owner(interaction: discord.Interaction) -> bool:
    """The bot owner: the app owner, any id in OWNER_IDS, or the owner of the
    primary guild. The owner is ALWAYS allowed -- any server, DMs, any channel,
    even when the bot is showing offline (owner away)."""
    if APP_OWNER_ID and interaction.user.id == APP_OWNER_ID:
        return True
    if interaction.user.id in OWNER_IDS:
        return True
    if _in_primary_guild(interaction) and interaction.guild.owner_id == interaction.user.id:
        return True
    return False


def _has_mod_role(member: discord.Member) -> bool:
    """True when a guild member holds a Head Mod / Mod role.

    Matched by role id (STAFF_ROLE_IDS) or by role name against VERIFYGROUP_ROLES
    (default 'owner','head mod','mod').
    """
    for role in getattr(member, "roles", []):
        if role.id in STAFF_ROLE_IDS:
            return True
        low = role.name.lower()
        for token in VERIFYGROUP_ROLES:
            if token in low:
                return True
    return False


def is_mod(interaction: discord.Interaction) -> bool:
    """A Head Mod / Mod of the PRIMARY guild. Roles in a different server don't count."""
    return _in_primary_guild(interaction) and _has_mod_role(interaction.user)


def bot_is_offline() -> bool:
    """True when presence mirroring turned the bot invisible (the owner is away).

    While the bot is showing offline, commands lock down to the owner only. If
    presence mirroring is disabled this always returns False (no lock).
    """
    return bool(bot._mirror_presence and bot._applied_status == discord.Status.invisible)


def is_whitelisted(interaction: discord.Interaction) -> bool:
    """True when the user is on the extra staff allow-list (/whitelist add).

    Grants the BASE (non-admin, non-owner) commands from anywhere -- the primary
    guild, another server, or the bot's DMs -- so a whitelisted person can use the
    bot even outside the server. It does NOT grant admin commands (/verifygroup,
    /ping) or owner-only commands (/reload, /whitelist).
    """
    return interaction.user.id in _read_staff_whitelist()


def is_authorized(interaction: discord.Interaction) -> bool:
    """Master gate for every base (non-admin) command.

    Allowed: the owner (always -- any server/DM/channel, even while the bot is
    offline), a whitelisted staff member (any server/DM/channel, while the bot is
    online), or a Head Mod / Mod of the primary guild (any channel in that server,
    while the bot is online). Everyone else is denied. Admin commands check
    is_admin_authorized instead, so a whitelisted user can't use those.
    """
    if is_owner(interaction):
        return True
    if bot_is_offline():
        return False
    if is_whitelisted(interaction):
        return True
    return is_mod(interaction)


def is_admin_authorized(interaction: discord.Interaction) -> bool:
    """Gate for the admin commands (/verifygroup, /ping).

    Stricter than is_authorized: the owner always, plus a Head Mod / Mod of the
    primary guild while the bot is online. A whitelisted (non-mod) user is NOT
    admin-authorized -- the whitelist only unlocks the base commands.
    """
    if is_owner(interaction):
        return True
    if bot_is_offline():
        return False
    return is_mod(interaction)


def in_commands_channel(interaction: discord.Interaction) -> bool:
    """True when the command ran in the configured command channel.

    A DM (interaction.guild is None) has no guild channel, so it always passes --
    the owner gate still applies on top."""
    if interaction.guild is None:
        return True
    channel = interaction.channel
    if channel is None or getattr(channel, "name", None) is None:
        return False
    if COMMAND_CHANNEL.isdigit():
        return str(channel.id) == COMMAND_CHANNEL
    return channel.name.lower() == COMMAND_CHANNEL


def _read_staff_whitelist() -> set:
    try:
        with open(STAFF_WHITELIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {int(x) for x in (data.get("user_ids", []) or [])}
    except (FileNotFoundError, ValueError, TypeError):
        return set()


def _write_staff_whitelist(user_ids: set):
    data = {"user_ids": sorted(user_ids)}
    with open(STAFF_WHITELIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---- compatibility shims so existing call sites stay simple ------------------- #

def is_allowed(interaction: discord.Interaction) -> bool:
    return is_authorized(interaction)


def is_verifygroup_authorized(interaction: discord.Interaction) -> bool:
    return is_authorized(interaction)


def is_staff(interaction: discord.Interaction) -> bool:
    return is_authorized(interaction)


def is_admin_command_authorized(interaction: discord.Interaction) -> bool:
    # Admin commands: the owner always, plus a primary-guild Head Mod / Mod while
    # the bot is online. Truly owner-only subcommands (/reload, /whitelist) check
    # is_owner again inside their handler. Whitelisted non-mods are NOT admin-
    # authorized (that's is_authorized).
    return is_admin_authorized(interaction)


async def _deny_staff(interaction):
    if bot_is_offline():
        await interaction.response.send_message(
            "The bot is currently offline (the owner is away) -- only the owner can "
            "use it right now.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "Only the bot owner, a whitelisted staff member, or a Head Mod / Mod of "
            "this server can use this.", ephemeral=True)


async def _deny_admin(interaction):
    await interaction.response.send_message(
        "Only the owner can use this in DMs; inside this server only the Owner, "
        "Head Mod, or Mod roles can.", ephemeral=True)


async def _deny_channel(interaction):
    await interaction.response.send_message(
        f"This command can only be used in **#{COMMAND_CHANNEL}**.", ephemeral=True
    )


def channel_ok(interaction: discord.Interaction) -> bool:
    """Authorized users (owner, or a primary-guild Head Mod / Mod) may run in any
    channel; everyone else is denied outright by the gate before this is reached."""
    return is_authorized(interaction)


async def _require(interaction, staff=True):
    """Return True when the caller may run a Killa feature, else send a denial."""
    if not is_authorized(interaction):
        await _deny_staff(interaction)
        return False
    return True


async def _require_admin(interaction):
    """Gate for the admin commands: owner always, primary-guild Head Mod / Mod online.

    Whitelisted users (non-mods) pass is_authorized but NOT this gate, so they can
    use the base commands but can't run /verifygroup or /ping.
    """
    if not is_admin_authorized(interaction):
        await _deny_admin(interaction)
        return False
    return True


async def _require_owner(interaction):
    """Strict owner-only gate (used by /reload and /whitelist)."""
    if not is_owner(interaction):
        await interaction.response.send_message(
            "This command is owner-only.", ephemeral=True)
        return False
    return True


async def _save_attachment(workdir, attachment):
    """Download a Discord attachment to disk, return the local path."""
    data = await attachment.read()
    ext = os.path.splitext(attachment.filename or "a.mp3")[1] or ".mp3"
    path = os.path.join(workdir, f"input_{int(time.time() * 1000)}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


async def _safe_send(fn, *args, **kwargs):
    """Await a send call but never let a stale interaction crash a bg task."""
    try:
        await fn(*args, **kwargs)
    except Exception:
        pass


@bot.tree.command(
    name="verify",
    description="Check whether a Roblox audio asset can be used in-game (and why not).",
)
@app_commands.describe(
    asset="The audio asset id, rbxassetid:// url, or roblox.com asset link to check.",
)
async def verify_slash(interaction: discord.Interaction, asset: str):
    if not is_authorized(interaction):
        await _deny_staff(interaction)
        return

    await interaction.response.defer(thinking=True)
    try:
        asset_id = await asyncio.to_thread(verify_audio.extract_asset_id, asset)
    except verify_audio.AssetError as e:
        await interaction.followup.send(
            embed=discord.Embed(title="Not a valid id", color=RED,
                                description=str(e)),
            ephemeral=True,
        )
        return

    # Roblox calls are synchronous + rate-limited; run off the event loop.
    try:
        result = await asyncio.to_thread(grant_audio.verify, asset_id)
    except grant_audio.GrantError as e:
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Audio rejected ({asset_id})",
                color=RED,
                description=f"**Reason:** {e}",
            ),
            ephemeral=True,
        )
        return
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Verification error ({asset_id})",
                color=discord.Color.orange(),
                description=f"Something went wrong talking to Roblox: `{e}`",
            ),
            ephemeral=True,
        )
        return

    title = f"Verified & granted ({asset_id})" if result.get("granted") else f"Audio OK ({asset_id})"
    embed = discord.Embed(
        title=title, color=GREEN,
        description=result["message"] +
        "\n\nThis audio is now **cached as verified** across every server, so any "
        "player gets instant playback (no re-check).")

    embed.set_footer(text=f"Verified by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="verifygroup",
    description="Verify (add), remove, or manage the Roblox groups this bot grants audio for.",
)
@app_commands.describe(
    action="add, remove, unlock, or delete.",
    group="The Roblox group id to add/remove. Use /groups to view the list.",
)
async def verifygroup_slash(interaction: discord.Interaction,
                            action: str, group: str = None):
    if not await _require_admin(interaction):
        return

    await interaction.response.defer(thinking=True, ephemeral=False)
    action = (action or "").strip().lower()
    # Back-compat: /verifygroup <id> (no action) is treated as add.
    if action not in ("add", "remove", "unlock", "delete") and group is None:
        if action.replace("-", "").isdigit():
            group = action
            action = "add"

    if action in ("add", "remove", "unlock", "delete"):
        if not group:
            embed = discord.Embed(
                title="Pick a group",
                description=(
                    "Pass the group's numeric id to `add`, `remove`, `unlock`, or "
                    "`delete`, e.g. `/verifygroup remove 15611107`."
                ),
                color=PURPLE,
            )
            _brand_footer(embed, "verifygroup")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        try:
            gid = int(group.strip())
        except (ValueError, TypeError):
            embed = discord.Embed(
                title="Not a valid group id", color=RED,
                description="Pass the group's numeric id, e.g. `15611107`.")
            _brand_footer(embed, "verifygroup")
            await interaction.followup.send(embed=embed, ephemeral=False)
            return

        if action == "remove":
            await _verifygroup_remove(interaction, gid)
            return
        if action == "unlock":
            await _verifygroup_unlock(interaction, gid)
            return
        if action == "delete":
            await _verifygroup_delete(interaction, gid)
            return

        # ---- add (was the whole of the old /verifygroup) ----
        try:
            result = await asyncio.to_thread(grant_audio.verify_group, gid)
        except grant_audio.GrantError as e:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"Could not verify group ({gid})",
                    color=RED,
                    description=f"**Reason:** {e}",
                ),
                ephemeral=False,
            )
            return
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"Group verify error ({gid})",
                    color=discord.Color.orange(),
                    description=f"Something went wrong: `{e}`",
                ),
                ephemeral=False,
            )
            return

        if result.get("captcha"):
            embed = discord.Embed(
                title=f"CAPTCHA needed to join group ({result['id']})",
                color=0xFB8C00,
                description=(
                    f"**{result['name']}** (id **{result['id']}**) was added to the "
                    f"supported groups list, so the bot can grant its audio.\n\n"
                    f"{result['message']}\n\n"
                    f"Complete the CAPTCHA manually (log into Roblox as the bot account "
                    f"and join the group), then run `/verifygroup add {result['id']}` again."
                ),
            )
        elif result.get("joined") or result.get("already_member"):
            embed = discord.Embed(
                title=f"Group verified & joined ({result['id']})",
                color=GREEN,
                description=(
                    f"**{result['name']}** (id **{result['id']}**) was added to the "
                    f"supported groups.\n\n{result.get('message') or ''}\n\n"
                    f"The bot can now grant this group's audio to your game "
                    f"(universe **{grant_audio.GRANT_UNIVERSE_ID}**)."
                ),
            )
        else:
            embed = discord.Embed(
                title=f"Group added, but not joined ({result['id']})",
                color=0xFB8C00,
                description=(
                    f"**{result['name']}** (id **{result['id']}**) was added to the "
                    f"supported groups list, but the account could **not** join the group.\n\n"
                    f"{result['message']}\n\n"
                    f"Grants on this group's audio may be refused until the account is a "
                    f"member. If the .env ROBLOX_COOKIE is stale, refresh it and re-run."
                ),
            )
        embed.set_footer(text=f"Verified by {interaction.user.display_name}")
        # posted in-channel (ephemeral=False) so the person asking can see it
        await interaction.followup.send(embed=embed, ephemeral=False)
        return

    # Unknown action.
    embed = discord.Embed(
        title="Add, remove, unlock, or delete",
        description=(
            "Pass `action` as `add`, `remove`, `unlock`, or `delete` "
            "(e.g. `/verifygroup add 15611107`). Use `/groups` to view the list."
        ),
        color=PURPLE,
    )
    _brand_footer(embed, "verifygroup")
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _verifygroup_remove(interaction: discord.Interaction, gid: int):
    """Handle `/verifygroup remove <id>` -- marks the group LOCKED (stays on the
    list, but can no longer be granted) instead of deleting it."""
    try:
        result = await asyncio.to_thread(grant_audio.mark_group_locked, gid)
    except grant_audio.GrantError as e:
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Could not lock group ({gid})",
                color=RED,
                description=f"**Reason:** {e}",
            ), ephemeral=False)
        return
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Group lock error ({gid})",
                color=discord.Color.orange(),
                description=f"Something went wrong: `{e}`",
            ), ephemeral=False)
        return

    if result and result.get("name"):
        embed = discord.Embed(
            title=f"Group locked ({result['id']})",
            color=0xFB8C00,
            description=(
                f"**{result['name']}** (id **{result['id']}**) is now **LOCKED**.\n\n"
                f"It stays on the verified list but can no longer be granted audio, "
                f"and it can't have new audio made in it.\n\n"
                f"The in-game Groups tab has been updated live. Use "
                f"`/verifygroup unlock {result['id']}` to grant it again."
            ),
        )
    else:
        embed = discord.Embed(
            title=f"Group not in supported list ({gid})",
            color=0xFB8C00,
            description=f"Group `{gid}` is not a supported group, so nothing was changed.",
        )
    embed.set_footer(text=f"Locked by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=False)


async def _verifygroup_unlock(interaction: discord.Interaction, gid: int):
    """Handle `/verifygroup unlock <id>` -- clears the LOCKED flag."""
    try:
        result = await asyncio.to_thread(grant_audio.unlock_supported_group, gid)
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Group unlock error ({gid})",
                color=discord.Color.orange(),
                description=f"Something went wrong: `{e}`",
            ), ephemeral=False)
        return

    if result and result.get("name"):
        embed = discord.Embed(
            title=f"Group unlocked ({result['id']})",
            color=GREEN,
            description=(
                f"**{result['name']}** (id **{result['id']}**) is no longer locked.\n\n"
                f"The bot can grant its audio again."
            ),
        )
    else:
        embed = discord.Embed(
            title=f"Group not in supported list ({gid})",
            color=0xFB8C00,
            description=f"Group `{gid}` is not a supported group, so nothing was changed.",
        )
    embed.set_footer(text=f"Unlocked by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=False)


async def _verifygroup_delete(interaction: discord.Interaction, gid: int):
    """Handle `/verifygroup delete <id>` -- removes the group from the list entirely."""
    try:
        result = await asyncio.to_thread(grant_audio.delete_supported_group, gid)
    except grant_audio.GrantError as e:
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Could not delete group ({gid})",
                color=RED,
                description=f"**Reason:** {e}",
            ), ephemeral=False)
        return
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"Group delete error ({gid})",
                color=discord.Color.orange(),
                description=f"Something went wrong: `{e}`",
            ), ephemeral=False)
        return

    if result and result.get("name"):
        embed = discord.Embed(
            title=f"Group deleted ({result['id']})",
            color=RED,
            description=(
                f"**{result['name']}** (id **{result['id']}**) was removed from the "
                f"supported groups list.\n\nThe in-game Groups tab has been updated live."
            ),
        )
    else:
        embed = discord.Embed(
            title=f"Group not in supported list ({gid})",
            color=0xFB8C00,
            description=f"Group `{gid}` is not a supported group, so nothing was changed.",
        )
    embed.set_footer(text=f"Deleted by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=False)


# --------------------------------------------------------------------------- #
# Killa feature commands (forked from typicaalusername/scope, GPL-3.0, gated to
# Owner / Head Mods / Mods / owner-whitelisted users)
# --------------------------------------------------------------------------- #
active_monitors = {}  # asset id -> asyncio.Task
_watcher_display = {}  # asset id -> display name (for the 'list' view)

# /cr auto-fit throttle. Only one auto-fit may run at a time (it hits Shazam,
# which rate-limits by device id), and each user gets a per-run cooldown so they
# can't start a new fit immediately after one ends.
_cr_autofit_running = False
_cr_cooldown_until = {}  # user id -> epoch after which they may auto-fit again


async def _update_cr_progress(msg, lines):
    """Keep the deferred message updated with the latest auto-fit step.

    Runs alongside the worker thread; cancels when the fit is done. Any stale
    edit is swallowed so a finished run can't be disturbed by an in-flight one.
    """
    shown = 0
    while True:
        if len(lines) > shown:
            shown = len(lines)
            try:
                await msg.edit(content="Auto-fit: " + lines[-1])
            except Exception:  # noqa: BLE001
                return
        await asyncio.sleep(3)


# A state is "final" once the moderation decision is locked in. In the review
# vocabulary an asset may sit at NotReviewed / Reviewing / Pending for a while;
# only these are treated as a done deal so the watcher keeps polling through
# any intermediate state and only stops + reacts on an actual decision.
_MODERATION_FINAL = {
    "approved", "declined", "revoked", "moderated", "denied", "edited",
    "unavailable", "removed", "rejected", "reject", "failed", "failure",
    "closed", "flagged",
}


def _is_final_state(state):
    return (state or "").strip().lower() in _MODERATION_FINAL


def _codec_ext(codec):
    """Pick a sensible audio extension from a codec name for the attached file."""
    c = (codec or "").lower()
    if "vorbis" in c or "opus" in c:
        return "ogg"
    if "mp3" in c:
        return "mp3"
    if "flac" in c:
        return "flac"
    if "wav" in c or "pcm" in c:
        return "wav"
    return "ogg"


def _fresh_files(files):
    """Rebuild attachment File objects so each target gets its own open handle.

    A discord.File is read from its current fp position on send; reusing the same
    object across a second send yields an empty attachment (fp is at EOF). Files
    backed by a real path are reopened here; others are passed through as-is.
    """
    out = []
    for f in files or []:
        src = getattr(f, "fp", None)
        path = getattr(src, "name", None)
        if path:
            out.append(discord.File(str(path), filename=f.filename))
        else:
            out.append(f)
    return out


def _monitor_embed(asset_id, display, prev, cur, description=None):
    """Fancy embed for a moderation state change."""
    cur_l = (cur or "").strip().lower()
    if cur_l == "approved":
        title, color = "Audio Accepted", PURPLE
    elif cur_l in ("declined", "denied", "revoked", "moderated"):
        title, color = "Audio Declined", RED
    else:
        title, color = f"Status: {cur}", PURPLE
    e = discord.Embed(title=title, color=color)
    e.add_field(name="Asset", value=f"`{asset_id}`", inline=True)
    e.add_field(name="Name", value=display or "—", inline=True)
    if description:
        e.add_field(name="Description", value=(description[:120] or "—"), inline=False)
    e.add_field(name="Changed", value=f"{prev} → **{cur}**", inline=False)
    e.add_field(name="Link", value=f"https://roblox.com/library/{asset_id}", inline=False)
    _brand_footer(e, "moderation monitor")
    return e


def _monitor_accepted_embed(asset_id, display, description, info, image="waveform.png"):
    """Fancy embed for an accepted asset, mirroring /analyze: the specs fields +
    the inline waveform image (set via the attached ``image`` file)."""
    e = discord.Embed(title="Audio Accepted", color=PURPLE)
    e.add_field(name="Asset", value=f"`{asset_id}`", inline=True)
    e.add_field(name="Name", value=display or "—", inline=True)
    if description:
        e.add_field(name="Description", value=(description[:120] or "—"), inline=False)
    e.add_field(name="Duration", value=info.get("duration", "—"), inline=True)
    e.add_field(name="Codec", value=info.get("codec", "—"), inline=True)
    channels = info.get("channels")
    e.add_field(name="Channels", value=str(channels) if channels else "—", inline=True)
    sample = info.get("sample_rate")
    e.add_field(name="Sample Rate", value=f"{sample} Hz" if sample else "—", inline=True)
    br = info.get("bitrate_kbps")
    e.add_field(name="Bitrate", value=f"{br} kbps" if br else "—", inline=True)
    lufs = info.get("lufs")
    e.add_field(name="Integrated Loudness", value=f"{lufs:.1f} LUFS" if lufs is not None else "—", inline=True)
    peak = info.get("peak")
    e.add_field(name="Peak", value=f"{peak:.1f} dBFS" if peak is not None else "—", inline=True)
    e.add_field(name="Link", value=f"https://roblox.com/library/{asset_id}", inline=False)
    if image:
        e.set_image(url=f"attachment://{image}")
    _brand_footer(e, "moderation monitor")
    return e


def _monitor_status_embed(title, color, fields, tag="moderation monitor"):
    """A clean, single-purpose informational embed for monitor status notices."""
    e = discord.Embed(title=title, color=color)
    for name, value in fields:
        e.add_field(name=name, value=value, inline=False)
    _brand_footer(e, tag)
    return e


class _MonitorSession:
    """Tracks every id watched by a single ``/monitor`` invocation.

    The channel the command ran in gets ONE beautified summary board that is
    EDIT-ed in place as those audios change -- it only shows COUNTS (pending /
    accepted / deleted), so a long watch never spams the chat. The owner's DMs
    get the FULL detail for each audio (its own card, edited in place, including
    the accepted download + analysis). When the command was run in a DM there is
    no channel board -- the detailed DM card is the only message.
    """

    def __init__(self, user, channel):
        self.user = user
        self.channel = channel
        self.in_dm = channel is not None and getattr(channel, "type", None) == discord.ChannelType.private
        self.board_msg = None      # channel summary Message (None when in a DM / send failed)
        self.states = {}           # asset id -> latest moderation state
        self.displays = {}         # asset id -> display name
        self.descriptions = {}     # asset id -> description
        self.pending = {}          # asset id -> True while still being watched
        self.dm_msgs = {}          # asset id -> the detailed DM Message (edited in place)

    # -- channel summary board (counts only) -------------------------------
    def _counts(self):
        accepted = deleted = pending = 0
        for aid, state in self.states.items():
            s = (state or "").strip().lower()
            if s == "approved":
                accepted += 1
            elif _is_final_state(s):
                deleted += 1
            elif self.pending.get(aid):
                pending += 1
        return pending, accepted, deleted

    def _board_embed(self):
        pending, accepted, deleted = self._counts()
        e = discord.Embed(title="Audio Monitor", color=PURPLE)
        e.add_field(name="Audio (pending)", value=str(pending), inline=True)
        e.add_field(name="Accepted", value=str(accepted), inline=True)
        e.add_field(name="Deleted", value=str(deleted), inline=True)
        watched = [aid for aid in self.pending if self.pending.get(aid)]
        if watched:
            e.add_field(name="Monitoring", value=", ".join(f"`{a}`" for a in watched), inline=False)
        _brand_footer(e, "moderation monitor")
        return e

    async def _update_board(self):
        if self.in_dm:
            return
        embed = self._board_embed()
        if self.board_msg is None:
            try:
                self.board_msg = await self.channel.send(embed=embed)
            except Exception:  # noqa: BLE001
                self.board_msg = None
            return
        try:
            await self.board_msg.edit(embed=embed)
        except Exception:  # noqa: BLE001
            try:
                self.board_msg = await self.channel.send(embed=embed)
            except Exception:  # noqa: BLE001
                self.board_msg = None

    # -- per-audio DM detail (edited in place) ------------------------------
    async def _dm_set(self, asset_id, embed, files=None):
        msg = self.dm_msgs.get(asset_id)
        if msg is not None:
            try:
                await msg.edit(embed=embed, files=_fresh_files(files))
                return
            except Exception:  # noqa: BLE001
                self.dm_msgs[asset_id] = None
        if self.user is None:
            return
        try:
            self.dm_msgs[asset_id] = await self.user.send(embed=embed, files=_fresh_files(files))
        except Exception:  # noqa: BLE001
            self.dm_msgs[asset_id] = None

    async def _set_state(self, asset_id, state, pending=True):
        self.states[asset_id] = state
        self.pending[asset_id] = pending
        await self._update_board()

    # -- public API ---------------------------------------------------------
    async def start(self, asset_id, initial_state, display, description):
        """Register one id: resolve it now (show a card in DMs), update the board,
        and spawn a watcher if it is still pending."""
        self.states[asset_id] = initial_state
        self.displays[asset_id] = display
        self.descriptions[asset_id] = description or ""
        if _is_final_state(initial_state):
            self.pending[asset_id] = False
            if (initial_state or "").strip().lower() == "approved":
                await self._dm_set(asset_id, _monitor_status_embed(
                    "Already accepted", PURPLE,
                    [("Asset", f"`{asset_id}` ({display})"),
                     ("Status", "showing its analysis as native Roblox audio...")]))
                await self._accept(asset_id)
            else:
                await self._dm_set(asset_id, _monitor_embed(
                    asset_id, display, "—", initial_state, description))
            await self._update_board()
            return
        self.pending[asset_id] = True
        await self._dm_set(asset_id, _monitor_status_embed(
            "Monitoring started", PURPLE,
            [("Asset", f"`{asset_id}` ({display})"),
             ("Status", f"currently **{initial_state}**"),
             ("Note", "full detail for this audio is kept here in your DMs.")]))
        await self._update_board()
        active_monitors[asset_id] = asyncio.create_task(self.watch(asset_id, initial_state))

    async def watch(self, asset_id, initial_state):
        task = asyncio.current_task()
        display = self.displays.get(asset_id, asset_id)
        last = initial_state
        try:
            deadline = time.monotonic() + MONITOR_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
                try:
                    st = await asyncio.to_thread(killa_tools.get_asset_moderation, asset_id)
                except Exception as e:  # noqa: BLE001
                    _log(f"monitor {asset_id}: read error -> {e}")
                    await self._dm_set(asset_id, _monitor_status_embed(
                        "Monitoring stopped", RED,
                        [("Asset", f"`{asset_id}` ({display})"),
                         ("Reason", f"{e}")]))
                    await self._set_state(asset_id, "stopped", pending=False)
                    return
                cur = st["state"]
                _log(f"monitor {asset_id}: {last} -> {cur}")
                if cur and cur != last:
                    if _is_final_state(cur):
                        await self._final(asset_id, st, last)
                        return
                    await self._dm_set(asset_id, _monitor_embed(
                        asset_id, st.get("display_name"), last, cur, st.get("description")))
                    await self._set_state(asset_id, cur, pending=True)
                    last = cur
            await self._dm_set(asset_id, _monitor_status_embed(
                "Monitoring timed out", PURPLE,
                [("Asset", f"`{asset_id}` ({display})"),
                 ("Detail", f"No final decision after {int(MONITOR_TIMEOUT_SECONDS)}s.")]))
            await self._set_state(asset_id, "stopped", pending=False)
        finally:
            if active_monitors.get(asset_id) is task:
                active_monitors.pop(asset_id, None)
                _log(f"monitor {asset_id}: ended (slot freed)")

    async def _final(self, asset_id, st, prev):
        cur = st["state"]
        display = st.get("display_name")
        _log(f"monitor {asset_id}: FINAL state {cur}")
        if (cur or "").strip().lower() == "approved":
            await self._dm_set(asset_id, _monitor_status_embed(
                "Accepted", PURPLE,
                [("Asset", f"`{asset_id}` ({display})"),
                 ("Status", "downloading the audio as native Roblox audio...")]))
            await self._accept(asset_id, st)
        else:
            await self._dm_set(asset_id, _monitor_embed(
                asset_id, display, prev, cur, st.get("description")))
        await self._set_state(asset_id, cur, pending=False)

    async def _accept(self, asset_id, st=None):
        """Download the accepted audio, analyze it, and post the full specs card
        (with the waveform) to the DMs -- the DM card is edited in place."""
        display = self.displays.get(asset_id) or (st or {}).get("display_name") or asset_id
        description = self.descriptions.get(asset_id) or (st or {}).get("description") or ""
        workdir = tempfile.mkdtemp(prefix="kb_monitor_")
        try:
            raw = await asyncio.to_thread(killa_tools.download_asset_audio, asset_id)
            input_path = os.path.join(workdir, "input_audio")  # ffmpeg sniffs content
            with open(input_path, "wb") as f:
                f.write(raw)
            pass1 = os.path.join(workdir, "pass1.ogg")
            ogg_path = os.path.join(workdir, "audio.ogg")
            wf = os.path.join(workdir, "waveform.png")
            res = await asyncio.to_thread(
                killa_tools.roblox_simulate, input_path, pass1, ogg_path, wf)
            # roblox_simulate returns (info_dict, waveform_path).
            info_dict = res[0] if isinstance(res, tuple) else res
            waveform_ok = os.path.exists(wf)
            title = killa_tools._safe_name(display or f"audio_{asset_id}", "ogg")
            files = [discord.File(ogg_path, filename=title)]
            if waveform_ok:
                files.append(discord.File(wf, filename="waveform.png"))
            embed = _monitor_accepted_embed(
                asset_id, display, description, info_dict,
                image="waveform.png" if waveform_ok else None)
            await self._dm_set(asset_id, embed, files=files)
        except Exception as e:  # noqa: BLE001
            await self._dm_set(asset_id, _monitor_status_embed(
                "Audio accepted, but download failed", RED,
                [("Asset", f"`{asset_id}` ({display})"),
                 ("Error", f"{e}")]))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def _split_ids(text):
    """Split a mixed id string on commas, semicolons and whitespace.

    Backticks are stripped so copy-pasted ids like ``123, 456`` work too.
    """
    return [t.strip("`") for t in text.replace(",", " ").replace(";", " ").split()]


@bot.tree.command(
    name="monitor",
    description="Monitor Roblox asset moderation states; changes are posted here.",
)
@app_commands.describe(
    asset="One or more audio asset ids (space/comma separated), 'list' to show "
          "watchers, or 'stop <id...>' to stop one or more.",
)
async def monitor_slash(interaction: discord.Interaction, asset: str):
    if not await _require(interaction):
        return
    if not killa_tools.ROBLOX_COOKIE:
        await interaction.response.send_message(
            "no Roblox cookie detected (set ROBLOX_COOKIE in .env)", ephemeral=True)
        return

    token = (asset or "").strip().lower()

    # Control sub-commands so you can run several watchers and manage them.
    if token in ("list", "list_"):
        if not active_monitors:
            await interaction.response.send_message(
                "no assets are being monitored right now.", ephemeral=True)
            return
        lines = [f"`{aid}` ({_watcher_display.get(aid, 'asset')})" for aid in sorted(active_monitors)]
        await interaction.response.send_message(
            "**Monitoring right now:**\n" + "\n".join(lines), ephemeral=True)
        return
    if token.startswith("stop "):
        targets = _split_ids((asset or "").strip()[5:])
        if not targets:
            await interaction.response.send_message(
                "pass one or more ids, e.g. `stop 123 456`.", ephemeral=True)
            return
        stopped, missing = [], []
        for t in targets:
            task = active_monitors.get(t)
            if task:
                task.cancel()
                active_monitors.pop(t, None)
                _watcher_display.pop(t, None)
                stopped.append(f"`{t}`")
            else:
                missing.append(f"`{t}`")
        msg = []
        if stopped:
            msg.append("stopped monitoring " + ", ".join(stopped) + ".")
        if missing:
            msg.append("not monitoring " + ", ".join(missing) + ".")
        await interaction.response.send_message(
            " ".join(msg) or "no targets.", ephemeral=True)
        return

    # Subscribe: comma/space/semicolon separated ids in one call, e.g.
    # `/monitor 123 456` or `/monitor 123,456,789`.
    ids, bad = [], []
    for t in _split_ids(asset or ""):
        try:
            ids.append(str(verify_audio.extract_asset_id(t)))
        except verify_audio.AssetError:
            bad.append(f"`{t}`")
    if not ids:
        if bad:
            await interaction.response.send_message(
                "couldn't read asset ids from: " + ", ".join(bad), ephemeral=True)
        else:
            await interaction.response.send_message(
                "pass one or more audio asset ids, e.g. `/monitor 123 456` "
                "or `/monitor 123,456`.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    channel = getattr(interaction, "channel", None)
    user = interaction.user
    session = _MonitorSession(user, channel)

    started, skipped, decided, failed = [], [], [], list(bad)
    for asset_id in ids:
        if asset_id in active_monitors:
            skipped.append(f"`{asset_id}`")
            continue
        try:
            st = await asyncio.to_thread(killa_tools.get_asset_moderation, asset_id)
        except Exception as e:  # noqa: BLE001
            failed.append(f"`{asset_id}` ({e})")
            continue
        initial_state = st["state"]
        display = st.get("display_name")
        description = st.get("description")
        if _is_final_state(initial_state):
            # Already decided: resolve it now (the detailed card goes to the DMs
            # and the counts update the board) but DON'T spawn a watcher -- there
            # is nothing left to watch.
            await session.start(asset_id, initial_state, display, description)
            decided.append(f"`{asset_id}` (**{initial_state}**)")
            continue
        _watcher_display[asset_id] = st["display_name"]
        await session.start(asset_id, initial_state, st["display_name"], st.get("description") or "")
        started.append(f"`{asset_id}` ({st['display_name']})")

    summary = []
    if started:
        summary.append(" Watching " + ", ".join(started) + ".")
    if skipped:
        summary.append("Already watching (skipped): " + ", ".join(skipped) + ".")
    if decided:
        summary.append("Already decided: " + ", ".join(decided) + ".")
    if failed:
        summary.append("Not started: " + ", ".join(failed) + ".")
    await interaction.followup.send(
        "\n".join(summary) if summary else "nothing started.", ephemeral=True)


@bot.tree.command(
    name="ping",
    description="Check that KILLA'S BOSS is online and ready.",
)
async def ping_slash(interaction: discord.Interaction):
    if not await _require_admin(interaction):
        return
    await interaction.response.defer(thinking=True)
    embed = discord.Embed(
        title="KILLA'S BOSS is online",
        description="Killa's audio verification bot is awake and ready to check your sounds.",
        color=PURPLE,
    )
    embed.add_field(name="Status", value="Active", inline=True)
    embed.add_field(name="Operated by", value="Killa", inline=True)
    embed.add_field(name="Service", value="Roblox audio verification", inline=True)
    embed.add_field(
        name="Discord",
        value="[Join the server](https://discord.gg/AWFAFSCbXQ)",
        inline=False,
    )
    embed.add_field(
        name="Roblox Group",
        value="[KILLA'S HUB 3.0](https://www.roblox.com/communities/101224973/KILLAS-HUB-3-0#!/about)",
        inline=False,
    )
    try:
        if bot.user:
            embed.set_thumbnail(url=bot.user.display_avatar.url)
    except Exception:
        pass
    _brand_footer(embed, "City Of Boombox audio tool")
    await interaction.followup.send(embed=embed)


def _audio_embed(title, filename, info, note=None, image="waveform.png", tag="audio analysis"):
    """Build a clean embed for a processed audio file (spec fields + image).

    `image` is the filename of the attached image to show inline (e.g. the
    waveform or the spectrogram); `tag` sets the branded footer label.
    """
    sr = info.get("sample_rate")
    chn = info.get("channels")
    codec = info.get("codec")
    br = info.get("bitrate_kbps")
    lufs = info.get("lufs")
    peak = info.get("peak")
    embed = discord.Embed(title=title, description=filename, color=PURPLE)
    if note:
        embed.add_field(name="Notes", value=note, inline=False)
    embed.add_field(name="Duration", value=info.get("duration", "—"), inline=True)
    embed.add_field(name="Codec", value=(codec or "—"), inline=True)
    embed.add_field(name="Channels", value=(str(chn) if chn else "—"), inline=True)
    embed.add_field(name="Sample Rate", value=f"{sr} Hz" if sr else "—", inline=True)
    embed.add_field(name="Bitrate", value=f"{br} kbps" if br else "—", inline=True)
    embed.add_field(name="Integrated Loudness", value=f"{lufs:.1f} LUFS" if lufs is not None else "—", inline=True)
    embed.add_field(name="Peak", value=f"{peak:.1f} dBFS" if peak is not None else "—", inline=True)
    embed.set_image(url=f"attachment://{image}")
    _brand_footer(embed, tag)
    return embed


@bot.tree.command(
    name="analyze",
    description="Analyze an audio file: loudness, waveform, format info.",
)
@app_commands.describe(file="The audio file to analyze.")
async def analyze_slash(interaction: discord.Interaction, file: discord.Attachment):
    if not await _require(interaction):
        return
    await interaction.response.defer(thinking=True)
    workdir = tempfile.mkdtemp(prefix="kb_analyze_")
    try:
        input_path = await _save_attachment(workdir, file)
        info = await asyncio.to_thread(killa_tools.analyze_audio, input_path)
        wf = os.path.join(workdir, "waveform.png")
        await asyncio.to_thread(killa_tools.render_waveform, input_path, wf)
        try:
            await interaction.followup.send(
                embed=_audio_embed("Audio Analysis", file.filename, info),
                files=[discord.File(wf, filename="waveform.png"),
                       discord.File(input_path, filename=file.filename)])
        except discord.HTTPException:
            # The original audio can exceed Discord's upload limit (e.g. a long
            # track); still post the analysis embed so the data isn't lost.
            await interaction.followup.send(
                embed=_audio_embed("Audio Analysis", file.filename, info),
                files=[discord.File(wf, filename="waveform.png")])
            await interaction.followup.send(
                "the original file was too large to attach, so only the analysis is shown.",
                ephemeral=True)
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(f"analyze failed: {e}", ephemeral=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@bot.tree.command(
    name="cr",
    description="Apply an audio preset (key/speed change) or auto-fit to an attached file.",
)
@app_commands.describe(
    file="The audio file to process.",
    preset="The processing preset to apply, or Auto-Fit to find a clean shift.",
)
@app_commands.choices(
    preset=[
        app_commands.Choice(name="Subtle", value="subtle"),
        app_commands.Choice(name="Balanced", value="balanced"),
        app_commands.Choice(name="Stealth", value="stealth"),
        app_commands.Choice(name="Aggressive", value="aggressive"),
        app_commands.Choice(name="Auto-Fit (closest)", value="autofit"),
    ],
)
async def cr_slash(interaction: discord.Interaction,
                     file: discord.Attachment, preset: str):
    # These are module-level; writing them inside the function would otherwise
    # make Python treat them as local and raise UnboundLocalError on the reads
    # below (the auto-fit gate).
    global _cr_autofit_running, _cr_cooldown_until
    if not await _require(interaction):
        return
    is_autofit = preset == "autofit"

    # Auto-fit is throttled: only one runs at a time (it contacts Shazam, which
    # rate-limits by device id), and each user is cooled down between runs.
    if is_autofit:
        now = time.time()
        if _cr_autofit_running:
            _log(f"cr-autofit BLOCKED (already running) for {interaction.user} "
                 f"(id {interaction.user.id})")
            await interaction.response.send_message(
                "The auto-fit is already testing a file right now. Wait for it "
                "to finish, then run this again.", ephemeral=True)
            return
        until = _cr_cooldown_until.get(interaction.user.id, 0)
        if now < until:
            remain = int(until - now)
            _log(f"cr-autofit BLOCKED (cooldown {remain}s) for {interaction.user} "
                 f"(id {interaction.user.id})")
            await interaction.response.send_message(
                f"This command is on cooldown. Try again in **{remain} second"
                f"{'' if remain == 1 else 's'}**.", ephemeral=True)
            return
        _cr_autofit_running = True
    elif preset not in killa_tools.CR_PRESETS:
        await interaction.response.send_message(
            f"unknown preset `{preset}`. pick one of: "
            + ", ".join(killa_tools.CR_PRESETS) + ", or autofit", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    workdir = tempfile.mkdtemp(prefix="kb_cr_")
    try:
        input_path = await _save_attachment(workdir, file)
        output_path = os.path.join(workdir, "output.mp3")
        spec_path = os.path.join(workdir, "spectrogram.png")
        # Export the shifted copy under the source's own name + "_cr" so the
        # copyright-safe version is identifiable (e.g. "song.mp3" -> "song_cr.mp3").
        out_stem = os.path.splitext(file.filename or "audio")[0] or "audio"
        cr_name = f"{out_stem}_cr.mp3"

        if is_autofit:
            progress_lines = []

            def _progress(msg):
                progress_lines.append(msg)
                _log(f"cr-autofit progress: {msg}")

            # Surface each step in the deferred message so the user sees which
            # shift is being tried ("getting the best it can get to").
            msg = await interaction.original_response()
            updater = asyncio.create_task(_update_cr_progress(msg, progress_lines))
            try:
                result = await asyncio.to_thread(
                    killa_tools.cr_autofit, input_path, output_path, spec_path, _progress)
            finally:
                updater.cancel()
            # Only cool the user down once the run actually reached Shazam. A run
            # that failed immediately (e.g. a missing engine dep) doesn't lock them out.
            _cr_cooldown_until[interaction.user.id] = time.time() + CR_COOLDOWN_SECONDS

            verdict = (
                "the processed file wasn't identifiable at any probe point."
                if result["clean"] else
                "the strongest shift still matched part of the file, so it was "
                "produced as a best effort (not guaranteed)."
            )
            embed = discord.Embed(
                title="Auto-Fit Complete",
                description=f"Processed **{file.filename}** so a matcher can't identify it.\n"
                            f"Exported as **{cr_name}**.",
                color=PURPLE,
            )
            info = result.get("info") or {}
            embed.add_field(name="Pitch", value=f"{result['pitch']:+.1f} st", inline=True)
            embed.add_field(name="Tempo", value=f"{result['tempo']:.2f}x", inline=True)
            if info.get("duration"):
                embed.add_field(name="Duration", value=info.get("duration", "—"), inline=True)
            if info.get("codec"):
                embed.add_field(name="Codec", value=info.get("codec", "—"), inline=True)
            if info.get("sample_rate"):
                embed.add_field(name="Sample Rate", value=f"{info['sample_rate']} Hz", inline=True)
            embed.add_field(name="Result", value=verdict, inline=False)
            embed.set_image(url="attachment://spectrogram.png")
            _brand_footer(embed, "auto-fit")
            _log(f"cr-autofit DONE for {interaction.user} (id {interaction.user.id}): "
                 f"pitch {result['pitch']:+.1f} st, tempo {result['tempo']:.2f}x, "
                 f"clean={result['clean']}")
            await interaction.followup.send(
                embed=embed,
                files=[discord.File(output_path, filename=cr_name),
                       discord.File(spec_path, filename="spectrogram.png")])
        else:
            info = await asyncio.to_thread(killa_tools.cr_process, input_path, output_path,
                                           spec_path, preset)
            semi, tempo = killa_tools.CR_PRESETS[preset]
            embed = _audio_embed(
                f"Preset Applied — {preset}",
                cr_name,
                info,
                note=f"Shifted by {semi:+.1f} st, tempo {tempo:.2f}x ({preset} preset).",
                image="spectrogram.png",
                tag="cr preset",
            )
            await interaction.followup.send(
                embed=embed,
                files=[discord.File(output_path, filename=cr_name),
                       discord.File(spec_path, filename="spectrogram.png")])
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(f"cr failed: {e}", ephemeral=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if is_autofit:
            _cr_autofit_running = False


@bot.tree.command(
    name="shazam",
    description="Identify the song in an attached audio file via Shazam.",
)
@app_commands.describe(file="The audio file to identify.")
async def shazam_slash(interaction: discord.Interaction, file: discord.Attachment):
    if not await _require(interaction):
        return
    await interaction.response.defer(thinking=True)
    workdir = tempfile.mkdtemp(prefix="kb_shazam_")
    try:
        input_path = await _save_attachment(workdir, file)
        result = await asyncio.to_thread(killa_tools.shazam_recognize, input_path)
        if not result.get("matched"):
            embed = discord.Embed(
                title="Shazam couldn't identify that",
                description=(
                    f"**{file.filename}** wasn't recognised by Shazam. "
                    "Try a longer or clearer clip of the song."
                ),
                color=RED,
            )
            _brand_footer(embed, "shazam")
            await interaction.followup.send(embed=embed)
            return

        title = result.get("title") or "Recognised Track"
        artist = result.get("artist")
        album = result.get("album")
        released = result.get("released")
        label = result.get("label")
        genres = result.get("genres")
        embed = discord.Embed(
            title=title,
            description=f"**{file.filename}** was identified.",
            color=PURPLE,
        )
        if artist:
            embed.add_field(name="Artist", value=artist, inline=True)
        if album:
            embed.add_field(name="Album", value=album, inline=True)
        if genres:
            embed.add_field(name="Genre", value=genres, inline=True)
        if released:
            embed.add_field(name="Released", value=released, inline=True)
        if label:
            embed.add_field(name="Label", value=label, inline=True)
        url = result.get("url")
        if url:
            embed.add_field(name="Shazam", value=f"[View on Shazam]({url})", inline=False)
        cover = result.get("coverart")
        if cover:
            embed.set_thumbnail(url=cover)
        if title and title != "Recognised Track":
            # Shazam sometimes returns a hit that isn't a real published release,
            # so give the user a way to hunt the name down themselves.
            links = killa_tools.search_links(title, artist)
            if links:
                embed.add_field(
                    name="Search",
                    value="\n".join(f"[{k}]({v})" for k, v in links.items()),
                    inline=False,
                )
        _brand_footer(embed, "shazam")
        _log(f"shazam DONE for {interaction.user} (id {interaction.user.id}): "
             f"{title!r} by {artist!r}")
        await interaction.followup.send(embed=embed)
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(f"shazam failed: {e}", ephemeral=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@bot.tree.command(
    name="roblox",
    description="Simulate Roblox audio compression (two-pass ogg) to test your file.",
)
@app_commands.describe(
    file="The audio file to compress.",
    quality="libvorbis quality (-1..10, default 0.5).",
)
async def roblox_slash(interaction: discord.Interaction, file: discord.Attachment,
                       quality: float = None):
    if not await _require(interaction):
        return
    quality = 0.5 if quality is None else quality
    await interaction.response.defer(thinking=True)
    workdir = tempfile.mkdtemp(prefix="kb_roblox_")
    try:
        input_path = await _save_attachment(workdir, file)
        pass1 = os.path.join(workdir, "pass1.ogg")
        pass2 = os.path.join(workdir, "pass2.ogg")
        wf = os.path.join(workdir, "waveform.png")
        info, wf = await asyncio.to_thread(killa_tools.roblox_simulate,
                                           input_path, pass1, pass2, wf, quality)
        stem = os.path.splitext(file.filename or "audio")[0] or "audio"
        note = f"Two-pass libvorbis re-encode at quality {quality}."
        try:
            await interaction.followup.send(
                embed=_audio_embed("Roblox Compression Test", f"{stem}.ogg", info, note=note),
                files=[discord.File(wf, filename="waveform.png"),
                       discord.File(pass2, filename=f"{stem}.ogg")])
        except discord.HTTPException:
            # The re-encoded ogg can exceed Discord's upload limit; still post the
            # analysis embed so the compression result isn't lost.
            await interaction.followup.send(
                embed=_audio_embed("Roblox Compression Test", f"{stem}.ogg", info, note=note),
                files=[discord.File(wf, filename="waveform.png")])
            await interaction.followup.send(
                "the compressed file was too large to attach, so only the analysis is shown.",
                ephemeral=True)
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(f"roblox processing failed: {e}", ephemeral=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _sync_all_guilds():
    """Sync the single GLOBAL command set, then delete any stale guild-scoped copies.

    The commands are now registered globally only (a global command appears once
    in every server the bot is in). Older configs also wrote a guild-scoped copy
    of each command, which made the server show every command twice. This deletes
    those leftover guild-scoped registrations so there is exactly one clean set.
    """
    total = 0
    try:
        synced = await bot.tree.sync()   # global scope (guilds + DMs + private)
        total += len(synced)
        print(f"Global command sync done ({len(synced)} commands).")
    except Exception as e:  # noqa: BLE001
        print(f"global sync failed: {e}")
    total += await _remove_guild_command_duplicates()
    return total


async def _remove_guild_command_duplicates():
    """Purge any guild-scoped command registrations for the bot's guilds.

    Idempotent: clearing a guild's command set and re-syncing it deletes whatever
    guild-scoped commands Discord still holds for that server. The global set is
    unaffected, so the command stays available in the server -- just once.
    """
    removed = 0
    targets = set()
    if GUILD_ID and GUILD_ID.isdigit():
        targets.add(int(GUILD_ID))
    for guild in list(bot.guilds):
        targets.add(guild.id)
    for gid in targets:
        try:
            bot.tree.clear_commands(guild=discord.Object(id=gid))
            synced = await bot.tree.sync(guild=discord.Object(id=gid))
            removed += len(synced)
            if synced:
                print(f"purged {len(synced)} duplicate guild command(s) for {gid}.")
        except Exception as e:  # noqa: BLE001
            print(f"guild purge failed for {gid}: {e}")
    return removed


def _restart_bot():
    """Replace the running process so edits to the .py files are loaded.

    Discord's slash commands are only reflected after a sync, and a sync can't
    pick up file changes on an already-running process -- so this restart, which
    re-runs setup_hook and re-syncs, is what actually makes edits live. execv
    swaps the image in place; if the platform won't let us, spawn a detached
    replacement and exit so the new process takes over.
    """
    script = os.path.abspath(sys.argv[0])
    try:
        os.execv(sys.executable, [sys.executable, script])
    except (OSError, AttributeError):
        subprocess.Popen([sys.executable, script], cwd=os.path.dirname(script))
        os._exit(0)


@bot.tree.command(
    name="download",
    description="Download a YouTube / web video or audio track via yt-dlp (personal use).",
)
@app_commands.describe(
    url="The video or audio link to download (YouTube, etc.).",
    kind="audio (mp3, default) or video (mp4, capped at 720p).",
)
@app_commands.choices(
    kind=[
        app_commands.Choice(name="Audio (mp3)", value="audio"),
        app_commands.Choice(name="Video (mp4)", value="video"),
    ],
)
async def download_slash(interaction: discord.Interaction, url: str,
                         kind: str = "audio"):
    if not await _require(interaction):
        return
    await interaction.response.defer(thinking=True)
    workdir = tempfile.mkdtemp(prefix="kb_dl_")
    try:
        info = await asyncio.to_thread(killa_tools.download_media, url, workdir, kind)
        title = info["title"]
        if len(title) > 100:
            title = title[:97] + "..."
        embed = discord.Embed(title="Downloaded", description=title, color=PURPLE)
        embed.add_field(name="Source", value=(info.get("webpage_url") or "—"), inline=False)
        embed.add_field(name="Duration", value=info.get("duration", "—"), inline=True)
        embed.add_field(name="Uploader", value=info.get("uploader", "—"), inline=True)
        embed.add_field(name="Format", value=info.get("kind", kind), inline=True)
        embed.add_field(name="Size", value=f"{info['size'] / 1e6:.1f} MB", inline=True)
        if info.get("note"):
            embed.add_field(name="Note", value=info["note"], inline=False)
        _brand_footer(embed, "download tool")
        path = info["path"]
        filename = os.path.basename(path)
        try:
            await interaction.followup.send(
                embed=embed,
                files=[discord.File(path, filename=filename)])
        except discord.HTTPException:
            # Extremely large outputs can still exceed Discord's limit even after
            # re-encoding; keep the metadata embed so the result isn't lost.
            await interaction.followup.send(
                embed=embed,
                content="(the file was too large to attach, so only the info is shown.)",
                ephemeral=True)
        _log(f"download DONE by {interaction.user} (id {interaction.user.id}) "
             f"kind={kind} url={url} -> {filename}")
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(f"download failed: {e}", ephemeral=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@bot.tree.command(name="reload", description="Re-sync the slash command tree (owner only).")
async def reload_slash(interaction: discord.Interaction):
    if not await _require_owner(interaction):
        return
    await interaction.response.defer(thinking=True)
    # Push the current command tree globally and purge any leftover guild-scoped
    # duplicates, then restart. The old code synced to every guild AND globally,
    # which made the server show every command twice (global + guild copy); the
    # global set alone is enough and stays clean.
    try:
        synced = await _sync_all_guilds()
        names = ", ".join(c.name for c in bot.tree.get_commands())
        _log(f"reload by {interaction.user} (id {interaction.user.id}): synced "
             f"{synced} command(s) -> {names}")
        await interaction.followup.send(
            f"Re-synced {synced} command(s) and restarting to apply code edits.\n"
            f"Commands: {names}",
            ephemeral=True)
    except Exception as e:  # noqa: BLE001
        await interaction.followup.send(f"reload failed: {e}", ephemeral=True)
        return
    # Give the reply a moment to deliver before swapping the process.
    await asyncio.sleep(1.0)
    _restart_bot()


@bot.tree.command(
    name="whitelist",
    description="Manage the extra staff allow-list (owner only).",
)
@app_commands.describe(
    action="add, remove, or list.",
    user="The Discord user to add/remove (use action=list to view the list).",
)
async def whitelist_slash(interaction: discord.Interaction,
                          action: str, user: discord.User = None):
    if not await _require_owner(interaction):
        return
    # Ack the interaction up front so the token can't expire before the reply.
    # Some invocations 404 "Unknown interaction" (10062) when the first response
    # lands late or the interaction is touching a file on a slow disk; deferring
    # first and replying via followup (same as the other commands) avoids it.
    # The response is public so the add/remove confirmations are visible to the
    # whole channel (only the "list" view below stays owner-only/ephemeral).
    await interaction.response.defer(thinking=True, ephemeral=False)
    action = (action or "").strip().lower()
    allow = _read_staff_whitelist()

    # /whitelist list — management view, only the requester sees it.
    if action == "list":
        entries = []
        for uid in sorted(allow):
            name = None
            try:
                u = await interaction.client.fetch_user(uid)
                name = u.display_name
            except Exception:
                name = None
            label = f"**{name}** — `{uid}`" if name else f"`{uid}`"
            entries.append(label)
        embed = discord.Embed(
            title="Staff Allow-List",
            description=(
            f"{len(allow)} user(s) verified and able to use this bot as an app."
        ) if allow else "No users verified yet.",
            color=PURPLE,
        )
        if entries:
            embed.add_field(name="Whitelisted", value="\n".join(entries),
                            inline=False)
        _advertise_fields(embed)
        _brand_footer(embed, "staff whitelist")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    if user is None:
        # add/remove needs a target; give a clear prompt instead of guessing.
        embed = discord.Embed(
            title="Pick a user",
            description=(
                "Pass the Discord `user` to `add` or `remove`, e.g. "
                "`/whitelist add @user`. Use `/whitelist list` to see who's "
                "whitelisted."
            ),
            color=PURPLE,
        )
        _brand_footer(embed, "staff whitelist")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    if action == "add":
        if user.id in allow:
            embed = discord.Embed(
                title="Already Whitelisted",
                description=f"{user.mention} is already on the staff allow-list.",
                color=PURPLE,
            )
            _advertise_fields(embed)
            _brand_footer(embed, "staff whitelist")
            await interaction.followup.send(embed=embed, ephemeral=False)
            return
        allow.add(user.id)
        _write_staff_whitelist(allow)
        embed = discord.Embed(
            title="User Whitelisted",
            description=(
                f"{user.mention} has been verified and can now use **KILLA'S BOSS** "
                "as an app on their user account."
            ),
            color=GREEN,
        )
        embed.add_field(name="Status", value="Verified", inline=True)
        embed.add_field(name="Added by",
                        value=interaction.user.display_name, inline=True)
        embed.add_field(
            name="Use it as an app",
            value=(
                f"{user.mention} can now run this bot as an app on their user "
                "account — add it under Discord → User Settings → Apps, then use "
                "the slash commands in a DM with the bot or in their own server."
            ),
            inline=False,
        )
        _advertise_fields(embed)
        _brand_footer(embed, "staff whitelist")
        await interaction.followup.send(embed=embed, ephemeral=False)
        # Tell the whitelisted user directly, and report whether it landed so the
        # person (and anyone watching) can see they've been whitelisted even when
        # their DMs are closed.
        try:
            await user.send(embed=_whitelist_dm_embed())
            dm_ok = True
        except discord.Forbidden:
            dm_ok = False
        except Exception:
            dm_ok = False
        note = discord.Embed(
            title="Notification sent",
            description=(
                f"{user.mention} has been DM'd that they're whitelisted."
                if dm_ok else
                f"{user.mention} couldn't be DM'd (private messages are likely "
                "closed) — they were shown in the confirmation above instead."
            ),
            color=GREEN if dm_ok else PURPLE,
        )
        _brand_footer(note, "staff whitelist")
        await interaction.followup.send(embed=note, ephemeral=False)
        return

    if action == "remove":
        if user.id not in allow:
            embed = discord.Embed(
                title="Not Whitelisted",
                description=f"{user.mention} is not on the staff allow-list.",
                color=RED,
            )
            _advertise_fields(embed)
            _brand_footer(embed, "staff whitelist")
            await interaction.followup.send(embed=embed, ephemeral=False)
            return
        allow.discard(user.id)
        _write_staff_whitelist(allow)
        embed = discord.Embed(
            title="User Removed From Whitelist",
            description=(
                f"{user.mention} has been removed from the verified list."
            ),
            color=RED,
        )
        embed.add_field(name="Status", value="Access revoked", inline=True)
        embed.add_field(name="Removed by",
                        value=interaction.user.display_name, inline=True)
        _advertise_fields(embed)
        _brand_footer(embed, "staff whitelist")
        await interaction.followup.send(embed=embed, ephemeral=False)
        return

    # Unknown action.
    embed = discord.Embed(
        title="Add, remove, or list",
        description=(
            "Pass `action` as `add`, `remove`, or `list` "
            "(e.g. `/whitelist add @user` or `/whitelist list`)."
        ),
        color=PURPLE,
    )
    _brand_footer(embed, "staff whitelist")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="groups",
    description="Show the Roblox groups this bot is verified to grant audio for.",
)
async def groups_slash(interaction: discord.Interaction):
    # Only the owner, or a Head Mod / Mod of this server, may see the group list.
    if not is_authorized(interaction):
        await _deny_staff(interaction)
        return
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        embed = await asyncio.to_thread(_build_groups_embed)
    except Exception as e:  # noqa: BLE001
        embed = discord.Embed(
            title="Couldn't load the group list", color=RED,
            description=f"Something went wrong: `{e}`")
    await interaction.followup.send(embed=embed, ephemeral=False)


@bot.event
async def on_interaction(interaction):
    """Log every slash-command use (who, when, and the options) to the console
    and to usage.log, so the command menu is traceable when you run the bot."""
    global _cmd_count
    cmd = interaction.command
    if cmd is None:
        return  # component / modal interactions aren't commands
    _cmd_count += 1
    opts = []
    for o in (interaction.data or {}).get("options", []) or []:
        if isinstance(o, dict) and o.get("name"):
            v = o.get("value")
            if isinstance(v, (list, dict)):
                v = json.dumps(v, default=str)
            opts.append(f"{o['name']}={v}")
    opt_txt = (" " + " ".join(opts)) if opts else ""
    _log(f"user {interaction.user} (id {interaction.user.id}) used /{cmd.name}{opt_txt}")


def _cleanup_stale_workdirs(max_age=24 * 3600):
    """Remove orphaned bot temp work dirs (kb_*) older than ``max_age`` seconds.

    Every work dir is removed in a ``finally``, so these should only linger if
    the process was hard-killed (crash / PC restart) mid-operation. Sweeping the
    old ones at startup keeps the temp folder from slowly filling up.
    """
    tmp = tempfile.gettempdir()
    now = time.time()
    try:
        for name in os.listdir(tmp):
            if not name.startswith("kb_"):
                continue
            d = os.path.join(tmp, name)
            try:
                if now - os.path.getmtime(d) > max_age:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        pass


@bot.event
async def on_ready():
    global APP_OWNER_ID
    _cleanup_stale_workdirs()
    try:
        APP_OWNER_ID = bot.application.owner.id
    except Exception:
        APP_OWNER_ID = None
    print(f"Audio bot ready: {bot.user} (id {bot.user.id}), app owner {APP_OWNER_ID}")
    _log("SESSION STARTED")
    # Register commands to every guild (instant) + the global scope. Awaited (not
    # a background task) so the startup is guaranteed to finish syncing before
    # returning -- otherwise a newly-added command can sit unsynced if the task
    # is interrupted, and /reload would look like it "didn't update".
    await _sync_all_guilds()


if __name__ == "__main__":
    _log(f"KILLA'S BOSS starting; {len(bot.tree.get_commands())} commands registered: "
         f"{', '.join(c.name for c in bot.tree.get_commands())}")
    try:
        bot.run(TOKEN)
    except discord.PrivilegedIntentsRequired:
        # Watching the owner's status (presence mirroring) is on, but the two
        # privileged intents aren't enabled in the Developer Portal, so Discord
        # refuses the login. The bot can't run as-is, but we keep this window OPEN
        # (and readable) instead of letting it vanish.
        print("\nDiscord refused the login: MIRROR_PRESENCE needs the privileged "
              "gateway intents enabled in the Developer Portal.\n"
              "  Bot -> Privileged Gateway Intents -> turn ON 'Presence Intent' and "
              "'Server Members Intent', then restart.\n"
              "  Or set MIRROR_PRESENCE=0 in .env to run WITHOUT presence mirroring "
              "right now.\n", flush=True)
        try:
            input("\nPress Enter to close this window after you've read the above...")
        except Exception:  # noqa: BLE001 - no tty (e.g. run detached); don't hang
            pass
    finally:
        # bot.run returns when the process is shutting down (Ctrl+C / close), which
        # is when the single SESSION ENDED summary is written. This beats on_disconnect
        # for the "you ended the session" case because a transient gateway drop or a
        # background reconnect doesn't end the process -- it would otherwise write a
        # misleading SESSION ENDED mid-run. If the process is killed outright, no
        # line is written (there is no clean exit to hook).
        _log_session_end()
