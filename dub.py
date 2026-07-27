"""dub.py — timestamp-synced translated voiceover for a YouTube video.

Why this exists
---------------
`app.py` flattens the transcript into one string (`" ".join(...)`), which throws
away every caption timestamp. The read-aloud mp3 is then a continuous read with
no relationship to when things are said on screen — so it never lines up.

This script keeps the timing skeleton the captions already give us:
  1. fetch the transcript WITH per-cue start/duration (never flatten),
  2. regroup fragmented cues into whole sentences, each with a [start,end] window,
  3. translate per sentence (good context AND a known time slot for each line),
  4. TTS each translated sentence into its own clip (edge-tts),
  5. FIT each clip into its window: if it runs long, speed it up (ffmpeg atempo,
     pitch-preserving, capped so it stays natural); if short, the trailing pause
     absorbs the slack. Drift never accumulates because we re-anchor at each
     sentence's real caption time whenever there's a pause to recover into.
  6. lay every clip onto a silent timeline at its real offset -> one dubbed .mp3
     that lines up with the video end to end. Optionally mux it over a video file.

Deliberately deterministic and dependency-light: reuses the deps app.py already
has (edge-tts, deep-translator, youtube-transcript-api) plus the bundled ffmpeg.

Usage
-----
  python dub.py <youtube url or id> --lang hi
  python dub.py <url> --lang es --video clip.mp4 --out dubbed.mp4
  python dub.py <url> --lang hi --max-speed 1.6 --keep-original 0.15

Run inside the venv so the deps resolve:  .venv\\Scripts\\python.exe dub.py ...
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

# Reuse the language/voice tables app.py already curates so the two stay in sync.
from app import LANGUAGES, VOICES, VOICE_CHOICES, yt_id
import hinglish     # optional Hinglish restyle (local Gemma via Ollama)
import idioms       # English idiom -> Hindi idiom, deterministic (EN->HI only)
import transcribe   # Whisper transcription (the trustworthy transcript source)

# --- locate ffmpeg/ffprobe (portable: env var -> local folder -> PATH) -------
_HERE = os.path.dirname(os.path.abspath(__file__))
def _ff_dirs() -> list[str]:
    """Places to look for ffmpeg, most specific first."""
    dirs = []
    env = os.environ.get("FFMPEG_DIR")          # explicit override
    if env:
        dirs.append(env)
    dirs.append(os.path.join(_HERE, "ffmpeg", "bin"))   # drop ffmpeg here to ship it
    dirs.append(r"D:\Toolkit\ffmpeg\bin")               # Sage05's bundled copy
    return dirs

def _tool(name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    for d in _ff_dirs():
        p = os.path.join(d, exe)
        if os.path.exists(p):
            return p
    found = shutil.which(name)                   # anywhere on PATH
    if found:
        return found
    sys.exit(f"Couldn't find {name}. Install ffmpeg and add it to PATH, put the "
             f"binaries in {os.path.join(_HERE, 'ffmpeg', 'bin')}, or set FFMPEG_DIR.")
FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")
FFMPEG_DIR = os.path.dirname(FFMPEG)             # for yt-dlp's merge step

# Audio format every intermediate clip is normalised to, so ffmpeg's concat
# demuxer can stitch them without re-encoding surprises.
SR, AR_ARGS = 24000, ["-ac", "1", "-ar", "24000"]


class DubError(Exception):
    """A failure worth showing the person who asked for the dub (bad link, no
    captions, silent audio...). Raised instead of sys.exit so the pipeline can be
    called from a long-running process — the web GUI — without killing it."""


# --- 1. fetch the transcript WITH timings ------------------------------------
_EN_CODES = ["en", "en-US", "en-GB", "en-IN"]

def _cue_of(d) -> dict:
    """Normalise a transcript item (dict on the classic API, object on the newer one)."""
    if isinstance(d, dict):
        return {"text": d["text"], "start": d["start"], "dur": d["duration"]}
    return {"text": d.text, "start": d.start, "dur": d.duration}


def fetch_cues(vid: str) -> tuple[list[dict], bool | None]:
    """Return (cues, is_generated). Timings intact, unlike app.py.

    Prefers a *human-made* English track when one exists, and reports whether the
    captions we ended up using are auto-generated — so the caller can warn that
    they're the error-prone kind (that's the whole reason Whisper is the default).
    `is_generated` is True/False when known, else None.
    """
    from youtube_transcript_api import YouTubeTranscriptApi
    try:   # transcript-LIST API lets us tell manual from auto and prefer manual
        tl = (YouTubeTranscriptApi.list_transcripts(vid)
              if hasattr(YouTubeTranscriptApi, "list_transcripts")
              else YouTubeTranscriptApi().list(vid))
        try:
            t, gen = tl.find_manually_created_transcript(_EN_CODES), False
        except Exception:
            t = tl.find_transcript(_EN_CODES)
            gen = bool(getattr(t, "is_generated", True))
        return [_cue_of(d) for d in t.fetch()], gen
    except Exception:
        pass
    # Fallback: simplest API, generation unknown.
    if hasattr(YouTubeTranscriptApi, "get_transcript"):        # classic API
        return [_cue_of(d) for d in YouTubeTranscriptApi.get_transcript(vid)], None
    return [_cue_of(d) for d in YouTubeTranscriptApi().fetch(vid)], None


# --- 2. regroup cues into sentence-sized chunks with a time window -----------
_END = re.compile(r"[.!?।。？！]['\")\]]?\s*$")   # sentence enders (multi-script)

# A sentence boundary anywhere in a body of text (ender + closing quote + space).
_SENT_SPLIT = re.compile(r"""[.!?।。？！]['")\]]*(?:\s+|$)""")

# Where it's acceptable to break a sentence that is simply too long to fit one
# window. Clause boundaries only — never mid-phrase, and never mid-word.
_CLAUSE_SPLIT = re.compile(r"(?:,|;|:|\s—|\s–)\s+|\s+(?:and|but|because|so|which|"
                           r"that|when|where|while|if|although)\s+")


def _char_times(cues: list[dict]) -> tuple[str, list[tuple[int, int, float, float]]]:
    """Join every cue into one string, remembering which time each character
    belongs to. Times inside a cue are interpolated across its characters — cues
    are seconds long, so that lands a sentence boundary within a fraction of a
    second of where it was actually spoken."""
    parts, spans, pos = [], [], 0
    for c in cues:
        text = " ".join(c["text"].split())              # normalise whitespace
        if not text:
            continue
        if parts:
            parts.append(" ")
            pos += 1
        spans.append((pos, pos + len(text), c["start"], c["start"] + c["dur"]))
        parts.append(text)
        pos += len(text)
    return "".join(parts), spans


def _time_at(spans, index: int, end: bool = False) -> float:
    """Time of the character at `index`, interpolated within its cue."""
    for lo, hi, t0, t1 in spans:
        if lo <= index < hi:
            frac = (index - lo) / max(hi - lo, 1)
            return t0 + (t1 - t0) * frac
        if index < lo:                                  # landed in a joining space
            return t0
    return spans[-1][3] if spans else 0.0


def _split_long(text: str, lo: int, hi: int, spans, max_secs: float) -> list[tuple[int, int]]:
    """Break one over-long sentence at the clause boundary nearest its middle,
    recursively, until each piece fits — or until there's nowhere left to break,
    in which case it stays whole rather than being cut mid-phrase."""
    if _time_at(spans, hi - 1, True) - _time_at(spans, lo) <= max_secs:
        return [(lo, hi)]
    mid = (lo + hi) // 2
    best = None
    for m in _CLAUSE_SPLIT.finditer(text, lo, hi):
        if lo + 12 < m.end() < hi - 12:                 # don't strand a fragment
            if best is None or abs(m.end() - mid) < abs(best - mid):
                best = m.end()
    if best is None:
        return [(lo, hi)]
    return (_split_long(text, lo, best, spans, max_secs)
            + _split_long(text, best, hi, spans, max_secs))


def group_sentences(cues: list[dict],
                    max_secs: float = 14.0, gap: float = 0.8) -> list[dict]:
    """Turn cues into WHOLE sentences, each with a time window.

    Why it works this way: a translated line is only as good as the sentence it
    sits in. Cutting "I've been blown away | by the whole thing" across two lines
    makes the first half translate literally (someone physically blown away), and
    that is the exact fault we removed at the transcript source by using Whisper
    instead of auto-captions. So this never closes a line mid-sentence.

    The text is stitched back into one string (remembering each character's time),
    split on real sentence boundaries, and only then — if a single sentence still
    runs longer than `max_secs` — broken at a clause boundary near its middle.

    Auto-captions carry no punctuation at all, so if the transcript has almost no
    sentence enders we fall back to the older time-and-pause chunking, which is
    the right behaviour for that input.
    """
    cues = [c for c in cues if c.get("text", "").strip()]
    if not cues:
        return []

    text, spans = _char_times(cues)
    bounds, start = [], 0
    for m in _SENT_SPLIT.finditer(text):
        bounds.append((start, m.end()))
        start = m.end()
    if start < len(text):
        bounds.append((start, len(text)))

    # Roughly one sentence per 8 seconds is normal speech; far fewer than that
    # means the transcript is unpunctuated (auto-captions) and splitting on
    # enders would produce a handful of enormous lines.
    total = _time_at(spans, len(text) - 1, True) - _time_at(spans, 0)
    if len(bounds) < max(2, total / 30):
        return _group_by_time(cues, max_secs, gap)

    groups: list[dict] = []
    for lo, hi in bounds:
        for a, b in _split_long(text, lo, hi, spans, max_secs):
            chunk = text[a:b].strip()
            if chunk:
                groups.append({"text": chunk,
                               "start": _time_at(spans, a),
                               "end": _time_at(spans, b - 1, True)})
    return groups


def _group_by_time(cues: list[dict], max_secs: float, gap: float) -> list[dict]:
    """The original chunker, kept for punctuation-free transcripts (auto-captions):
    close a group on a sentence ender, a real pause, or once it has run long."""
    groups: list[dict] = []
    buf, start, last_end = [], None, None
    for c in cues:
        text = c["text"].replace("\n", " ").strip()
        if not text:
            continue
        c_start, c_end = c["start"], c["start"] + c["dur"]
        if start is None:
            start = c_start
        # A big silence before this cue -> the previous sentence has ended.
        if last_end is not None and c_start - last_end > gap and buf:
            groups.append({"text": " ".join(buf), "start": start, "end": last_end})
            buf, start = [], c_start
        buf.append(text)
        last_end = c_end
        joined = " ".join(buf)
        if _END.search(joined) or (last_end - start) >= max_secs:
            groups.append({"text": joined, "start": start, "end": last_end})
            buf, start = [], None
    if buf:
        groups.append({"text": " ".join(buf), "start": start, "end": last_end})
    return groups


# --- 3. translate each group (threaded; keeps sentence context) --------------
def translate_groups(groups: list[dict], target: str) -> int:
    """Translate every group's text into `target`. Returns how many idioms were
    replaced, so the caller can report it.

    For Hindi this is not a plain pass-through to Google. English idioms are
    detected in the ENGLISH first and masked, so the translator never gets the
    chance to render them literally ("the blood run from their face" as actual
    bleeding); the Hindi idiom is spliced into the slot afterwards. See idioms.py
    for why masking beats patching the Hindi output.

    Detection happens once, here, and the resulting tags are stored on the group
    as "idioms". Later stages read that tag rather than re-detecting: it is how
    the Hinglish guard below knows a span was substituted on purpose, and it is
    what a transcript view would show.
    """
    from deep_translator import GoogleTranslator

    # Idioms are an EN->HI asset. Other targets take the plain path unchanged.
    data = idioms.load() if target == "hi" else None
    use_idioms = bool(data and len(data))

    def one(g: dict) -> tuple[str, list]:
        # A fresh translator per call keeps the threads independent.
        def tr(s: str) -> str:
            return GoogleTranslator(source="auto", target=target).translate(s) or s

        text = g["text"]
        if not use_idioms:
            return tr(text), []
        masked, marks = data.mark(text)
        if not marks:
            return tr(text), []
        out, lost = idioms.splice(tr(masked), marks)
        if lost:
            # A placeholder didn't survive, so the sentence would carry a hole
            # where its idiom belongs. Re-translate the original instead: an
            # over-literal line is recoverable by ear, a missing clause is not.
            return tr(text), []
        return out, marks

    with ThreadPoolExecutor(max_workers=8) as pool:
        out = list(pool.map(one, groups))
    for g, (t, marks) in zip(groups, out):
        g["tr"] = t
        if marks:
            g["idioms"] = marks
    return sum(len(m) for _, m in out)


# --- 4. TTS each translated group into an mp3 (edge-tts, concurrent) ---------
async def _synth_all(groups: list[dict], voice: str, workdir: str,
                     on_one=None, only: list[int] | None = None,
                     rates: dict[int, str] | None = None) -> None:
    """TTS every group (or just the indices in `only`).

    `on_one(done, total)` fires as each line lands, so a caller with a progress bar
    (the web GUI) can show movement during the slowest step.

    `rates` maps a group index to an edge-tts rate string like "+25%". That asks the
    ENGINE to speak faster, which is a different thing from speeding the finished
    audio up afterwards — see `plan_native_rates`.
    """
    import edge_tts
    sem = asyncio.Semaphore(6)   # don't hammer Microsoft on long videos
    idxs = list(range(len(groups))) if only is None else list(only)
    done = 0

    async def one(i: int, text: str) -> None:
        nonlocal done
        rate = (rates or {}).get(i, "+0%")
        async with sem:
            buf = bytearray()
            async for part in edge_tts.Communicate(text, voice, rate=rate).stream():
                if part["type"] == "audio":
                    buf.extend(part["data"])
        with open(os.path.join(workdir, f"raw{i}.mp3"), "wb") as f:
            f.write(bytes(buf))
        done += 1
        if on_one:
            on_one(done, len(idxs))

    await asyncio.gather(*(one(i, groups[i]["tr"]) for i in idxs))


def plan_native_rates(groups: list[dict], workdir: str, cap_pct: int
                      ) -> dict[int, str]:
    """Which lines should be RE-SPOKEN faster, and how much faster.

    Why this exists. A translated line is usually longer than the slot it has to
    fit — measured on a 19-minute talk, 196 of 311 Hindi lines overran, because
    Google renders formal written Hindi and formal Hindi is wordier than speech.
    `build_timeline` fixes that with ffmpeg `atempo`, which time-compresses audio
    that has already been spoken. It preserves pitch, but the pauses, stresses and
    breaths were all placed for the slower reading, so it sounds like a recording
    played fast.

    edge-tts can instead be asked to speak the line faster in the first place, and
    it re-synthesises with prosody appropriate to that speed: brisk speech rather
    than a sped-up tape. So do as much of the compression as possible in the engine
    and leave only the remainder to `atempo`.

    Measured on that same talk: 71 of the overrunning lines needed less than 1.25x,
    i.e. 40% of the problem is inside a range the engine can simply speak.

    The budget here is the IDEAL slot (this line's start to the next line's start).
    `build_timeline` uses a cursor-adjusted start, so the two agree whenever lines
    fit, which is the case this is trying to bring about. Over-shooting is safe:
    a clip shorter than its window is not slowed down, the trailing pause absorbs
    it, and sync is unaffected — `build_timeline` remains the thing that guarantees
    the fit. This only changes HOW the line got short.
    """
    rates: dict[int, str] = {}
    n = len(groups)
    for i, g in enumerate(groups):
        raw = os.path.join(workdir, f"raw{i}.mp3")
        if not os.path.exists(raw):
            continue
        clip_len = _dur(raw)
        nxt = groups[i + 1]["start"] if i + 1 < n else g["start"] + clip_len
        budget = max(nxt - g["start"], 0.01)
        if clip_len <= budget * 1.02:        # already fits; leave it alone
            continue
        # Speak it fast enough to fit, but never past the cap — past roughly +40%
        # the voice stops sounding like a person and atempo is no worse.
        needed_pct = (clip_len / budget - 1.0) * 100.0
        pct = int(min(needed_pct, cap_pct))
        if pct >= 5:                         # under 5% is not worth a second call
            rates[i] = f"+{pct}%"
    return rates


# --- ffmpeg helpers ----------------------------------------------------------
def _run(args: list[str]) -> None:
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True)

def _dur(path: str) -> float:
    out = subprocess.check_output(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path]).decode().strip()
    try:
        return float(out)
    except ValueError:
        return 0.0

def _to_wav(src: str, dst: str, tempo: float = 1.0) -> None:
    filt = f"aresample={SR}" if tempo == 1.0 else f"atempo={tempo:.4f},aresample={SR}"
    _run(["-i", src, "-filter:a", filt, *AR_ARGS, dst])

def _silence(seconds: float, dst: str) -> None:
    _run(["-f", "lavfi", "-t", f"{max(seconds,0):.3f}",
          "-i", f"anullsrc=r={SR}:cl=mono", *AR_ARGS, dst])


def _concat(pieces: list[str], dst: str, workdir: str, tag: str) -> None:
    """Stitch a list of same-format clips with the ffmpeg concat demuxer."""
    listfile = os.path.join(workdir, f"list_{tag}.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for p in pieces:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")
    _run(["-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", dst])


# --- retime mode: keep audio near natural speed, stretch the VIDEO instead ---
def _video_info(path: str) -> tuple[str, int, int]:
    """Return (fps as a string ffmpeg accepts, width, height).

    Parsed by key (not column order) — ffprobe emits fields in the stream's own
    order, which isn't the order we ask for."""
    out = subprocess.check_output(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate,width,height",
         "-of", "default=noprint_wrappers=1", path]).decode()
    info = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    return info["r_frame_rate"], int(info["width"]), int(info["height"])


def _vseg(src: str, start: float, dur: float, factor: float, fps: str,
          w: int, h: int, dst: str, smooth: bool) -> float:
    """Cut [start, start+dur] from the video, retime by `factor` (>1 slows it),
    re-encode to a uniform format so segments concat cleanly. Returns real dur."""
    vf = [f"setpts=(PTS-STARTPTS)*{factor:.6f}"]
    if smooth and factor > 1.01:            # optional motion interpolation on slows
        vf.append(f"minterpolate=fps={fps}:mi_mode=mci")
    vf.append(f"scale={w}:{h}")
    _run(["-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}",
          "-an", "-vf", ",".join(vf), "-r", fps, "-pix_fmt", "yuv420p",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", dst])
    return _dur(dst)


def _fit_audio(raw: str, target: float, dst: str) -> None:
    """Make `raw` exactly `target` seconds: speed up if longer, pad if shorter.
    Never slows speech (that sounds unnatural) — short audio is padded instead."""
    a = _dur(raw)
    if a > target + 0.01:
        tempo = a / target
        _run(["-i", raw, "-filter:a", f"atempo={tempo:.4f},aresample={SR},apad",
              *AR_ARGS, "-t", f"{target:.3f}", dst])
    else:
        _run(["-i", raw, "-filter:a", f"aresample={SR},apad", *AR_ARGS,
              "-t", f"{target:.3f}", dst])


def _split_ratio(R: float, max_audio: float, max_video: float) -> tuple[float, float]:
    """Split an expansion ratio R>1 across audio speed-up and video slow-down.

    Aim for an even split (sqrt on each) so neither is pushed hard; respect each
    cap; if both caps are hit and it still doesn't fit, spill the remainder onto
    the audio (fast speech guarantees the fit; the video bound is never exceeded,
    which is what protects on-camera faces)."""
    v = min(R ** 0.5, max_video)
    a = R / v
    if a > max_audio:                       # audio would be unnatural -> lean on video
        a = max_audio
        v = min(R / a, max_video)
        a = R / v                           # any residual goes back to audio (may exceed cap)
    return a, v


def build_retimed(groups: list[dict], workdir: str, video: str,
                  max_audio: float, max_video: float, smooth: bool,
                  total: float) -> tuple[str, str, list[dict]]:
    """Build a retimed (silent) video + matching audio track.

    Each sentence's video window is stretched to help the translated line fit at
    a natural voice, splitting the mismatch between audio and video. Alignment is
    exact because every audio segment is fit to the *measured* length of its
    rendered video segment. Returns (video_path, audio_path, warnings)."""
    fps, w, h = _video_info(video)
    vsegs: list[str] = []
    asegs: list[str] = []
    warnings: list[dict] = []
    n = len(groups)

    def add(vpath: str, apath: str):
        vsegs.append(vpath)
        asegs.append(apath)

    # Lead-in before the first caption: play the video untouched under silence.
    if groups and groups[0]["start"] > 0.05:
        d = _vseg(video, 0.0, groups[0]["start"], 1.0, fps, w, h,
                  os.path.join(workdir, "lead_v.mp4"), False)
        sil = os.path.join(workdir, "lead_a.wav"); _silence(d, sil)
        add(os.path.join(workdir, "lead_v.mp4"), sil)

    for i, g in enumerate(groups):
        raw = os.path.join(workdir, f"raw{i}.mp3")
        A = _dur(raw)                                   # natural translated length
        sp = max(g["end"] - g["start"], 0.05)           # on-screen speech window
        R = A / sp

        if R > 1.0:
            a_fac, v_fac = _split_ratio(R, max_audio, max_video)
            if a_fac > max_audio + 1e-3:
                warnings.append({"start": g["start"], "audio": a_fac,
                                 "video": v_fac, "text": g["tr"]})
        else:
            v_fac = 1.0                                 # audio shorter -> don't touch video

        # Retime the speech video segment, then fit its audio to what rendered.
        vpath = os.path.join(workdir, f"seg_v{i}.mp4")
        vd = _vseg(video, g["start"], sp, v_fac, fps, w, h, vpath, smooth)
        apath = os.path.join(workdir, f"seg_a{i}.wav")
        _fit_audio(raw, vd, apath)
        add(vpath, apath)

        # Pause after this sentence (video untouched, silence under it).
        nxt = groups[i + 1]["start"] if i + 1 < n else total
        pause = (nxt if nxt else g["end"]) - g["end"]
        if pause > 0.05:
            pv = os.path.join(workdir, f"seg_pv{i}.mp4")
            pd = _vseg(video, g["end"], pause, 1.0, fps, w, h, pv, False)
            if pd > 0.02:                               # skip if it rendered empty
                pa = os.path.join(workdir, f"seg_pa{i}.wav"); _silence(pd, pa)
                add(pv, pa)

    out_v = os.path.join(workdir, "retimed_video.mp4")
    out_a = os.path.join(workdir, "retimed_audio.wav")
    _concat(vsegs, out_v, workdir, "v")
    _concat(asegs, out_a, workdir, "a")
    return out_v, out_a, warnings


# --- 5+6. fit every clip into its window and lay them on the timeline --------
def build_timeline(groups: list[dict], workdir: str, max_speed: float,
                   hard_max: float, allow_drift: bool,
                   total: float | None) -> tuple[str, list[dict]]:
    """Produce one wav where each clip is fit into its caption's time window.

    The point of the tool: when a translated line is longer than the original
    slot (English->Hindi expands, and vice-versa), it must still FIT the window
    so the dub stays locked to the video. So each line is sped up by exactly the
    factor needed to fit — `atempo` (pitch-preserving, "fast speech", not
    chipmunk) — up to `hard_max`. Because every line fits, the cursor tracks the
    real caption times and there is essentially no drift.

    `max_speed` is the *natural* threshold: we only warn when a line has to go
    faster than that, so you can see which lines got hurried. Lines shorter than
    their window aren't slowed — the trailing pause absorbs the slack.

    `--allow-drift` restores the older behaviour: cap at `max_speed` for a more
    natural voice and let long lines overrun (re-anchoring at the next pause).
    """
    pieces: list[str] = []   # ordered wav fragments (clips + silences) to concat
    cursor = 0.0
    n = len(groups)
    warnings: list[dict] = []
    for i, g in enumerate(groups):
        raw = os.path.join(workdir, f"raw{i}.mp3")
        clip_len = _dur(raw)
        actual_start = max(cursor, g["start"])
        # Budget = time until the next sentence ideally starts (absorbs the pause).
        nxt = groups[i + 1]["start"] if i + 1 < n else actual_start + clip_len
        budget = max(nxt - actual_start, 0.01)

        tempo = 1.0
        if clip_len > budget:                       # too long -> speed up to fit
            needed = clip_len / budget
            if allow_drift:                         # old mode: stay natural, drift
                tempo = min(needed, max_speed)
            else:                                   # fit mode: fit up to the ceiling
                tempo = min(needed, hard_max)
            if needed > max_speed + 1e-3:           # flag anything past "natural"
                warnings.append({"start": g["start"], "needed": needed,
                                 "used": tempo, "text": g["tr"]})
        wav = os.path.join(workdir, f"clip{i}.wav")
        _to_wav(raw, wav, tempo)
        final_len = _dur(wav)

        gap = actual_start - cursor                 # silence before this clip
        if gap > 0.02:
            sfile = os.path.join(workdir, f"sil{i}.wav")
            _silence(gap, sfile)
            pieces.append(sfile)
        pieces.append(wav)
        cursor = actual_start + final_len

    # Pad the tail so a muxed video keeps full length / trailing silence.
    if total and total > cursor + 0.05:
        tail = os.path.join(workdir, "tail.wav")
        _silence(total - cursor, tail)
        pieces.append(tail)

    listfile = os.path.join(workdir, "list.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for p in pieces:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")
    joined = os.path.join(workdir, "joined.wav")
    _run(["-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", joined])
    return joined, warnings


def mux(video: str, dub_wav: str, out: str, keep_original: float) -> None:
    """Replace (or duck-mix) the video's audio with the dubbed track."""
    if keep_original > 0:
        # Keep the original quietly under the dub (ambience, laughter, music).
        _run(["-i", video, "-i", dub_wav, "-filter_complex",
              f"[0:a]volume={keep_original}[a0];[1:a]volume=1.0[a1];"
              f"[a0][a1]amix=inputs=2:duration=first:dropout_transition=0[a]",
              "-map", "0:v", "-map", "[a]", "-c:v", "copy",
              "-shortest", out])
    else:
        _run(["-i", video, "-i", dub_wav, "-map", "0:v", "-map", "1:a",
              "-c:v", "copy", "-shortest", out])


def download_video(url: str, dst_dir: str) -> str:
    """Fetch the source video with yt-dlp (bundled ffmpeg does the merge)."""
    from yt_dlp import YoutubeDL
    outtmpl = os.path.join(dst_dir, "source.%(ext)s")
    opts = {
        "format": "bv*+ba/b",              # best video+audio, else best single file
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "ffmpeg_location": FFMPEG_DIR,
        "quiet": True, "no_warnings": True, "noprogress": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    path = os.path.join(dst_dir, "source.mp4")
    if not os.path.exists(path):                    # fall back to whatever it wrote
        rd = (info.get("requested_downloads") or [{}])[0]
        path = rd.get("filepath") or path
    if not os.path.exists(path):
        sys.exit("Video download failed (yt-dlp produced no file).")
    return path


def download_audio(url: str, dst_dir: str) -> str:
    """Fetch just the audio track (for Whisper when we don't also need the video)."""
    from yt_dlp import YoutubeDL
    opts = {
        "format": "ba/b",                  # best audio, else best single file
        "outtmpl": os.path.join(dst_dir, "audio.%(ext)s"),
        "ffmpeg_location": FFMPEG_DIR,
        "quiet": True, "no_warnings": True, "noprogress": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    rd = (info.get("requested_downloads") or [{}])[0]
    path = rd.get("filepath")
    if not path or not os.path.exists(path):            # fall back to guessing the ext
        for ext in ("m4a", "webm", "opus", "mp3", "wav"):
            p = os.path.join(dst_dir, f"audio.{ext}")
            if os.path.exists(p):
                path = p
                break
    if not path or not os.path.exists(path):
        sys.exit("Audio download failed (yt-dlp produced no file).")
    return path


_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm")

def _interactive_argv() -> list[str]:
    """Ask the questions when the tool is launched with no arguments (i.e. it was
    double-clicked). Builds the same argv the command line would take."""
    print("=" * 52)
    print("  Video translation - paste a link, get a dubbed video")
    print("=" * 52)
    url = input("\nPaste the YouTube link and press Enter:\n> ").strip()
    if not url:
        print("No link given. Closing.")
        input("\nPress Enter to close.")
        sys.exit(0)
    print("\nLanguage codes:  hi=Hindi  es=Spanish  fr=French  de=German")
    print("                 ar=Arabic  zh-CN=Chinese  ja=Japanese  en=English")
    lang = input("Language code [default hi]:\n> ").strip() or "hi"
    hinglish_argv: list[str] = []
    if lang == "hi":
        print("\nSpeak natural Hinglish (Hindi-English mix, like a real YouTuber)?")
        print("  1) Yes - needs Ollama running on this PC (ollama pull gemma3:4b)")
        print("  2) No  - pure Hindi")
        if (input("Choose 1 or 2 [default 1]:\n> ").strip() or "1") == "1":
            hinglish_argv = ["--hinglish"]
    print("\nTranscript source?")
    print("  1) Whisper           (transcribe the audio ourselves - accurate, recommended)")
    print("  2) YouTube captions  (faster, but auto-captions are often wrong)")
    source = "captions" if (input("Choose 1 or 2 [default 1]:\n> ").strip() or "1") == "2" else "whisper"
    print("\nWhat kind of footage is it?")
    print("  1) Talking head  (a person speaking on camera)")
    print("  2) No faces      (POV / gameplay / screen recording)")
    print("  3) Audio only    (just the dubbed .mp3, no video)")
    mode = input("Choose 1, 2 or 3 [default 2]:\n> ").strip() or "2"
    argv = [url, "--lang", lang, "--source", source] + hinglish_argv
    if mode == "1":
        argv += ["--faces", "--download", "--out", "dubbed.mp4"]
    elif mode == "3":
        argv += ["--out", "dubbed.mp3"]
    else:
        argv += ["--broll", "--download", "--out", "dubbed.mp4"]
    print()
    return argv


# --- the whole pipeline, as one callable -------------------------------------
# The CLI is not the only front door any more: the web GUI in app.py runs the
# same job from a background thread. So the run lives in run_dub() and main() is
# just argument parsing. Progress goes through a callback instead of print(),
# because a browser needs to see the same stages a console does — a dub takes
# minutes, and silence is indistinguishable from a hang.

# Every knob run_dub understands, with the defaults the CLI advertises. options()
# lets a non-CLI caller build a valid job without mirroring argparse's flag list.
DEFAULTS = dict(
    url="", lang="hi", out=None, video=None, download=False,
    max_speed=1.5, hard_max=3.0, allow_drift=False, keep_original=0.0,
    retime=False, max_video=1.25, smooth=False, faces=False, broll=False,
    hinglish=False, hinglish_model=None, source="whisper", whisper_model="small",
    # None = the language's standard voice from VOICES. Any other value must be a
    # voice name from VOICE_CHOICES for that language; see run_dub.
    voice=None,
    # How much of an overrun edge-tts is asked to absorb by speaking faster, before
    # ffmpeg atempo takes the rest. 0 disables it and restores the old behaviour.
    tts_rate_max=40,
)


def options(**over) -> argparse.Namespace:
    """Build a run_dub() options object: the CLI defaults, plus any overrides."""
    unknown = set(over) - set(DEFAULTS)
    if unknown:
        raise DubError(f"unknown option(s): {', '.join(sorted(unknown))}")
    return argparse.Namespace(**{**DEFAULTS, **over})


def _apply_presets(args) -> None:
    """Footage presets set the retime knobs. An explicit --max-video still wins,
    which is why each preset only fires when the value is still the default."""
    if args.faces and args.broll:
        raise DubError("Pick one of --faces / --broll, not both.")
    if args.faces:
        args.retime = True
        if args.max_video == 1.25:
            args.max_video = 1.1
    if args.broll:
        args.retime = True
        args.smooth = True
        if args.max_video == 1.25:
            args.max_video = 1.6


def transcript_of(groups: list[dict]) -> list[dict]:
    """The dub's script as plain JSON-safe rows: what was said, when, and what got
    spoken in its place.

    Separate from the pipeline because the sentences are worth reading on their
    own — this is what lets the GUI show a real synced transcript instead of the
    flat unsynced block that used to live in the deleted caption tab.
    """
    return [{"start": round(g["start"], 2),
             "end": round(g["end"], 2),
             "text": g["text"],
             "tr": g.get("tr", ""),
             # Idioms are surfaced, not hidden: a substituted span is the one part
             # of the output that is NOT a translation of the words above it, so a
             # reader comparing the two columns deserves to know why they differ.
             "idioms": [{"id": m.entry_id, "en": m.en, "hi": m.hi}
                        for m in g.get("idioms", [])]}
            for g in groups]


def run_dub(args, progress=None, on_transcript=None) -> str:
    """Produce the dub described by `args` (see options()); return the output path.

    `progress(pct, message)` is called at each stage — `pct` is a rough 0-100 for
    a progress bar, `message` is the human line the CLI would have printed.
    `on_transcript(rows)` is called once the script is final (translated, restyled)
    and before the voice is generated, so a caller can show the transcript while
    the slow audio steps are still running.
    Raises DubError for anything the person asking can act on.
    """
    def say(pct: int | None, msg: str) -> None:
        if progress:
            progress(pct, msg)

    _apply_presets(args)

    vid = yt_id(args.url)
    if not vid:
        raise DubError("That doesn't look like a YouTube link or id.")
    if args.lang not in dict((c, l) for l, c in LANGUAGES):
        raise DubError(f"Unknown lang '{args.lang}'. Known: {', '.join(c for _, c in LANGUAGES)}")
    if args.hinglish and args.lang != "hi":
        raise DubError("Hinglish is Hindi-only; choose Hindi (or turn Hinglish off).")
    # The dub voice. `VOICES` is one default per language and every one of them is
    # female — that was never a decision, just a default nobody revisited, and until
    # --voice existed there was no supported way to dub in a man's voice at all.
    # `VOICE_CHOICES` is the curated male/female catalogue the read-aloud panel has
    # always used; the dub simply could not reach it.
    #
    # An explicit voice is validated against that catalogue rather than passed
    # straight through: edge-tts rejects an unknown name at synthesis time, which is
    # minutes into a run, after transcription and translation have already been paid
    # for. Falling back to the default instead would be worse still — it would hand
    # back a whole dub in the wrong voice under a green "Done".
    voice = VOICES.get(args.lang, VOICES["en"])
    if args.voice:
        choices = VOICE_CHOICES.get(args.lang, [])
        if args.voice not in [v for _, v in choices]:
            raise DubError(
                f"Unknown voice '{args.voice}' for '{args.lang}'.\nAvailable: "
                + ", ".join(f"{lbl} = {v}" for lbl, v in choices)
                + "\n(Run: dub.py --list-voices)")
        voice = args.voice

    # Decide up front whether we're producing a video (mux) or just audio.
    out = args.out
    want_video = bool(args.video) or args.download or args.retime or (
        out is not None and out.lower().endswith(_VIDEO_EXTS))
    if out is None:
        out = "dubbed.mp4" if want_video else "dubbed.mp3"

    url = f"https://www.youtube.com/watch?v={vid}"

    # Check the Hinglish model answers BEFORE spending minutes on transcription.
    # Finding out at minute six that the feature you ticked can't run is the worst
    # possible time to find out.
    if args.hinglish:
        say(1, "Checking the Hinglish model...")
        try:
            hinglish.preflight(model=args.hinglish_model or "gemma3:4b")
        except hinglish.HinglishUnavailable as e:
            raise DubError(
                f"Hinglish is on, but the local model isn't available.\n{e}\n"
                "Fix it (start Ollama, run 'ollama pull gemma3:4b', or close some "
                # ASCII only: this prints to a console, and cp1252 turns an
                # em-dash into mojibake. It is the first error a new user hits.
                "apps if it's out of memory), or turn Hinglish off to dub in "
                "regular Hindi.") from e

    with tempfile.TemporaryDirectory(prefix="dub_") as workdir:
        # Get the source media up front if we'll mux a video, or if Whisper needs it.
        video_path = args.video
        if want_video and not video_path:
            say(3, "Downloading video...")
            video_path = download_video(url, workdir)

        # Transcript source: Whisper (accurate, default) or YouTube captions (fast).
        if args.source == "whisper":
            media = video_path
            if not media:
                say(6, "Downloading audio...")
                media = download_audio(url, workdir)
            say(10, f"Transcribing with Whisper ({args.whisper_model}; "
                    "the first run loads the model)...")
            # Long audio is transcribed in chunks; report them, because this is the
            # step a long video sits in longest (10 -> 38% of the bar).
            try:
                cues = transcribe.transcribe(
                    media, model_size=args.whisper_model, lang="en",
                    on_progress=lambda d, t: say(10 + int(28 * d / t),
                                                 f"  transcribing part {d}/{t}"))
            except MemoryError as e:
                raise DubError(str(e)) from e     # already a plain-English message
            finally:
                # Hand the memory back before the Hinglish model and ffmpeg want it.
                transcribe.release()
            if not cues:
                raise DubError("Whisper found no speech (silent or music-only audio?).")
        else:
            say(10, "Fetching captions...")
            cues, generated = fetch_cues(vid)
            if not cues:
                raise DubError("This video has no captions. Switch the transcript "
                               "source to Whisper.")
            if generated:
                say(None, "  ! Heads-up: these are AUTO-GENERATED captions (often "
                          "wrong). Whisper is more accurate.")
        groups = group_sentences(cues)
        say(40, f"  {len(cues)} segments -> {len(groups)} sentences")

        # Translate, then (optionally) restyle. Hinglish is a *second pass* over
        # Google's Hindi, never a translation of its own — the model only fixes
        # register and wrong words, which is what keeps it from inventing meaning.
        say(42, "Translating...")
        n_idioms = translate_groups(groups, args.lang)
        if n_idioms:
            say(None, f"  {n_idioms} idiom(s) replaced with Hindi equivalents")
        if args.hinglish:
            say(55, "Restyling to Hinglish (local Gemma)...")
            # Keep the pre-restyle text for any line carrying an idiom. Gemma is
            # measured at 7/7 for leaving spliced idioms alone, but "measured
            # once" isn't "guaranteed", and the idiom was put there deliberately.
            pre_restyle = {i: g["tr"] for i, g in enumerate(groups) if g.get("idioms")}
            try:
                hinglish.apply(
                    groups, model=args.hinglish_model,
                    on_progress=lambda d, t: say(55 + int(8 * d / t),
                                                 f"  Hinglish batch {d}/{t}"))
            except hinglish.HinglishUnavailable as e:
                # Deliberately fatal. Hinglish was asked for; handing back formal
                # Hindi under a green "Done" is a silent downgrade nobody sees.
                raise DubError(
                    f"The Hinglish pass failed part-way through.\n{e}\n"
                    "Nothing was written. Free some memory and run it again, or "
                    "turn Hinglish off to dub in regular Hindi.") from e

            # Guard: if the restyle dropped an idiom we deliberately spliced in,
            # keep the pre-restyle line for that line only. Reverting one line
            # costs a little Hinglish register; losing the idiom costs the
            # meaning, which is the whole reason the dictionary exists.
            reverted = 0
            for i, g in enumerate(groups):
                if g.get("idioms") and idioms.survived(g["tr"], g["idioms"]):
                    g["tr"] = pre_restyle[i]
                    reverted += 1
            if reverted:
                say(None, f"  kept {reverted} line(s) un-restyled to protect their idioms")

        # The script is final here: translated, restyled, idioms spliced. Hand it
        # over before the voice run, which is the longest step — there's no reason
        # to make someone wait on audio to read what the dub is going to say.
        if on_transcript:
            try:
                on_transcript(transcript_of(groups))
            except Exception:
                pass          # a display feature must never take the dub down

        say(65, "Generating voice...")
        # Per-line ticks: TTS is the step where a long video sits longest, so the
        # bar has to keep moving there or the page looks stuck.
        asyncio.run(_synth_all(groups, voice, workdir,
                               on_one=lambda d, t: say(65 + int(13 * d / t),
                                                       f"  voice {d}/{t} lines")))

        # Second pass: re-speak the lines that overran, asking the ENGINE to talk
        # faster rather than compressing finished audio with ffmpeg. Costs one extra
        # TTS call per overrunning line and buys a voice that sounds hurried instead
        # of sped-up. build_timeline still guarantees the fit either way, so if this
        # step is skipped or fails the dub is exactly what it used to be.
        if args.tts_rate_max > 0:
            try:
                rates = plan_native_rates(groups, workdir, args.tts_rate_max)
                if rates:
                    say(78, f"  re-speaking {len(rates)} long line(s) faster "
                            f"(up to +{args.tts_rate_max}%) so ffmpeg has less to squeeze")
                    asyncio.run(_synth_all(groups, voice, workdir,
                                           only=sorted(rates), rates=rates))
            except Exception as e:
                # A quality improvement must never cost the dub. Fall through to the
                # atempo-only path, which is what shipped before this existed.
                say(None, f"  ! couldn't re-speak long lines ({type(e).__name__}); "
                          f"using ffmpeg speed-up only.")

        total = _dur(video_path) if video_path else None

        if args.retime:
            if not video_path:
                raise DubError("Retiming needs the video: use a footage preset that "
                               "downloads it, or pass a local file.")
            say(82, "Retiming video to the voice (this re-encodes; be patient)...")
            rv, ra, warns = build_retimed(
                groups, workdir, video_path, args.max_speed, args.max_video,
                args.smooth, total or 0.0)
            say(94, "Muxing...")
            _run(["-i", rv, "-i", ra, "-map", "0:v", "-map", "1:a",
                  "-c:v", "copy", "-shortest", out])
        else:
            say(82, "Fitting to the timeline...")
            joined, warns = build_timeline(
                groups, workdir, args.max_speed, args.hard_max, args.allow_drift, total)
            if video_path:
                say(94, "Muxing onto the video...")
                mux(video_path, joined, out, args.keep_original)
            else:
                say(94, "Writing the mp3...")
                _run(["-i", joined, "-b:a", "160k", out])

    # The sync trade-off, reported honestly: which lines had to hurry, and how far.
    if warns and args.retime:
        fastest = max(w["audio"] for w in warns)
        say(None, f"Note: {len(warns)} line(s) expanded past both the audio and video caps; "
                  f"audio took the remainder (up to {fastest:.2f}x) to keep the fit. "
                  f"Loosen with --max-video / --max-speed, or --smooth for cleaner slow video.")
    elif warns:
        fastest = max(w["needed"] for w in warns)
        mode = "let it drift" if args.allow_drift else f"sped up (capped at {args.hard_max:g}x)"
        extra = (f" The voice already absorbed up to +{args.tts_rate_max}% of this by speaking "
                 f"faster, so these figures are what was left after that."
                 if args.tts_rate_max > 0 else "")
        say(None, f"Note: {len(warns)} line(s) ran longer than their slot at a natural pace; "
                  f"{mode} to keep sync (fastest needed {fastest:.2f}x).{extra} "
                  f"Tighter sync vs. calmer voice is the --max-speed / --hard-max / --allow-drift trade.")
    say(100, f"Done -> {out}")
    return out


class _ListVoices(argparse.Action):
    """Print the voice catalogue and exit.

    An argparse Action rather than a normal flag because `url` is positional and
    required: `dub.py --list-voices` would otherwise fail for a missing URL before
    it could answer the question. Actions fire during parsing, so this answers and
    exits first.
    """
    def __call__(self, parser, namespace, values, option_string=None):
        for label, code in LANGUAGES:
            choices = VOICE_CHOICES.get(code)
            if not choices:
                continue
            default = VOICES.get(code)
            print(f"\n{label} ({code})")
            for name, v in choices:
                print(f"   {v:<28} {name}{'   [default]' if v == default else ''}")
        print("\nUse: dub.py <url> --lang hi --voice hi-IN-MadhurNeural")
        parser.exit()


def main() -> None:
    interactive = len(sys.argv) == 1        # double-clicked, not run from a shell
    ap = argparse.ArgumentParser(description="Timestamp-synced translated dub of a YouTube video.")
    ap.add_argument("url", help="YouTube URL or 11-char id")
    ap.add_argument("--lang", required=True, help="target code, e.g. hi es fr (see app.py LANGUAGES)")
    ap.add_argument("--out", help="output file (.mp3, or a video ext when muxing)")
    ap.add_argument("--video", help="local video file to mux the dub onto (skips download)")
    ap.add_argument("--download", action="store_true",
                    help="download the source video and mux the dub onto it")
    ap.add_argument("--max-speed", type=float, default=1.5,
                    help="'natural' speed threshold; lines that must go faster to fit are flagged (default 1.5)")
    ap.add_argument("--hard-max", type=float, default=3.0,
                    help="absolute speed ceiling used to guarantee fit (default 3.0)")
    ap.add_argument("--allow-drift", action="store_true",
                    help="don't force the fit: cap at --max-speed for a natural voice and let long lines drift")
    ap.add_argument("--tts-rate-max", type=int, default=40, metavar="PCT",
                    help="how much of an overrun the VOICE absorbs by speaking faster "
                         "before ffmpeg squeezes the rest; sounds better than atempo "
                         "alone. 0 = off, old behaviour (default 40)")
    ap.add_argument("--keep-original", type=float, default=0.0,
                    help="0-1 volume of the original audio kept under the dub (video output only)")
    ap.add_argument("--retime", action="store_true",
                    help="keep the voice near natural speed and stretch the VIDEO to fit instead "
                         "(hybrid: splits the mismatch across audio and video). Implies a video output.")
    ap.add_argument("--max-video", type=float, default=1.25,
                    help="retime mode: cap on how far the video may be slowed (default 1.25; "
                         "use ~1.1 for talking heads, higher for faceless footage)")
    ap.add_argument("--smooth", action="store_true",
                    help="retime mode: motion-interpolate slowed video (smoother but slow to render)")
    ap.add_argument("--faces", action="store_true",
                    help="preset for talking-head footage: --retime with a tight video bound (--max-video 1.1)")
    ap.add_argument("--broll", action="store_true",
                    help="preset for faceless footage (POV/gameplay/screencast): --retime --max-video 1.6 --smooth")
    ap.add_argument("--voice", default=None, metavar="NAME",
                    help="voice for the dub, e.g. hi-IN-MadhurNeural for a male Hindi "
                         "voice (default: the language's standard voice). "
                         "See --list-voices")
    ap.add_argument("--list-voices", action=_ListVoices, nargs=0,
                    help="print the available voices for every language and exit")
    ap.add_argument("--hinglish", action="store_true",
                    help="speak natural Hinglish (Hindi-English mix) instead of formal Hindi (--lang hi only)")
    ap.add_argument("--hinglish-model", default=None,
                    help="override the Ollama model used for the Hinglish restyle (default gemma3:4b)")
    ap.add_argument("--source", choices=("whisper", "captions"), default="whisper",
                    help="transcript source: 'whisper' = transcribe the audio ourselves "
                         "(accurate, default); 'captions' = use YouTube captions (faster, "
                         "but auto-captions are error-prone)")
    ap.add_argument("--whisper-model", default="small",
                    help="faster-whisper model size: tiny/base/small/medium/large-v3 "
                         "(bigger = more accurate, slower; default small)")
    args = ap.parse_args(_interactive_argv() if interactive else None)
    try:
        # On the console the percentage is noise — just print the same lines this
        # tool always printed.
        run_dub(args, progress=lambda pct, msg: print(msg))
    except DubError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    # When double-clicked (no args) keep the window open so the person can read
    # the result or any error, instead of the console flashing shut.
    _interactive = len(sys.argv) == 1
    try:
        main()
    except SystemExit as e:
        # `main()` reports a DubError as sys.exit(str(e)), which normally prints the
        # message to stderr and exits 1. Catching it here and doing nothing when the
        # console wasn't interactive meant EVERY CLI error — bad URL, unknown
        # language, unavailable Hinglish model — printed nothing and exited 0, so a
        # script calling dub.py read failure as success. Only the double-clicked
        # case needs to be intercepted, and only so the window can be held open.
        if not _interactive:
            raise
        if e.code not in (0, None):
            print(f"\nStopped: {e.code}")
    except Exception:
        if not _interactive:
            raise
        import traceback
        traceback.print_exc()
    finally:
        if _interactive:
            input("\nPress Enter to close.")
