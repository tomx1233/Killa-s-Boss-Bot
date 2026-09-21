"""
KILLA'S BOSS -- Killa tools engine.

A faithful port of the useful Scope bot (github.com/typicaalusername/scope)
commands, written for the Python discord.py audio bot. Everything here is a
SYNCHRONOUS helper (subprocess ffmpeg/ffprobe + urllib), meant to be called
through `asyncio.to_thread` so it never blocks the Discord event loop.

Command surface this module backs (the slash wrappers live in audio_bot.py):
  /analyze      -> analyze_audio() + render_waveform()
  /cr         -> cr_process()                        (preset key/speed shift)
  /roblox       -> roblox_simulate()                     (two-pass ogg compression)
  /monitor      -> get_asset_moderation()                (poll a Roblox asset's moderation)
  /download     -> download_media() (yt-dlp CLI: audio mp3 or <=720p video mp4)
  /ping         -> get_system_stats()

Attribution: this bot is a fork of typicaalusername's Scope bot (GPL-3.0). The
ffmpeg filter recipes (ebur128 loudness, showwavespic overlay, anequalizer notch,
libvorbis two-pass) and the two Roblox polling endpoints are derived from it.
"""

import glob
import gzip
import json
import os
import platform
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

# The grant account session used for user-auth / toolbox endpoints. Loaded from
# .env the same way grant_audio does (audio_bot.py calls load_dotenv before
# importing this module, so the var is already present).
ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE", "")

# The workspace root that holds the audio-modifier engine (shazam_proof.py +
# songrec_tester.py + fingerprint_oracle.py, plus the SongRec package). In this
# handoff copy those files sit in the folder one level up from audio_bot/ (the
# bot folder's parent). If you move them, point this at that folder.
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0 Safari/537.36 RobloxApp")

# Endpoints (same as the original Scope bot; both confirmed against the live API).
MODERATION_URL = "https://apis.roblox.com/assets/user-auth/v1/assets/{id}"
TOOLBOX_URL = ("https://apis.roblox.com/toolbox-service/v1/marketplace/3"
               "?keyword={artist}&limit=999999&uiSortIntent=10")
ASSET_DELIVERY_URL = "https://assetdelivery.roblox.com/v1/asset/?id={id}"

GREEN = 0x4CAF50
RED = 0xE53935


# --------------------------------------------------------------------------- #
# subprocess helpers
# --------------------------------------------------------------------------- #
def _run(args, timeout=180):
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        raise RuntimeError("ffmpeg/ffprobe is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg timed out processing that file (it may be too long).")


_FFMPEG = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
_FFPROBE = ["ffprobe", "-v", "error"]


def ffprobe_json(path):
    """Probe an audio file -> dict with 'format' and 'streams'."""
    rc, out, err = _run(_FFPROBE + ["-print_format", "json", "-show_format",
                                    "-show_streams", path])
    if rc != 0 or not out.strip():
        raise RuntimeError("Could not read that file with ffprobe."
                           + (f" ({err.strip()})" if err.strip() else ""))
    return json.loads(out)


def _loudness(path):
    """Integrated loudness (LUFS) + true peak (dBFS) via ffmpeg ebur128."""
    # ebur128 prints its summary at info level, so we can't pass -loglevel error.
    rc, _out, err = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
                          "-af", "ebur128=peak=true", "-f", "null", "-"])
    if not err:
        raise RuntimeError("ffmpeg gave no loudness output.")
    m_lufs = __import__("re").search(r"Integrated loudness[\s\S]*?I:\s+(-?[\d.]+) LUFS", err)
    m_peak = __import__("re").search(r"Peak:\s+(-?[\d.]+) dBFS", err)
    return (float(m_lufs.group(1)) if m_lufs else None,
            float(m_peak.group(1)) if m_peak else None)


def _sample_rate(path):
    """Pull the audio stream sample rate (ffprobe), defaulting sensibly."""
    try:
        data = ffprobe_json(path)
        stream = next(s for s in data.get("streams", [])
                      if s.get("codec_type") == "audio")
        return int(stream.get("sample_rate") or 44100) or 44100
    except Exception:
        return 48000


# --------------------------------------------------------------------------- #
# /analyze
# --------------------------------------------------------------------------- #
def analyze_audio(path):
    """Return a dict of format/loudness info for an audio file."""
    data = ffprobe_json(path)
    fmt = data.get("format", {})
    stream = next((s for s in data.get("streams", [])
                   if s.get("codec_type") == "audio"), {})
    lufs, peak = _loudness(path)

    duration = float(fmt.get("duration") or 0)
    minutes = int(duration // 60)
    seconds = int(duration % 60)
    bitrate = fmt.get("bit_rate")
    return {
        "duration": f"{minutes}:{seconds:02d}",
        "duration_secs": duration,
        "bitrate_kbps": round(int(bitrate) / 1000) if bitrate else None,
        "sample_rate": stream.get("sample_rate"),
        "codec": stream.get("codec_name"),
        "channels": stream.get("channels"),
        "lufs": lufs,
        "peak": peak,
    }


def render_waveform(path, out_path, size="1920x660"):
    """Render a peak+rms waveform PNG (blue peak over darker purple rms)."""
    peak_color, rms_color = "2986CC", "5C3D8C"
    fx = (f"[0:a]showwavespic=s={size}:colors={peak_color}:filter=peak"
          f":split_channels=1[peaks];"
          f"[0:a]showwavespic=s={size}:colors={rms_color}:filter=average"
          f":split_channels=1[rms];[peaks][rms]overlay")
    rc, _out, err = _run(_FFMPEG + ["-i", path, "-filter_complex", fx,
                                    "-update", "1", out_path])
    if rc != 0:
        raise RuntimeError("waveform render failed." + (f" ({err.strip()})" if err.strip() else ""))
    return out_path


# Brand tint for the spectrogram image: recolor the ffmpeg luminance ramp onto the
# KILLA'S BOSS blue-purple palette so the graph matches the embeds + waveform
# (dark violet -> brand purple -> peak blue) instead of the default plasma ramp.
_BRAND_LOW = (58, 25, 76)     # deep violet  #3A194C
_BRAND_MID = (92, 61, 140)    # rms purple   #5C3D8C
_BRAND_HIGH = (41, 134, 204)  # peak blue    #2986CC


def _brand_spectrogram(path):
    """Recolor `path` (an ffmpeg spectrogram PNG) onto the brand blue/purple ramp.

    ffmpeg's showspectrumpic `color` option only accepts named colormaps (plasma,
    magma, ...) -- none is the KILLA'S BOSS palette. So we keep any valid colormap,
    take its grayscale luminance as the energy ramp, and redraw it as a
    dark-violet -> purple -> blue gradient that matches the waveform + embeds.
    """
    import numpy as np
    from PIL import Image

    lum = np.asarray(Image.open(path).convert("L"), dtype=np.float32) / 255.0
    low, mid, high = (np.array(_BRAND_LOW, np.float32),
                      np.array(_BRAND_MID, np.float32),
                      np.array(_BRAND_HIGH, np.float32))
    t = np.clip(lum, 0.0, 1.0)[..., None]            # (h, w, 1)
    down = low + (mid - low) * np.clip(t * 2.0, 0.0, 1.0)
    up = mid + (high - mid) * np.clip((t - 0.5) * 2.0, 0.0, 1.0)
    rgb = np.where(t <= 0.5, down, up).astype("uint8")
    Image.fromarray(rgb, "RGB").save(path)


# /cr preset table: name -> (pitch_semitones, tempo_factor).
# These just re-encode a file with a key/speed shift; the names stay neutral.
CR_PRESETS = {
    "subtle":     (1.5, 1.04),
    "balanced":   (2.0, 1.07),
    "stealth":    (2.5, 1.10),
    "aggressive": (3.0, 1.12),
}


def cr_process(input_path, output_path, spectrogram_path, preset_name="balanced"):
    """Re-encode a file with an independent pitch + tempo shift preset.

    Uses `asetrate` to shift the pitch and `atempo` to restore or change the
    tempo, so a file can be shifted in key and/or speed independently of its
    duration. Returns the rebuilt info dict for the processed file.
    """
    if preset_name not in CR_PRESETS:
        raise RuntimeError(f"unknown preset {preset_name!r}")
    semi, tempo = CR_PRESETS[preset_name]
    pitch_factor = 2.0 ** (semi / 12.0)
    sr = _sample_rate(input_path)

    # asetrate=sr*pitch_factor raises pitch (and speed) by pitch_factor;
    # aresample returns to the source rate; atempo=1/pitch_factor restores the
    # original tempo while keeping the shifted pitch; atempo=tempo applies the
    # preset's tempo change. Each factor stays inside atempo's 0.5-2.0 range.
    af = (
        f"asetrate={sr * pitch_factor:.6f},"
        f"aresample={sr},"
        f"atempo={1.0 / pitch_factor:.6f},"
        f"atempo={tempo:.6f}"
    )

    rc, _out, err = _run(_FFMPEG + ["-i", input_path, "-af", af,
                                    "-c:a", "libmp3lame", "-b:a", "192k", output_path])
    if rc != 0:
        raise RuntimeError("cr pass failed." + (f" ({err.strip()})" if err.strip() else ""))

    rc, _out, err = _run(_FFMPEG + ["-i", output_path, "-lavfi",
                                    "showspectrumpic=s=1100x280:legend=0:gain=.5:color=plasma",
                                    spectrogram_path])
    if rc != 0:
        raise RuntimeError("spectrogram render failed." + (f" ({err.strip()})" if err.strip() else ""))
    _brand_spectrogram(spectrogram_path)
    return analyze_audio(output_path)


def cr_autofit(input_path, output_path, spectrogram_path, progress=None):
    """Closed-loop auto-fit: the mildest uniform shift the recogniser can't ID.

    Mirrors the Audio Edit App's "Auto-Fit": it probes a candidate shift at every
    point across the whole song, renders a candidate only when the shift is clean
    at every point, re-confirms it on the finished file, and escalates to a
    stronger (but still uniform pitch/tempo) shift until the real recogniser
    (Shazam via SongRec) can't identify any part. It only ever uses that one
    method, so the output always sounds like the same song, just a touch faster
    and/or a half-step flat.

    `progress(msg)` is called between steps (from this worker thread) so the bot
    can show what it's trying. Returns:
        {"pitch", "tempo", "clean", "counts", "info"}
    where `clean` is True only when every probe window confirmed a clean result
    (counts is None when the search fell back to the strongest shift as a best
    effort because nothing on the ladder cleared).
    """
    if not os.path.isfile(input_path):
        raise RuntimeError("the input file could not be read from disk.")

    import sys  # noqa: PLC0415
    if WORKSPACE_ROOT not in sys.path:
        sys.path.insert(0, WORKSPACE_ROOT)

    try:
        import shazam_proof as sp  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "The auto-fit engine isn't available in this Python (it needs librosa, "
            "soundfile and the SongRec package). Run the bot with the same Python "
            "3.10 that runs the Audio Edit App, which has them -- see "
            "run_audio_bot.bat. (%s)" % e
        )

    work_wav = os.path.join(os.path.dirname(output_path) or ".", "autofit_work.wav")
    if progress:
        progress("starting the auto-fit search (it contacts Shazam and may take a while)")

    try:
        result = sp.find_undetectable(input_path, work_wav, progress=progress)
    except RuntimeError as e:
        raise RuntimeError(str(e))

    pitch = result["pitch"]
    tempo = result["tempo"]
    counts = result.get("counts")
    clean = counts is not None

    # The engine writes a WAV; re-encode to MP3 so the Discord attachment stays
    # small enough to send.
    rc, _out, err = _run(_FFMPEG + ["-i", work_wav, "-c:a", "libmp3lame",
                                    "-b:a", "192k", output_path])
    if rc != 0:
        raise RuntimeError("auto-fit mp3 re-encode failed." + (f" ({err.strip()})" if err.strip() else ""))
    rc, _out, err = _run(_FFMPEG + ["-i", output_path, "-lavfi",
                                    "showspectrumpic=s=1100x280:legend=0:gain=.5:color=plasma",
                                    spectrogram_path])
    if rc != 0:
        raise RuntimeError("spectrogram render failed." + (f" ({err.strip()})" if err.strip() else ""))
    _brand_spectrogram(spectrogram_path)

    if progress:
        progress(f"done: used {pitch:+.1f} st / tempo {tempo:.2f}x")
    return {
        "pitch": pitch,
        "tempo": tempo,
        "clean": clean,
        "counts": counts,
        "info": analyze_audio(output_path),
    }


# --------------------------------------------------------------------------- #
# /shazam  (SongRec recognition)
# --------------------------------------------------------------------------- #
def shazam_recognize(input_path, progress=None):
    """Identify an uploaded audio file via Shazam's servers.

    Returns a dict describing the best match:
        {"matched": bool, "title", "artist", "album", "label", "released",
         "genres", "coverart", "url", "key"}
    `matched` is False when Shazam answered cleanly but found nothing. Raises
    RuntimeError with a friendly message when recognition is impossible.
    """
    if not os.path.isfile(input_path):
        raise RuntimeError("the input file could not be read from disk.")

    import sys  # noqa: PLC0415
    if WORKSPACE_ROOT not in sys.path:
        sys.path.insert(0, WORKSPACE_ROOT)

    try:
        import songrec_tester as st  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "SongRec isn't available in this Python (it needs numpy, requests, "
            "pytz and the SongRec package). Run the bot with the same Python "
            "3.10 that runs the Audio Edit App, which has them -- see "
            "run_audio_bot.bat. (%s)" % e
        )

    if progress:
        progress("contacting Shazam to identify the song")
    try:
        return st.recognize(input_path, progress=progress)
    except st.RecognizerError as e:
        raise RuntimeError(str(e))


def search_links(title, artist):
    """Web search URLs for a title/artist.

    Shazam sometimes returns a hit that isn't a real published release, so a
    search link lets the user hunt it down instead of getting a dead end.
    Returns {} when there's nothing to search for.
    """
    q = " ".join(x for x in (artist or "", title or "") if x).strip()
    if not q:
        return {}
    enc = urllib.parse.quote_plus(q)
    return {
        "YouTube": f"https://www.youtube.com/results?search_query={enc}",
        "Spotify": f"https://open.spotify.com/search/{enc}",
        "Apple Music": f"https://music.apple.com/us/search?term={enc}",
    }


# --------------------------------------------------------------------------- #
# /roblox  (two-pass libvorbis compression simulation)
# --------------------------------------------------------------------------- #
def roblox_simulate(input_path, pass1_path, pass2_path, waveform_path, quality=0.5):
    """Re-encode via two libvorbis passes (Roblox-style), then analyze + waveform.

    Returns (info_dict, waveform_path).
    """
    rc, _out, err = _run(_FFMPEG + ["-i", input_path, "-af", "aformat=sample_fmts=s16",
                                    "-c:a", "libvorbis", "-q:a", str(quality), pass1_path])
    if rc != 0:
        raise RuntimeError("first compression pass failed." + (f" ({err.strip()})" if err.strip() else ""))
    rc, _out, err = _run(_FFMPEG + ["-i", pass1_path, "-c:a", "libvorbis",
                                    "-q:a", str(quality), pass2_path])
    if rc != 0:
        raise RuntimeError("second compression pass failed." + (f" ({err.strip()})" if err.strip() else ""))
    info = analyze_audio(pass2_path)
    render_waveform(pass2_path, waveform_path)
    return info, waveform_path


# --------------------------------------------------------------------------- #
# /ping  (system stats)
# --------------------------------------------------------------------------- #
def get_system_stats():
    """Return a small dict of CPU / RAM / GPU / OS info for /ping."""
    stats = {"host": platform.node(), "os": platform.platform(),
             "cpu": platform.processor() or platform.machine(),
             "ram": None, "gpu": "Integrated"}

    try:
        import psutil  # noqa: PLC0415
        vm = psutil.virtual_memory()
        stats["ram"] = f"{vm.used / 1024**3:.2f}GB / {vm.total / 1024**3:.2f}GB"
    except Exception:
        stats["ram"] = "unknown"

    if os.name == "nt":
        try:
            rc, out, _err = _run(["nvidia-smi", "--query-gpu=name",
                                  "--format=csv,noheader"])
            if rc == 0 and out.strip():
                stats["gpu"] = out.strip().splitlines()[0]
        except Exception:
            pass
    return stats


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _cookie_header(cookie):
    """Return a valid Cookie header value.

    The env var may hold just the .ROBLOSECURITY *token* (what a browser/dev
    tool hands you) or already a full 'name=value' cookie string. The Cookie
    header needs the cookie NAME, so wrap a bare token in '.ROBLOSECURITY='.
    """
    if not cookie:
        return ""
    if cookie.lstrip().startswith(".ROBLOSECURITY="):
        return cookie
    return ".ROBLOSECURITY=" + cookie


def _get(url, cookie=ROBLOX_COOKIE, timeout=15, return_headers=False):
    h = {"User-Agent": UA, "Accept": "*/*",
         "Referer": "https://create.roblox.com/", "Origin": "https://create.roblox.com"}
    if cookie:
        h["Cookie"] = _cookie_header(cookie)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            if return_headers:
                return resp.status, body, dict(resp.headers)
            return resp.status, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        if return_headers:
            return e.code, body, dict(e.headers)
        return e.code, body
    except Exception as e:
        return None, "", {}


def _get_bytes(url, cookie=ROBLOX_COOKIE, timeout=30):
    h = {"User-Agent": UA, "Accept": "*/*",
         "Referer": "https://create.roblox.com/", "Origin": "https://create.roblox.com"}
    if cookie:
        h["Cookie"] = _cookie_header(cookie)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return None, b""


# --------------------------------------------------------------------------- #
# /monitor  (asset moderation)
# --------------------------------------------------------------------------- #
def require_cookie():
    """Raise a clear message when there's no cookie (ending with the scoped reply)."""
    if not ROBLOX_COOKIE:
        raise RuntimeError("no Roblox cookie detected -- set ROBLOX_COOKIE in .env")


def get_asset_moderation(asset_id):
    """GET an asset's moderation state. Returns a dict, or raises on the reasons
    scope surfaced (401 bad cookie, non-200, unreadable state)."""
    require_cookie()
    status, text, _headers = _get(MODERATION_URL.format(id=asset_id), return_headers=True)
    if status == 401:
        raise RuntimeError(
            "authentication failed: the ROBLOX_COOKIE in .env isn't a valid session for the "
            "moderation endpoint. /monitor needs a full .ROBLOSECURITY session cookie to read "
            "moderation state; the raw cookie string alone won't work.")
    if status is None:
        raise RuntimeError("could not reach Roblox to check moderation (network/CSRF). try again shortly.")
    if status != 200:
        raise RuntimeError(f"failed to fetch asset (status {status}). check the asset ID")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError("could not read a response from Roblox.")
    state = (data.get("moderationResult") or {}).get("moderationState")
    if not state:
        raise RuntimeError("could not read moderation state.")
    return {
        "state": state,
        "display_name": data.get("displayName") or data.get("name") or asset_id,
        "description": data.get("description") or "",
    }


def download_asset_audio(asset_id):
    """Download an audio asset's bytes via the delivery endpoint (follows redirects).

    assetdelivery serves the body GZIP-COMPRESSED (every asset type comes back
    wrapped in a `1f 8b` header, audio OggS bytes included), so it's gunzipped
    here before returning — otherwise ffmpeg/ffprobe see a gzip container, not
    audio, and the re-encode/analysis fails.
    """
    require_cookie()
    status, data = _get_bytes(ASSET_DELIVERY_URL.format(id=asset_id))
    if status == 200 and data:
        if data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except OSError:
                pass  # not a real gzip stream; hand back the bytes as-is
        return data
    if status in (401, 403, 409):
        # 401 = no valid session; 403/409 = account genuinely not allowed (a
        # private / group-locked / archived or non-audio asset). No downloader
        # can fetch those, so say so instead of a generic failure.
        if status == 401:
            raise RuntimeError(
                f"asset {asset_id} needs Roblox authentication, but the session "
                "isn't valid (401). refresh ROBLOX_COOKIE in .env.")
        raise RuntimeError(
            f"this asset ({asset_id}) isn't publicly downloadable — it returned "
            f"{status} 'not authorized' (private / group-locked or archived). No "
            "downloader can fetch it.")
    if status is None:
        raise RuntimeError("could not reach the Roblox delivery endpoint (network).")
    raise RuntimeError(f"could not download audio (status {status})")


# --------------------------------------------------------------------------- #
# /download  (yt-dlp)
# --------------------------------------------------------------------------- #
# yt-dlp is a separate CLI (installed via `winget install yt-dlp.yt-dlp`) and is
# NOT a Python module in the bot's interpreter, so it's driven as a subprocess.
# This fallback is the WinGet shim path in case the process PATH does not include
# the WinGet Links directory.
_YTDLP_FALLBACK = r"C:\Users\james\AppData\Local\Microsoft\WinGet\Links\yt-dlp"
# Discord's message-attachment ceiling (~25MB). Anything bigger is re-encoded.
_DISCORD_MAX_BYTES = 24 * 1024 * 1024


def _ytdlp_bin():
    """Locate the yt-dlp CLI (prefer PATH, else the known WinGet shim)."""
    p = shutil.which("yt-dlp")
    if p:
        return p
    if os.path.isfile(_YTDLP_FALLBACK):
        return _YTDLP_FALLBACK
    raise RuntimeError(
        "yt-dlp is not installed or not on PATH (install with: winget install yt-dlp.yt-dlp).")


def _run_yt(args, timeout, what="yt-dlp"):
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        raise RuntimeError(f"{what} is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{what} timed out (the file may be too long to download in time).")


def _cookies_args():
    """Build yt-dlp cookie args from opt-in env config (used for gated/region pages).

    Real content DRM (Widevine/PlayReady) is NOT acceptable here -- yt-dlp refuses
    to decrypt those and this code does nothing to help. What these cookies DO fix
    are the sites that simply want a logged-in session (age/login walls, some
    region-locked or bot-protected pages) and otherwise report a vague error.

      YT_DLP_COOKIES_BROWSER=chrome|firefox|edge   -> --cookies-from-browser <x>
      YT_DLP_COOKIES=<path to a cookies.txt>      -> --cookies <path>
    """
    args = []
    browser = (os.getenv("YT_DLP_COOKIES_BROWSER") or "").strip().lower()
    if browser:
        args += ["--cookies-from-browser", browser]
    cpath = (os.getenv("YT_DLP_COOKIES") or "").strip().strip('"')
    if cpath:
        args += ["--cookies", cpath]
    return args


def _err_hint(err):
    """Turn a raw yt-dlp error blob into a short, honest explanation."""
    e = err or ""
    el = e.lower()
    if any(k in el for k in ("drm", "widevine", "playready", "fairplay")):
        return ("That source is real DRM-protected content (Widevine/PlayReady). "
                "yt-dlp does not decrypt DRM, so no flag can download it. If it is "
                "only BEHIND a login or region gate instead, add your browser cookies "
                "and retry.")
    if any(k in el for k in ("sign in", "login required", "auth required", "cookies",
                             "cookie", "401", "403", "not authorized")):
        return ("The site is holding the file behind a login/region session. Set "
                "YT_DLP_COOKIES_BROWSER to chrome/firefox/edge (or point YT_DLP_COOKIES "
                "at a cookies.txt) and retry.")
    if any(k in el for k in ("geo", "your country", "your region", "unavailable in")):
        return ("That content is geo-restricted and is not being served in your region. "
                "A matching proxy/VPN would be needed.")
    return ""


def _safe_name(name, ext):
    """Turn a media title into a filesystem-safe attachment filename."""
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip()
    base = base[:60].strip() or "download"
    return f"{base}.{ext}"


def _find_output(out_dir):
    """Pick the finished download file (skip yt-dlp temp/partial artifacts)."""
    cands = [
        f for f in glob.glob(os.path.join(out_dir, "download.*"))
        if os.path.splitext(f)[1].lower() not in (".part", ".ytdl", ".tmp", ".json")
    ]
    if not cands:
        raise RuntimeError("yt-dlp finished but produced no output file.")
    return max(cands, key=os.path.getsize)


def _shrink(path, max_bytes, kind):
    """Re-encode a too-large file to progressively smaller sizes to fit Discord."""
    ext = os.path.splitext(path)[1].lower()
    out = os.path.splitext(path)[0] + "_small" + ext
    if kind == "audio":
        plans = [["-vn", "-codec:a", "libmp3lame", "-b:a", br] for br in ("128k", "96k", "64k")]
    else:
        plans = [[
            "-vf", f"scale=-2:{h}", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", crf, "-c:a", "aac", "-b:a", "128k",
        ] for h, crf in (("480", "28"), ("360", "30"), ("240", "30"))]
    for plan in plans:
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path] + plan + [out]
        try:
            p = subprocess.run(
                args, capture_output=True, text=True, timeout=150,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            if p.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) <= max_bytes:
                os.replace(out, path)   # keep the original filename, swap the content
                return path
        except Exception:
            pass
        if os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
    return None


def download_media(url, out_dir, kind="audio", max_bytes=_DISCORD_MAX_BYTES):
    """Download a video/audio URL via the yt-dlp CLI into out_dir.

    kind='audio' extracts the best audio as mp3; kind='video' grabs the best
    <=720p stream and merges it to mp4. Returns a dict with the media title,
    duration, uploader, source url, local path, size and any shrink note.
    Raises RuntimeError if yt-dlp can't read or download the link.
    """
    ytdlp = _ytdlp_bin()
    if not url or not str(url).strip().lower().startswith(("http://", "https://")):
        raise RuntimeError("That doesn't look like a URL (needs http:// or https://).")

    # Let yt-dlp find ffmpeg explicitly (it needs it to extract/merge audio video).
    # YouTube's default client frequently answers the bestaudio/media request with
    # HTTP 403 (throttling); the android / web_embedded players are allowed and
    # download fine, so pin those. They only apply to the youtube extractor, so
    # non-YouTube links are unaffected.
    base = [ytdlp, "--no-warnings", "--no-playlist",
            "--extractor-args", "youtube:player_client=android,web_embedded"]
    # Cookies let gated / region-locked / bot-protected pages work when the user
    # enables them (see _cookies_args). Real DRM still refuses -- that is intended.
    base += _cookies_args()
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        base += ["--ffmpeg-location", os.path.dirname(ffmpeg)]

    # Relaxed flags used as a second attempt, for pages that block the default
    # client or serve a self-signed cert. Harmless when the first pass succeeds.
    relaxed = ["--user-agent", UA, "--no-check-certificate",
               "--extractor-retries", "3", "--retries", "3"]

    def _probe(cmd):
        return _run_yt(cmd, timeout=60)

    # Phase A -- fast metadata probe (validates the link, gives us the title).
    rc, out, err = _probe(
        base + ["--skip-download", "--print", "%(title)s\t%(duration)s\t%(uploader)s", url])
    if rc != 0:
        rc, out, err = _probe(
            base + relaxed + ["--skip-download", "--print", "%(title)s\t%(duration)s\t%(uploader)s", url])
    if rc != 0:
        hint = _err_hint(err)
        raise RuntimeError("yt-dlp could not read that link."
                           + (f" {err.strip()}" if err.strip() else "")
                           + (f"\n{hint}" if hint else ""))
    parts = (out.strip().split("\t") + ["", "", ""])[:3]
    title, duration, uploader = parts[0], parts[1], parts[2]

    # Phase B -- the actual download into a fixed basename in out_dir.
    out_tmpl = os.path.join(out_dir, "download.%(ext)s")
    if kind == "audio":
        dl = base + ["-x", "--audio-format", "mp3", "--audio-quality", "0",
                     "-o", out_tmpl, url]
    else:
        dl = base + ["-f", "bv*[height<=720]+ba/b[height<=720]",
                     "--merge-output-format", "mp4", "-o", out_tmpl, url]
    rc, _out, err = _run_yt(dl, timeout=240)
    if rc != 0:
        rc, _out, err = _run_yt(dl + relaxed, timeout=240)
    if rc != 0:
        hint = _err_hint(err)
        raise RuntimeError("yt-dlp download failed."
                           + (f" {err.strip()}" if err.strip() else "")
                           + (f"\n{hint}" if hint else ""))

    path = _find_output(out_dir)
    note = ""
    if os.path.getsize(path) > max_bytes:
        if _shrink(path, max_bytes, kind):
            note = "Compressed to fit Discord's file-size limit."
        else:
            note = (f"Final file is {os.path.getsize(path) / 1e6:.1f} MB, "
                    "larger than Discord's attachment limit — you may need to trim it.")

    ext = os.path.splitext(path)[1].lstrip(".") or "download"
    final = os.path.join(out_dir, _safe_name(title, ext))
    if os.path.abspath(path) != os.path.abspath(final):
        os.replace(path, final)
        path = final

    return {
        "title": title or os.path.basename(path),
        "duration": duration or "—",
        "uploader": uploader or "—",
        "webpage_url": url,
        "path": path,
        "size": os.path.getsize(path),
        "ext": ext,
        "kind": kind,
        "note": note,
    }
