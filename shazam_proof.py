"""
Audio modifier that defeats song-recognition fingerprinting.

Recognition apps such as Shazam identify a track by hashing peaks in a
spectrogram, anchored on ABSOLUTE frequencies plus the TIME GAPS between those
peaks. That fingerprint is not invariant to:

  * a pitch / key shift   -> the absolute frequencies move, so the hash
                             anchors change;
  * a tempo change        -> the time gaps between peaks move, so the
                             pairwise hashes change.

Shift both the right amount and the matcher gets a different fingerprint and
fails to identify the track -- while your ear still reads it as the same song.

This tool uses ONE method only: a clean UNIFORM pitch + tempo shift (time-stretch,
then pitch-shift the whole track). No detune, no warble, no scrambling, no
varispeed. It is the only approach verified to stay sounding like the original
while the matcher could not identify it.

The near-identical setting found on a real track (verified clean on every probe
window; the track only drops 0.5 st and sits ~18% shorter):
    pitch = -0.5 semitones   (half a semitone flat, barely audible)
    tempo = 1.22             (22% faster)

Usage:
    python shazam_proof.py input.mp3 output.wav --pitch -0.5 --tempo 1.22
    python shazam_proof.py input.mp3 output.wav                # defaults to that
    python shazam_proof.py input.mp3 output.wav --check        # + fingerprint test
    python shazam_proof.py input.mp3 output.wav --auto         # closed-loop fit

Notes:
  * --auto probes the WHOLE song against the real recogniser (Shazam via SongRec)
    and, starting from the near-same setting, nudges the shift until it can't
    identify any part. It only ever uses the uniform pitch/tempo method.
  * A human can still name the song at -0.5 st / +22% easily.
  * This only makes the FILE unrecognisable to a matcher. If the source is a
    track you don't own, don't re-publish it as if it were yours and don't
    use this to smuggle copyright-cleared content past a platform's filter
    for distribution. It's a personal / fun experiment, not a publishing tool.
"""

import argparse
import os
import shutil
import sys
import tempfile

import numpy as np
import soundfile as sf
import librosa

import fingerprint_oracle as fo
import songrec_tester as st

# The verified near-identical setting. tempo_factor > 1 = faster, < 1 = slower;
# pitch < 0 = down in key. A half-semitone flat keeps the song sounding like
# itself while still giving the matcher a different fingerprint.
DEFAULT_PITCH = -0.5
DEFAULT_TEMPO = 1.22

# How many points across the song are probed for the acceptance test. Shazam
# only needs a 12s clip and detects weak shifts on *some* parts, so a handful of
# spots is not enough -- spread probes over the whole track and require every one
# to be clean.
SPREAD_WINDOWS = 8


def _process_audio(y, sr, pitch_semi, tempo):
    """Apply a uniform pitch + tempo shift to an already-loaded signal.

    librosa's effects are mono, so multi-channel audio is handled per channel.
    Returns the processed (samples, channels) array (mono stays (samples,)).
    """
    if y.ndim == 1:
        channels = [y]
    else:  # (channels, samples)
        channels = [y[ch] for ch in range(y.shape[0])]

    out_channels = []
    for mono in channels:
        # Time-stretch first (keeps pitch, changes duration), then pitch-shift
        # (keeps tempo, changes key). Order barely matters; this reads clearest.
        stretched = librosa.effects.time_stretch(mono, rate=tempo)
        shifted = librosa.effects.pitch_shift(
            stretched, sr=sr, n_steps=pitch_semi
        )
        out_channels.append(shifted)

    if len(channels) > 1:
        return np.stack(out_channels, axis=0).T  # back to (samples, channels)
    return out_channels[0]


def modify(input_path, output_path, pitch_semi=DEFAULT_PITCH, tempo=DEFAULT_TEMPO):
    """Load a file, apply the uniform shift, and write the result.

    Returns a dict with the processing info.
    """
    y, sr = librosa.load(input_path, sr=None, mono=False)
    out = _process_audio(y, sr, pitch_semi, tempo)
    sf.write(output_path, out, sr)

    return {
        "sr": sr,
        "channels": 1 if out.ndim == 1 else out.shape[1],
        "duration": out.shape[0] / sr,
        "pitch": pitch_semi,
        "tempo": tempo,
    }


def _window_points(duration, n=SPREAD_WINDOWS, clip=st._CLIP_SECONDS):
    """Spread n probe windows evenly across the song, staying inside the file.

    A track is only "not recognised" if a recogniser fails on every part, so we
    probe many evenly-spaced spots rather than trusting one or two clips.
    """
    if duration <= clip:
        return [0.0]
    usable = duration - clip
    if n <= 1:
        return [round(usable * 0.5, 1)]
    # Skip the very start/end (often silence or a fadeout) and spread evenly.
    return [round((0.05 + 0.90 * (i / (n - 1))) * usable, 1) for i in range(n)]


# Candidate uniform (pitch, tempo) shifts, mild -> stronger. Only ever uses the
# single uniform pitch/tempo method (no detune / warble / scramble), so the
# output always sounds like the same song; escalating only makes it faster and/or
# a touch flatter if a lower setting still matches.
UNDETECTABLE_LADDER = [
    (DEFAULT_PITCH, DEFAULT_TEMPO),  # the verified near-identical setting
    (-0.5, 1.25),
    (-0.5, 1.28),
    (-1.0, 1.25),
    (-1.0, 1.30),
]


def find_undetectable(input_path, output_path, windows=None, progress=None):
    """Closed-loop: process -> real-recognise -> escalate until undetectable.

    Tries each ladder shift in order. For each it renders the candidate IN FULL
    and asks the REAL recogniser (Shazam via SongRec) whether any probe window on
    the finished file still identifies the track. If a shift matches anywhere it
    is rejected (and we escalate to a stronger one); if it is clean everywhere it
    is accepted. Candidate probe windows are computed on the finished duration,
    which is what actually determines acceptance and avoids the clip-vs-full-file
    window mismatch that used to cause false escalations.

    Nothing is written to `output_path` until a shift clears every window, so
    your folder never sees a half-baked / still-detectable file mid-run. The
    accepted result is moved there at the very end.

    progress(msg) is called between steps so a GUI can show what it's doing.

    Returns a dict: {"pitch", "tempo", "counts", "windows", "info"}.
    counts is the per-window match list, or None if nothing on the ladder
    cleared (in which case the strongest shift is still written as a best
    effort, and the caller should treat the result as NOT guaranteed).
    """
    winner = None  # (temp_path, pitch, tempo, counts, info, windows)
    # Whether the server ever returned a definite (non-None) answer. If it never
    # did, the search didn't actually test anything -- reporting that beats
    # writing an unverified "strongest shift" and pretending it worked.
    got_answer = False

    # Surface back-off waits to the UI (the recogniser uses these to pause when
    # Shazam rate-limits, then resume) and always clear it afterwards.
    if progress:
        st.set_wait_callback(progress)
    try:
        # All candidate renders go into a temp dir so they never show up in the
        # user's folder; only the accepted one is moved out at the end.
        with tempfile.TemporaryDirectory() as td:
            for pitch, tempo in UNDETECTABLE_LADDER:
                pct = (tempo - 1.0) * 100.0
                if progress:
                    progress("testing uniform shift %+.1f st / +%.0f%%..." % (
                        pitch, pct))

                # Render the candidate in full and confirm every window on the
                # FINISHED file, so the acceptance test matches what the user
                # hears. Probe windows are computed on the finished duration.
                cand_path = os.path.join(td, "candidate.wav")
                info = modify(input_path, cand_path, pitch, tempo)
                cand_windows = _window_points(st.duration(cand_path))
                ok, counts = st.real_undetectable(cand_path, cand_windows)
                if counts is not None and None not in counts:
                    got_answer = True
                    if ok:
                        winner = (cand_path, pitch, tempo, counts, info,
                                  cand_windows)
                        break
                # Inconclusive (throttle/network): escalate and keep trying;
                # we never accept a shift we couldn't verify.

            if winner:
                tmp, pitch, tempo, counts, info, w = winner
                shutil.move(tmp, output_path)
                return {"pitch": pitch, "tempo": tempo, "counts": counts,
                        "windows": w, "info": info}
    finally:
        st.set_wait_callback(None)

    # If nothing on the ladder cleared, fall back to the strongest shift -- but
    # only if the recogniser actually responded during the search. If it never
    # returned a definitive answer (even after back-off retries), don't write a
    # file we have no evidence about; tell the caller plainly instead.
    pitch, tempo = UNDETECTABLE_LADDER[-1]
    if not got_answer:
        raise RuntimeError(
            "The recogniser never returned a usable answer even after pausing "
            "and retrying (Shazam is still rate-limiting / the network can't "
            "reach it). No file was written. Try again in a few minutes."
        )
    if progress:
        progress("strongest shift still not fully clean; using it anyway")
    info = modify(input_path, output_path, pitch, tempo)
    fallback_windows = _window_points(st.duration(output_path))
    return {"pitch": pitch, "tempo": tempo, "counts": None,
            "windows": fallback_windows, "info": info}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Pitch/tempo shift an audio file so a fingerprint matcher "
                    "(e.g. Shazam) can't identify it but a human still can.",
    )
    parser.add_argument("input", help="input audio file (mp3/wav/ogg/...)")
    parser.add_argument("output", help="output audio file path")
    parser.add_argument(
        "--pitch", type=float, default=None,
        help="semitone shift; default -0.5 (half a semitone flat)",
    )
    parser.add_argument(
        "--tempo", type=float, default=None,
        help="tempo factor; default 1.22 (22% faster)",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="closed-loop: try the near-same shift and escalate only as far as "
             "needed, running the real recogniser against the whole song until "
             "it can't identify any part. Overrides --pitch/--tempo.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="run the local fingerprint oracle and report whether the output "
             "still matches the input (i.e. would be recognised)",
    )
    parser.add_argument(
        "--songrec", action="store_true",
        help="run SongRec's own signature generator and report whether the "
             "output still matches the input under SongRec's algorithm",
    )
    args = parser.parse_args(argv)

    if args.auto:
        result = find_undetectable(args.input, args.output)
        pitch = result["pitch"]
        tempo = result["tempo"]
        info = result["info"]
        counts = result["counts"]
        tempo_pct = (tempo - 1.0) * 100.0
        print(
            f"Wrote {args.output}\n"
            f"  shift: uniform {pitch:+g} st / "
            f"{tempo_pct:+.1f}%\n"
            f"  sr:    {info['sr']} Hz\n"
            f"  new dur: {info['duration']:.1f}s"
        )
        if counts is not None:
            clean = all(c == 0 for c in counts)
            print(f"  real Shazam: {'clean' if clean else 'not fully clean'} "
                  f"-- {len(counts)} windows, matches {counts}")
        return

    pitch = args.pitch if args.pitch is not None else DEFAULT_PITCH
    tempo = args.tempo if args.tempo is not None else DEFAULT_TEMPO
    info = modify(args.input, args.output, pitch, tempo)
    tempo_pct = (info["tempo"] - 1.0) * 100.0
    print(
        f"Wrote {args.output}\n"
        f"  shift: uniform {pitch:+g} st / "
        f"{tempo_pct:+.1f}%\n"
        f"  sr:    {info['sr']} Hz\n"
        f"  new dur: {info['duration']:.1f}s"
    )

    if args.check:
        recognised, score = fo.is_recognised(args.input, args.output)
        verdict = "STILL MATCHES" if recognised else "NO LONGER MATCHES (good)"
        print(f"  fingerprint: {verdict} (score {score:.3f}, needs >0.15 to be "
              f"recognised)")

    if args.songrec:
        recognised, score = st.is_recognised(args.input, args.output)
        verdict = "STILL MATCHES" if recognised else "NO LONGER MATCHES (good)"
        print(f"  songrec:     {verdict} (score {score:.3f}, needs >0.15 to be "
              f"recognised)")


if __name__ == "__main__":
    main()
