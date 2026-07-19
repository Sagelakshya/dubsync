# dubsync

**Translate a YouTube video into another language, with the new voice kept in time with the picture.**

Most caption translators hand you a wall of text or a voice track that drifts out of
sync. dubsync keeps every caption's timestamp and *fits each translated line to its slot*,
so the dubbed audio lines up with what's on screen. Runs entirely on your own machine.

---

## Get it

**Easiest — no install (Windows):** download the latest **`dubsync-portable-win64.zip`**
from the [Releases](../../releases) page, unzip it, and double-click **`Dub a video.cmd`**.
It bundles its own Python and ffmpeg, so there is nothing to set up. Paste a link, pick a
language, choose the footage type — your `dubbed.mp4` appears in the folder.

**From source (any OS with Python 3.10+):**

```bash
git clone https://github.com/Sagelakshya/dubsync
cd dubsync
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# ffmpeg must be installed and on PATH (or set FFMPEG_DIR):
#   Windows: winget install Gyan.FFmpeg   |   macOS: brew install ffmpeg   |   Linux: apt install ffmpeg
python dub.py "https://youtu.be/VIDEO_ID" --lang hi
```

## What it does

- **Synced dub (`dub.py`)** — captions (kept *with* timestamps) → regrouped into sentences →
  translated → spoken with free Microsoft Edge voices → each line fit to its caption window
  so it stays in sync → optionally muxed onto the downloaded video.
- **Caption translator (web app, `app.py`)** — a local browser page showing the original and
  translation side by side, read-aloud, and a paste-any-text-to-speech box.

## Keeping it in sync — the two strategies

- **Default (fit the audio):** each translated line is sped up just enough to fit its window
  using ffmpeg `atempo` (pitch-preserving — faster speech, not chipmunk). Guarantees the fit.
- **`--retime` (stretch the video):** keep the voice near natural speed and gently retime the
  *video* instead, splitting the mismatch across both. Presets: `--faces` (tight video bound,
  for talking heads) and `--broll` (looser, for POV/gameplay/screen recordings).

```bash
python dub.py VIDEO_ID --lang es --download            # fit the audio, mux onto the video
python dub.py VIDEO_ID --lang hi --faces --download    # talking head: retime, tight video bound
python dub.py VIDEO_ID --lang hi --broll --download    # faceless footage: retime + motion-smooth
python dub.py VIDEO_ID --lang fr --out dub.mp3          # audio only
```

Run `python dub.py --help` for every flag.

## Honest limitations

- Translated speech is usually a different length than the original (Hindi/Spanish expand).
  Forcing the fit speeds expansion-heavy lines up audibly; `--retime` on a talking head shows
  the stretched motion. The dials (`--max-speed`, `--max-video`, `--allow-drift`) let you trade
  tightness of sync against how natural it sounds.
- It uses a video's captions (including YouTube's auto-generated ones). A video with captions
  fully disabled has nothing to translate.
- Quality comes from free services (Google Translate, Edge voices) — good, not perfect.

## Built with

Python · [yt-dlp](https://github.com/yt-dlp/yt-dlp) · [edge-tts](https://github.com/rany2/edge-tts) ·
[deep-translator](https://github.com/nidhaloff/deep-translator) ·
[youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api) · ffmpeg · Flask

## License

dubsync's own code is [MIT](LICENSE). It builds on open-source software (ffmpeg, edge-tts, yt-dlp,
Flask and others) and calls free external services (Google Translate, Microsoft voices, YouTube) —
all credited, with their licenses, in [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md). The bundled
ffmpeg is GPLv3 and edge-tts is LGPLv3; the portable download ships their full license texts.

Everything runs locally; nothing you translate is uploaded to a dubsync server (there isn't one).
