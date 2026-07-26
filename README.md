# YouTube Transcript Translator + Video Dubber

Runs entirely on your own machine. Two ways in:

- **Dub a video (one click):** double-click **`dub.cmd`**, paste a YouTube link,
  answer a few questions (language, and for Hindi whether you want natural
  **Hinglish**), and get back a `dubbed.mp4` whose translated voice stays in sync
  with the picture. It **transcribes the audio itself** (Whisper) for an accurate
  script, rather than trusting error-prone auto-captions. First run sets itself up
  automatically. ffmpeg is bundled — no installs, no PATH fiddling.
- **Translate captions (web app):** double-click **`run.cmd`** for a browser page
  that shows the original + translation side by side, reads it aloud, and turns any
  pasted text into speech.

The rest of this file explains both in detail.

## What it does
1. Reads the **captions** of a YouTube video (works for any video that has
   captions — including YouTube's auto-generated ones).
2. Translates them into the language you choose, using free Google Translate.
3. Shows the original and the translation side by side in your browser.
4. **Read aloud:** click the speaker button to hear the translation in a natural
   voice (free Microsoft Edge voices) and download it as an `.mp3`.

You can also skip YouTube entirely: scroll down to **"Or paste your own script"**,
type or paste any text, pick a language/voice, and generate an `.mp3` directly.

> It uses captions only — it does **not** download or watch the video. If a
> video has captions completely disabled, there's nothing to translate.

## Setup (one time)

You need **Python 3.10+** installed ([python.org](https://www.python.org/downloads/),
tick "Add Python to PATH" during install).

**Windows — easiest:** double-click **`run.cmd`**. The first run installs
everything automatically, then starts the app.

**Any OS — manual:**
```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

**For the synced dub (`dub.py`) you also need ffmpeg.** The web app above doesn't
need it, but `dub.py` does. Get it working one of three ways: install ffmpeg and
add it to your PATH; drop `ffmpeg.exe` + `ffprobe.exe` into an `ffmpeg\bin` folder
next to `dub.py`; or set an `FFMPEG_DIR` environment variable to the folder that
holds them. `dub.py` runs from the same venv: `.venv\Scripts\python.exe dub.py ...`

## Use it
Once it's running, open **http://127.0.0.1:5000** in your browser.
Paste a link, choose a language, click **Translate**. Use **Copy** to grab the text.

Stop the app with **Ctrl+C** in the terminal window.

## Notes
- Long videos are translated in chunks automatically.
- Free Google Translate quality is good, not perfect.
- Everything runs locally; nothing is stored.

## Synced dub (`dub.py`) — translated voiceover that matches the video

The web app reads captions as one flat block, so its `.mp3` doesn't line up with
the video. `dub.py` fixes that: it keeps every caption's timestamp and produces a
translated voiceover **synced to the video timeline**.

```bash
# audio only — a dubbed.mp3 that lines up when played alongside the video
.venv\Scripts\python.exe dub.py "https://youtu.be/VIDEO_ID" --lang hi

# download the video and mux the dub straight onto it
.venv\Scripts\python.exe dub.py VIDEO_ID --lang es --download --out dubbed.mp4

# use a video you already have (skips the download)
.venv\Scripts\python.exe dub.py VIDEO_ID --lang es --video clip.mp4 --out dubbed.mp4

# keep the original audio faintly under the dub (music/ambience)
.venv\Scripts\python.exe dub.py VIDEO_ID --lang hi --download --keep-original 0.15
```

**The core guarantee — every line fits its time slot.** Translated speech is
usually a different length than the original (English→Hindi expands, Hindi→English
shrinks). Each sentence is sped up by exactly the factor needed to fit its caption
window — `atempo`, which preserves pitch, so it's *faster speech*, not chipmunk —
so the dub stays locked to the video with essentially zero drift. Shorter lines
aren't slowed; the natural pause after them absorbs the slack.

How it works: transcript → regrouped into sentences (each with a time window) →
translated per sentence → spoken with edge-tts → **fit** into its window → laid
onto a silent timeline at its real offset.

### Transcript source: Whisper (default) or captions

The dub's accuracy ceiling is its transcript, so by default `dub.py` **transcribes
the audio itself** with Whisper (`faster-whisper`) — accurate, punctuated, whole
sentences. That matters: YouTube auto-captions often chop a sentence mid-thought,
which strips the context the translator needs ("long trunks" with no "elephant"
nearby gets mistranslated). Whole sentences fix that at the source. Whisper also
works on videos that have **no captions at all**.

```bash
# default — transcribe the audio (accurate); first run downloads the model (~0.5 GB)
.venv\Scripts\python.exe dub.py VIDEO_ID --lang hi

# faster: use YouTube captions instead (warns if they're the auto-generated kind)
.venv\Scripts\python.exe dub.py VIDEO_ID --lang hi --source captions

# bigger Whisper model = more accurate, slower (tiny/base/small/medium/large-v3)
.venv\Scripts\python.exe dub.py VIDEO_ID --lang hi --whisper-model medium
```

Whisper uses the GPU when CUDA/cuDNN is available and **falls back to CPU**
otherwise (slower on long videos, but it always works).

### Hinglish — natural Hindi-English (Hindi only)

`--hinglish` makes the Hindi dub sound like a real Indian YouTuber (Hindi grammar
with the English words people actually say — बिज़नेस, ऑनलाइन, प्रॉब्लम) instead of
stiff *shuddh* Hindi. Two backends:

```bash
# local, free, offline — needs Ollama + a GPU on this PC
.venv\Scripts\python.exe dub.py VIDEO_ID --lang hi --hinglish
#   setup: install Ollama (ollama.com), then:  ollama pull gemma3:4b

# Sarvam API — best quality, no GPU; needs a free key
set SARVAM_API_KEY=your_key      # get one at dashboard.sarvam.ai
.venv\Scripts\python.exe dub.py VIDEO_ID --lang hi --hinglish --hinglish-engine sarvam
```

For a teammate on a laptop **without** a GPU, the Sarvam engine is the easy path
(just a key). If either backend is unavailable at run time, the dub still succeeds
in accurate (slightly more formal) Hindi rather than failing.

- **Dense speech:** effectively perfect — a 14-min talk to Spanish held under ~1s
  drift with the final line dead on time; every line lands on its caption time.
- **The trade you control:** to *guarantee* fit, expansion-heavy lines get sped
  up. A very slow, pause-heavy speaker can need 2–3× on a stretch (the run prints
  which lines and how fast). Dials:
  - `--max-speed 1.5` — the "natural" threshold; lines faster than this are flagged.
  - `--hard-max 3.0` — absolute ceiling used to force the fit.
  - `--allow-drift` — the opposite choice: cap at `--max-speed` for a calmer voice
    and let long lines overrun instead (re-anchored at the next pause).
- `--download` needs internet; muxing/fitting needs the bundled `D:\Toolkit\ffmpeg`
  (or ffmpeg on PATH). Deps: adds `yt-dlp` (for `--download`) to what the web app
  already uses.

### `--retime`: keep the voice natural, stretch the video instead

The default fits by speeding the *audio* up. `--retime` does the opposite where it
helps: it keeps the voice near natural speed and gently **stretches the video** to
make room, splitting the mismatch across both so neither is pushed hard. A line
that would need 2.3× on audio alone becomes ~1.5× audio + ~1.5× video.

```bash
# hybrid retime, downloading the source
.venv\Scripts\python.exe dub.py VIDEO_ID --lang hi --retime --download

# presets (recommended) — pick by footage type instead of remembering numbers:
.venv\Scripts\python.exe dub.py VIDEO_ID --lang hi --faces --download   # talking heads: tight video bound (1.1x)
.venv\Scripts\python.exe dub.py VIDEO_ID --lang hi --broll --download   # POV/gameplay/screencast: 1.6x + --smooth

# or set the video bound by hand
.venv\Scripts\python.exe dub.py VIDEO_ID --lang hi --retime --video clip.mp4 --max-video 1.3
```

`--faces` = `--retime --max-video 1.1`. `--broll` = `--retime --max-video 1.6 --smooth`.
An explicit `--max-video` still overrides the preset.

- `--max-video 1.25` — cap on how far the video may be slowed. Lower it (~1.1) for
  faces; raise it for faceless footage. When both this and `--max-speed` are maxed
  and a line still doesn't fit, the audio takes the remainder (the fit always wins).
- `--smooth` — motion-interpolate the slowed video (smoother, but much slower to
  render). Leave off for a quick result with duplicated frames.
- **Needs the video** (`--download` or `--video`) and re-encodes it, so it's slower
  and heavier than the audio-only path. Alignment is exact: each line's audio is fit
  to the *measured* length of its rendered video segment, so nothing drifts.
- Retime **replaces** the original audio (it doesn't mix the original under, since
  that track would need retiming too). The net video length changes when there's
  net expansion — expected, since you're making room for the longer speech.
- **Best for faceless footage;** on talking heads the stretched motion is visible,
  so keep `--max-video` tight or use the default audio-fit mode there.
