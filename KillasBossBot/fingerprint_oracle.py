"""
Local Shazam-style fingerprint oracle.

This is a pure-Python re-implementation of the classic acoustic-fingerprint
matching algorithm that Mousai (via the AudD recognition API) and audio-shazam
(Shazam-style microservices) both rely on:

  1. STFT the audio into a magnitude spectrogram.
  2. Pick spectral "peaks" (landmarks) that are strong local maxima.
  3. For each anchor peak, pair it with nearby peaks in time and hash
     (anchor_freq, target_freq, time_gap).
  4. Recognise by counting how many of a candidate's hashes show up in the
     reference database.

Because these hashes are anchored on ABSOLUTE frequencies and on the TIME GAPS
between peaks, they are NOT invariant to a pitch shift (frequencies move) or a
tempo change (time gaps move). So a modest pitch/tempo shift flips the hash
match rate to ~0 while a human still hears the same song.

This module is used by the app's built-in "can't recognize" self-check. It is
not a production recognizer -- it just gives a runnable, dependency-light way
to prove the shift worked, on Windows, without needing a GUI/GNOME app or a
Java/Kafka/Elasticsearch deployment.
"""

import numpy as np
import librosa

# Tunables for the fingerprint. Keep the band inside roughly where a song's
# melody lives so we get good landmarks without noise floor garbage.
N_FFT = 2048
HOP = 512
MIN_FREQ = 120          # Hz, lower band
MAX_FREQ = 4000         # Hz, upper band
MIN_DB = -45.0          # a peak must be this loud (dB relative to max) to count
PEAKS_PER_FRAME = 6     # strongest peaks kept per time frame
TARGET_ZONE = (10, 60)  # min/max frames after an anchor to look for a target
FUZZ = 2                # allow hashes within +/- this many frames of each other
                        # to match, so a tiny jitter doesn't false-negative

# Shazam/Mousai only need a few seconds of audio to name a track, but the
# fingerprint pairs every peak with its neighbours across the WHOLE file. On a
# 2-3 minute song that STFT + peak-pairing builds millions of hashes -- slow and
# multi-GB memory-heavy -- which made the app's "Test" button appear to hang.
# Fingerprinting a short clip centred on the middle keeps the oracle fast and
# bounded while still comparing the same musical section of ref and candidate.
CLIP_SECONDS = 12.0


def _db(mag, ref):
    """Convert magnitude to dB relative to a reference value."""
    return 20.0 * np.log10(np.maximum(mag, 1e-12) / max(ref, 1e-12))


def _peaks(S, sr):
    """Return a list of (frame, freq_hz) landmark peaks."""
    dt = N_FFT / float(sr) * 0.5  # hann window doubles the effective window
    freqs = np.linspace(0, sr / 2.0, S.shape[0])
    mask = (freqs >= MIN_FREQ) & (freqs <= MAX_FREQ)
    freqs = freqs[mask]
    smag = S[mask, :]

    if smag.size == 0:
        return []

    smag_db = _db(smag, smag.max())
    peaks = []
    n_frames = smag.shape[1]

    # Local maxima across frequency, then keep the strongest per frame.
    for t in range(n_frames):
        col = smag_db[:, t]
        local = np.zeros(col.shape, dtype=bool)
        local[1:-1] = (col[1:-1] > col[:-2]) & (col[1:-1] > col[2:])
        local &= col > MIN_DB
        idx = np.where(local)[0]
        if idx.size == 0:
            continue
        # keep the strongest PEAKS_PER_FRAME bins
        order = idx[np.argsort(col[idx])[::-1][:PEAKS_PER_FRAME]]
        for i in order:
            peaks.append((t, float(freqs[i])))
    return peaks


def _hashes(peaks):
    """Turn landmarks into a hash list [(anchor_t, fa, fb, gap), ...].

    anchor_t is the time frame of the anchor peak; fa/fb are the anchor and
    target frequencies; gap is the frames between them.
    """
    lo, hi = TARGET_ZONE
    h = []
    peaks.sort(key=lambda p: p[0])
    for i, (ta, fa) in enumerate(peaks):
        j = i + 1
        while j < len(peaks) and peaks[j][0] - ta <= hi:
            tb, fb = peaks[j]
            gap = tb - ta
            if gap >= lo:
                h.append((ta, int(round(fa * 2.0)), int(round(fb * 2.0)), gap))
            j += 1
    return h


def _index(hashes):
    """Build a lookup dict: (fa, fb, gap) -> list of anchor times."""
    db = {}
    for ta, fa, fb, gap in hashes:
        db.setdefault((fa, fb, gap), []).append(ta)
    return db


def fingerprint(path):
    """Return the (hashes, index) for a short clip of an audio file on disk."""
    y, sr = librosa.load(path, sr=44100, mono=True)
    n = int(CLIP_SECONDS * sr)
    if len(y) > n:
        start = (len(y) - n) // 2
        y = y[start:start + n]
    S = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=HOP))
    hashes = _hashes(_peaks(S, sr))
    return hashes, _index(hashes)


def match_score(ref, cand):
    """Return the best-fraction of candidate hashes that align on one offset.

    Mirrors the Shazam technique: look up each candidate hash in the reference
    database, and histogram the *time offset* (cand_anchor - ref_anchor). A real
    same-song pair produces one dominant offset shared by most hashes; a shifted
    clip produces no consistent offset, so the score collapses toward 0.
    ref/cand are the (hash_list, index) tuples returned by fingerprint().
    """
    ref_hashes, ref_index = ref
    cand_hashes, _ = cand
    if not cand_hashes or not ref_index:
        return 0.0
    offsets = {}
    for ta, fa, fb, gap in cand_hashes:
        anchors = ref_index.get((fa, fb, gap))
        if anchors:
            for tr in anchors:
                offsets[ta - tr] = offsets.get(ta - tr, 0) + 1
    best = max(offsets.values(), default=0)
    return best / float(len(cand_hashes))


def is_recognised(ref_path, cand_path, threshold=0.15):
    """True when cand_path still matches ref_path's fingerprint.

    threshold is the fraction of the candidate's hashes that share the single
    best time offset. Shazam-style matchers typically latch on to a clip once
    more than ~15-20% of hashes align. A pitch/tempo-shifted clip drops well
    below that because the offset gets smeared.
    """
    ref = fingerprint(ref_path)
    cand = fingerprint(cand_path)
    score = match_score(ref, cand)
    return score >= threshold, score
