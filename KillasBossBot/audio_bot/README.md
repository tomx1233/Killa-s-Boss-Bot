# KILLA'S BOSS — Discord Audio Bot

A standalone Discord bot that verifies **Roblox audio** assets, grants your game
permission to use group-owned audio, and gives staff a set of audio-analysis /
copyright-avoidance tools. Built for the workflow where you want to sanity-check
an `rbxassetid://` audio id before wiring it into an in-game audio player.

> **License:** GPL-3.0. This bot is a fork of
> [typicaalusername/scope](https://github.com/typicaalusername/scope) — the
> attribution is retained in the source. If you distribute changes, keep it
> GPL-3.0 and credit the original author.

---

## Folder layout

```
KillasBossBot/
├── audio_bot/            <- the Discord bot (run it from here)
│   ├── audio_bot.py      <- the Discord client and all slash commands
│   ├── grant_audio.py    <- engine: grant a group-owned audio to your game
│   ├── verify_audio.py   <- engine: build the verify verdict
│   ├── killa_tools.py    <- engine: /analyze /cr /roblox /shazam /download /monitor /ping
│   ├── run_audio_bot.bat <- Windows launcher
│   ├── .env.example      <- copy to `.env` and fill in
│   ├── requirements.txt  <- pip dependencies
│   ├── TOS.md / PRIVACY.md
│   └── README.md         <- this file
├── shazam_proof.py       <- SongRec engine source (used by /shazam + /cr Auto-Fit)
├── songrec_tester.py     <- SongRec Shazam client
└── fingerprint_oracle.py <- fingerprint helper for the above
```

`killa_tools.py` loads `shazam_proof.py`, `songrec_tester.py` and
`fingerprint_oracle.py` from the folder **one level above `audio_bot/`** (the
`WORKSPACE_ROOT` constant). In this copy they are already there. If you reorganize
the folders, update that constant in `audio_bot/killa_tools.py`.

---

## Getting started (setup)

### 1. Install Python and ffmpeg

- Install **Python 3.10+** from [python.org](https://www.python.org/downloads/).
  On installation check **"Add Python to PATH"**.
- Install **ffmpeg** and put it on PATH (needed for `/analyze`, `/cr`, `/roblox`,
  `/shazam` and the `/cr` spectrogram). On Windows:
  ```
  winget install Gyan.FFmpeg
  ```
  Then close and reopen your terminal so the new PATH is picked up.
- For `/download` you also need the **yt-dlp CLI**:
  ```
  winget install yt-dlp.yt-dlp
  ```

### 2. Create your own Discord bot application

1. Go to the [Discord developer portal](https://discord.com/developers/applications)
   → **New Application** → **Bot**.
2. Click **Reset Token** and copy the token (this is `DISCORD_TOKEN`).
3. (Optional) Enable the two **Privileged Gateway Intents** — **Presence Intent**
   and **Server Members Intent** — if you want `MIRROR_PRESENCE` to work.

### 3. Install the Python dependencies

From the `audio_bot` folder:

```
cd KillasBossBot\audio_bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

That installs the core bot deps plus the optional Shazam/SongRec engine. If you
only want the basic commands and don't need `/shazam` / `/cr Auto-Fit`, you can
install just the core block:
```
pip install discord.py python-dotenv numpy pillow
```
The bot will still run — those two commands reply with a "missing dependency"
message.

### 4. Create your `.env` file

Copy the template and fill in your values:

```
cd KillasBossBot\audio_bot
copy .env.example .env
```

Edit `.env`:
- `DISCORD_TOKEN` — your bot token from step 2.
- `GUILD_ID` — your server id (enable Developer Mode, right-click the server →
  Copy Server ID). With this set, `/verify` appears within seconds of the bot
  going online.
- `ROBLOX_COOKIE` — the `.ROBLOSECURITY=...` cookie of the account that owns the
  group audios you want to grant. Log into Roblox in a browser, open DevTools →
  Application → Cookies, copy the value of the `ROBLOSECURITY` cookie for
  `.roblox.com`.

> ⚠️ **`ROBLOX_COOKIE` grants full access to that Roblox account.** It is a
> secret. Never commit it, never paste it into a public chat, and rotate it if it
> ever leaks. The real cookie is not included in this handoff copy — you supply
> your own.

### 5. Run the bot

```
cd KillasBossBot\audio_bot
run_audio_bot.bat
```

...or by hand (from the repo folder):
```
python audio_bot.py
```

### 6. Invite the bot to your server

In the Developer Portal → OAuth2 → URL Generator:
- Scopes: **bot**, **applications.commands**
- Bot Permissions: whatever you need (Slash Commands, Send Messages, Attach
  Files, Read Message History at minimum)

Use the generated URL to invite the bot. Because `GUILD_ID` is set, its slash
commands register immediately; otherwise they can take up to an hour.

---

## What it does

`/verify <asset id>` (also accepts an `rbxassetid://` url or a `roblox.com`
asset link) checks the audio and, if it is **group-owned** audio from a supported
group, **grants** your game permission to use it on the spot (green "verified &
granted"). A free user-owned audio just confirms it is usable. A red embed gives
the concrete reason otherwise.

Failure reasons it can report: not a valid asset id; asset banned / under
moderation; group-owned audio isn't supported; not an audio asset; paid / for-sale;
archived / private / restricted; couldn't be found.

`/verifygroup` manages the bot's supported groups: `add`, `remove` (marks
**LOCKED**), `unlock`, `delete`, `list`. The list is persisted in
`supported_groups.json` and takes effect immediately.

---

## Staff (Killa feature) commands

These are **restricted** to the server Owner, Head Mods, and Mods (roles in
`VERIFYGROUP_ROLES`, plus the server owner, plus anyone the owner adds via
`/whitelist`). The admin commands `/ping`, `/verifygroup`, `/reload` and
`/whitelist` are stricter: **owner-only in DMs**, and inside a server only the
**Owner, Head Mod, and Mod** roles (the staff whitelist and generic admin
permissions are not enough).

The bot is registered **globally**, so every command also works in the bot's own
DMs. In a DM there are no guild roles, so staff commands are available to the app
owner (auto-detected), user ids in `OWNER_IDS`, and `/whitelist`ed users; admin
commands are **owner-only** in a DM.

| Command | What it does |
| --- | --- |
| `/analyze <file>` | Loudness (LUFS/peak), duration, bitrate, sample rate + a peak/RMS waveform PNG. |
| `/cr <file> [preset]` | Applies a key/speed shift preset (Subtle/Balanced/Stealth/Aggressive) or **Auto-Fit (closest)** — the auto-fit finds the mildest uniform shift a recogniser can't identify and returns the MP3 + a spectrogram. Exports as `<original_name>_cr.mp3`. |
| `/roblox <file> [quality]` | Simulates Roblox's two-pass libvorbis OGG compression and returns the OGG + waveform + loudness. |
| `/shazam <file>` | Identifies the song in an attached audio file via Shazam (SongRec) and returns title, artist, album, genre + a Shazam link. |
| `/download <url> [kind]` | Downloads a YouTube / web video via the yt-dlp CLI. `kind` is `audio` (MP3, default) or `video` (MP4 capped at 720p). |
| `/monitor <asset id>` | Polls a Roblox asset's moderation state every 6s and pings when it leaves `Reviewing` (15-min timeout). Needs a valid `ROBLOX_COOKIE`. |
| `/groups` | Shows the Roblox groups the bot can grant audio for. |
| `/verifygroup add/remove/list [group id]` | Manage the supported Roblox groups. |
| `/ping` | Host CPU / RAM / GPU / OS health check. |
| `/reload` | Re-syncs the slash command tree. |
| `/whitelist add/remove/list @user` | Manage the extra staff allow-list. |

`/shazam` and `/cr Auto-Fit` also need the SongRec package (numpy, requests, pytz)
so the bot can reach Shazam. `/download` needs yt-dlp on PATH; YouTube links use
the `android,web_embedded` player clients, which the default `bestaudio` request
otherwise answers with HTTP 403.

If a link reports a "DRM protection" style error, that is usually a **login /
region / bot gate**, not real DRM. Enable cookies for it via env (and restart the
bot):
```
YT_DLP_COOKIES_BROWSER=chrome   # or firefox / edge
YT_DLP_COOKIES=C:\path\to\cookies.txt
```
True **content DRM** (Widevine/PlayReady — Netflix, Disney+, etc.) cannot be
downloaded; the bot does not bypass it.

### Extra env (optional)

```
OWNER_IDS=123456789,987654321   # extra user ids treated as the owner
CR_COOLDOWN_SECONDS=300         # seconds between /cr auto-fit runs, per user
STAFF_ROLE_IDS=111,222,333      # role ids treated as Mods/Head Mods
MIRROR_PRESENCE=1               # bot's online status follows the owner
MIRROR_PRESENCE_POLL=30         # seconds between owner-status checks
```

---

## Logging

Every slash command is logged to the console and appended to `usage.log` (next to
the scripts) as:

`[2026-09-15 05:19:00] user Name (id 123) used /cr preset=autofit`

`staff_whitelist.json` is created next to this script the first time you use
`/whitelist`.

---

## Testing without Discord

```bash
python -c "import verify_audio as v; print(v.verify_asset(109470258845792))"
```

Swap in any audio id. A successful call prints `ok: True`; a rejected asset raises
`AssetError` with the reason text.

---

[Discord developer portal]: https://discord.com/developers/applications
