"""
SongRec tester / real recogniser.

This has two jobs:

1. REAL recognition. SongRec's python version doesn't just match the signature
   locally -- it POSTs the generated signature to Shazam's servers and reads
   back the "matches" list. That *is* the recognised/not-recognised answer, so
   the app can prove a file would not be detected by the real recogniser,
   rather than trusting a homemade comparator. Those functions live below
   ("real_match_count" / "real_undetectable").

2. A local fallback signature comparator (signature / match_score /
   is_recognised) kept for the "Test" button, which also reports the real
   recogniser result.

Dependencies: numpy, requests, pytz, ffmpeg on PATH (for 16 kHz mono s16).
"""

import os
import subprocess
import sys
import time
import random
from locale import getlocale
from uuid import uuid5, getnode, NAMESPACE_DNS, NAMESPACE_URL

import numpy as np
from pytz import all_timezones
from requests import post

# Make SongRec's package importable regardless of the current working dir.
_SONGREC_SRC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "audio_tools", "SongRec", "python-version", "src",
)
if _SONGREC_SRC not in sys.path:
    sys.path.insert(0, _SONGREC_SRC)

from songrec.fingerprinting.algorithm import SignatureGenerator  # noqa: E402
from songrec.fingerprinting.user_agent import USER_AGENTS  # noqa: E402

# A pitch/tempo shift must move enough peaks that the best time-offset alignment
# collapses. A real match keeps a large share of peaks on one offset.
MATCH_THRESHOLD = 0.15
FREQ_BIN_TOL = 2  # +/- this many FFT bins counts as the "same" anchor


def _s16_16k(path):
    """Return signed 16-bit 16 kHz mono samples for an audio file (via ffmpeg)."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path,
         "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16)


def signature(path, clip_seconds=12.0):
    """Generate SongRec's peak list: [(fft_pass, freq_bin, band), ...].

    Recognition only needs a few seconds of audio, so we feed just a clip
    (starting near the middle for long songs, mirroring SongRec's own tooling).
    This keeps the Python signature loop fast on a full-length track.
    """
    samples = _s16_16k(path)
    max_samples = int(clip_seconds * 16000)
    if len(samples) > max_samples * 2:
        # long file: grab a 12s window around the middle
        start = len(samples) // 2 - max_samples // 2
        samples = samples[start:start + max_samples]
    elif len(samples) > max_samples:
        samples = samples[:max_samples]
    gen = SignatureGenerator()
    gen.MAX_TIME_SECONDS = clip_seconds
    gen.feed_input([int(x) for x in samples])
    peaks = []
    while True:
        msg = gen.get_next_signature()
        if msg is None:
            break
        for band, plist in msg.frequency_band_to_sound_peaks.items():
            for p in plist:
                peaks.append((p.fft_pass_number, p.corrected_peak_frequency_bin,
                              int(band)))
    return peaks


def _index(peaks):
    """(freq_bin, band) -> list of fft_pass times."""
    db = {}
    for t, fb, band in peaks:
        db.setdefault((fb, band), []).append(t)
    return db


def match_score(ref_peaks, cand_peaks):
    """Best fraction of candidate peaks sharing one time offset with a ref peak.

    Mirrors Shazam's offset-histogram: look up each candidate peak by (freq_bin,
    band) in the reference, and histogram the offset (cand_time - ref_time). A
    genuine match has one dominant offset; a shifted clip has none.
    """
    ref_index = _index(ref_peaks)
    if not ref_index or not cand_peaks:
        return 0.0
    offsets = {}
    for tc, fc, band in cand_peaks:
        hits = ref_index.get((fc, band))
        if hits is None:
            # small frequency tolerance so a tiny jitter isn't a false negative
            for d in range(1, FREQ_BIN_TOL + 1):
                hits = ref_index.get((fc + d, band)) or ref_index.get((fc - d, band))
                if hits:
                    break
        if hits:
            for tr in hits:
                offsets[tc - tr] = offsets.get(tc - tr, 0) + 1
    best = max(offsets.values(), default=0)
    return best / float(len(cand_peaks))


def is_recognised(ref_path, cand_path, threshold=MATCH_THRESHOLD):
    """(recognised, score) for cand_path vs ref_path using SongRec's algorithm."""
    ref = signature(ref_path)
    cand = signature(cand_path)
    score = match_score(ref, cand)
    return score >= threshold, score


# --------------------------------------------------------------------------
# REAL recognition -- Shazam's servers, exactly as SongRec uses them.
#
# SongRec's "communication.py" sends the generated signature to a Shazam tag
# endpoint and reads back the "matches" list. A non-empty list means the track
# was identified. We reproduce that call so we can control HTTP status / retry
# (the server rate-limits by device id, so repeated hits return an empty body).
# --------------------------------------------------------------------------

_BREATH_SECONDS = 6.0   # pause between Shazam POSTs (avoids throttling)
_POST_RETRIES = 3       # attempts per window before calling it inconclusive
_CLIP_SECONDS = 12.0    # Shazam only needs a short clip to recognise

# Rate-limit back-off. When the server throttles (a POST returns an empty body,
# a non-200, or otherwise doesn't yield usable matches), we pause for a growing
# cool-down instead of aborting the whole fit; once it passes we resume.
_RATE_LIMIT_BASE = 30.0   # first cool-down after a throttle (seconds)
_RATE_LIMIT_MAX = 180.0   # cap on the cool-down
_RATE_LIMIT_EXPONENT = 2  # cool-down doubles each consecutive throttle

# Global pace keeper: the server throttles requests that come too close
# together (drive by a device id), so we space *every* POST by a fixed amount
# instead of retrying into a throttle cascade.
_last_post_time = time.time() - _BREATH_SECONDS

# Absolute time (time.time()) before which we must not POST again. Set by the
# back-off when a throttle is detected; _throttle() sleeps until it passes.
_rate_limit_until = 0.0
_consecutive_limits = 0

# Optional callback for reporting a back-off pause to the UI (e.g. the app's
# status bar). Registered via set_wait_callback().
_wait_cb = None


def set_wait_callback(cb=None):
    """Register (or clear) a callback that receives back-off messages."""
    global _wait_cb
    _wait_cb = cb


def _notify_wait(seconds, reason):
    if _wait_cb:
        try:
            _wait_cb("pausing %.0fs: %s" % (seconds, reason))
        except Exception:
            pass


def _mark_rate_limited():
    """Record a detected throttle and raise the global cool-down window."""
    global _rate_limit_until, _consecutive_limits, _last_post_time
    _consecutive_limits += 1
    wait = min(
        _RATE_LIMIT_MAX,
        _RATE_LIMIT_BASE * (_RATE_LIMIT_EXPONENT ** (_consecutive_limits - 1)),
    )
    _rate_limit_until = time.time() + wait
    _last_post_time = time.time()  # don't double-wait for the base spacing
    _notify_wait(wait, "Shazam is rate-limiting; waiting it out")


def _mark_ok():
    """Reset the back-off once the server answers normally again."""
    global _consecutive_limits
    _consecutive_limits = 0


def _throttle():
    """Sleep past the rate-limit cool-down, then keep the base POST spacing."""
    global _last_post_time
    now = time.time()
    if _rate_limit_until > now:
        wait = _rate_limit_until - now
        _notify_wait(wait, "Shazam cool-down still running")
        time.sleep(wait)
        now = time.time()
    elapsed = now - _last_post_time
    if elapsed < _BREATH_SECONDS:
        time.sleep(_BREATH_SECONDS - elapsed)
    _last_post_time = time.time()

_tz_locale = (getlocale()[0] or "en_US").split(".")[0]
_device_a = str(uuid5(NAMESPACE_DNS, str(getnode()))).upper()
_device_b = str(uuid5(NAMESPACE_URL, str(getnode())))


def _window_signature(path, start_seconds, clip_seconds=_CLIP_SECONDS):
    """Build SongRec's signature for the clip starting at start_seconds.

    Returns None if there isn't enough audio at that offset.
    """
    samples = _s16_16k(path)
    start = int(start_seconds * 16000)
    chunk = samples[start:start + int(clip_seconds * 16000)]
    if len(chunk) < 1024:
        return None
    gen = SignatureGenerator()
    gen.MAX_TIME_SECONDS = clip_seconds
    gen.feed_input([int(x) for x in chunk])
    return gen.get_next_signature()


def _post_signature(msg):
    """Send a DecodedMessage signature to Shazam's tag endpoint."""
    fuzz = random.random() * 15.3 - 7.65
    random.seed(getnode())
    return post(
        "https://amp.shazam.com/discovery/v5/fr/FR/android/-/tag/"
        + _device_a + "/" + _device_b,
        params={"sync": "true", "webv3": "true", "sampling": "true",
                "connected": "", "shazamapiversion": "v3", "sharehub": "true",
                "video": "v3"},
        headers={"Content-Type": "application/json",
                 "User-Agent": random.choice(USER_AGENTS),
                 "Content-Language": _tz_locale},
        json={
            "geolocation": {
                "altitude": random.random() * 400 + 100 + fuzz,
                "latitude": random.random() * 180 - 90 + fuzz,
                "longitude": random.random() * 360 - 180 + fuzz,
            },
            "signature": {
                "samplems": int(msg.number_samples / msg.sample_rate_hz * 1000),
                "timestamp": int(time.time() * 1000),
                "uri": msg.encode_to_uri(),
            },
            "timestamp": int(time.time() * 1000),
            "timezone": random.choice([t for t in all_timezones if "Europe/" in t]),
        },
        timeout=20,
    )


def real_match_count(path, start_seconds, clip_seconds=_CLIP_SECONDS):
    """Real recogniser match count for one window of the file.

    Returns an int match count, or None only after _POST_RETRIES attempts have
    all failed to produce a usable answer. On a rate-limit it pauses (back-off)
    and retries rather than aborting, so a briefly-throttled server eventually
    cools down and the search continues. None must NOT be treated as "no match".
    """
    msg = _window_signature(path, start_seconds, clip_seconds)
    if msg is None:
        return None
    last_err = None
    for _ in range(_POST_RETRIES):
        _throttle()
        try:
            resp = _post_signature(msg)
        except Exception as exc:
            # Network-level failure (server didn't answer). Not necessarily a
            # rate-limit; retry after the normal spacing without inflating the
            # back-off.
            last_err = repr(exc)
            continue
        if resp.status_code != 200:
            # Server reached but refused -> treat as a rate-limit and cool down.
            last_err = "HTTP %d" % resp.status_code
            _mark_rate_limited()
            continue
        try:
            data = resp.json()
        except Exception as exc:
            # 200 but an empty / unparseable body is Shazam's classic throttle.
            last_err = repr(exc)
            _mark_rate_limited()
            continue
        # Only a body that actually carries a `matches` LIST is a usable answer.
        # A 200 whose JSON is parseable but has NO matches field (e.g. a bare
        # {} or a stale/empty envelope) is another throttle signature, and must
        # NOT be treated as "0 matches" -- that would let the auto-fit accept a
        # shift Shazam can still identify.
        matches = data.get("matches") if isinstance(data, dict) else None
        if not isinstance(matches, list):
            last_err = "200 without a matches list"
            _mark_rate_limited()
            continue
        _mark_ok()
        return len(matches)
    return None


def real_undetectable(path, windows, clip_seconds=_CLIP_SECONDS,
                      progress=None):
    """(ok, per-window match counts).

    True only if EVERY window gives a clean recogniser result of 0 matches.
    Any None (inconclusive) or >0 makes the whole test fail, because a failed
    request is not proof of a clean match -- that short-sightedness is what let
    the old preset-based fit slip through.

    progress(i, w, total) is called before each window so a UI can show which
    probe is running (the whole pass spaces POSTs and can take a while).
    """
    counts = []
    total = len(windows)
    for i, w in enumerate(windows):
        if progress:
            progress(i, w, total)
        counts.append(real_match_count(path, w, clip_seconds))
    ok = (None not in counts) and all(c == 0 for c in counts)
    return ok, counts


def duration(path):
    """Duration in seconds, from the same 16 kHz mono decode the tests use."""
    return len(_s16_16k(path)) / 16000.0


# --------------------------------------------------------------------------
# Recognition that RETURNS the song (not just a match count).
#
# real_match_count() only tells us whether Shazam found anything. For a "what
# song is this?" lookup (/shazam) we want the actual track metadata, so we parse
# the first matches[].track -- the format Shazam returns and SongRec ignores.
# --------------------------------------------------------------------------

class RecognizerError(RuntimeError):
    """The file couldn't be analysed, or Shazam couldn't be reached."""
    pass


def _describe_match(data):
    """Parse the track metadata from a Shazam tag response.

    Shazam puts the song info at the TOP level of the response (`track`); the
    `matches` list only carries the detection offsets ({id, offset, timeskew,
    frequencyskew}) and has no track. So we read `data["track"]`, not the first
    match. Album/label/release live under `sections[].metadata` (some responses
    spell it `metadatas`), and the artist is `subtitle`.
    """
    track = data.get("track") if isinstance(data, dict) else None
    track = track or {}
    meta = {}
    for sec in track.get("sections") or []:
        if sec.get("type") != "SONG":
            continue
        for m in (sec.get("metadata") or sec.get("metadatas") or []):
            title = (m.get("title") or "").lower()
            if title and m.get("text"):
                meta[title] = m["text"]
        break
    artists = track.get("artists") or []
    images = track.get("images") or {}
    genres = track.get("genres") or {}
    hub = track.get("hub") or {}
    return {
        "matched": True,
        "title": track.get("title"),
        "artist": track.get("subtitle")
        or next((a.get("name") for a in artists if a.get("name")), None),
        "album": meta.get("album"),
        "label": meta.get("label"),
        "released": meta.get("released"),
        "explicit": hub.get("explicit") if isinstance(hub, dict) else None,
        "genres": genres.get("primary") if isinstance(genres, dict) else None,
        "coverart": images.get("coverarthq") or images.get("coverart"),
        "url": track.get("url"),
        "key": track.get("key"),
    }


def recognize(path, clip_seconds=_CLIP_SECONDS, progress=None):
    """Identify the song in an audio file via Shazam.

    Returns a dict (see _describe_match) with "matched" True when Shazam found
    the track, or {"matched": False} when Shazam answered cleanly but didn't
    recognise the clip. Raises RecognizerError only when the file couldn't be
    analysed or Shazam couldn't be reached conclusively (e.g. throttled).
    """
    samples = _s16_16k(path)
    if len(samples) < 1024:
        raise RecognizerError("the file is too short to recognise.")
    # Grab a clip centred on the middle of the file, mirroring SongRec's own
    # tooling -- the intro/fade rarely carries the identifying peaks.
    start = max(0, len(samples) // 2 - int(clip_seconds * 16000) // 2)
    chunk = samples[start:start + int(clip_seconds * 16000)]
    if len(chunk) < 1024:
        raise RecognizerError("the file is too short to recognise.")
    gen = SignatureGenerator()
    gen.MAX_TIME_SECONDS = clip_seconds
    gen.feed_input([int(x) for x in chunk])
    msg = gen.get_next_signature()
    if msg is None:
        raise RecognizerError("couldn't build a signature for this file.")

    last_err = None
    for _ in range(_POST_RETRIES):
        _throttle()
        try:
            resp = _post_signature(msg)
        except Exception as exc:  # noqa: BLE001
            last_err = repr(exc)
            continue
        if resp.status_code != 200:
            last_err = "HTTP %d" % resp.status_code
            _mark_rate_limited()
            continue
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            last_err = repr(exc)
            _mark_rate_limited()
            continue
        matches = data.get("matches") if isinstance(data, dict) else None
        if not isinstance(matches, list):
            last_err = "200 without a matches list"
            _mark_rate_limited()
            continue
        _mark_ok()
        if not matches:
            return {"matched": False}
        return _describe_match(data)
    raise RecognizerError(
        "Shazam didn't answer (rate-limited or unreachable): %s" % last_err)
