"""app.py — dubsync's local web GUI.

Two things live here, both in the browser at http://127.0.0.1:5000 :

  1. **Dub a video** — the real product. Paste a YouTube link, pick a language and
     a couple of options, watch it work, download the dubbed file. The pipeline
     itself is `dub.py`; this file only drives it and reports progress.
  2. **Text -> speech** — paste any script, optionally translate it, pick a voice,
     get an .mp3. No video needed.

Why a GUI at all: a dub is a multi-minute job with half a dozen knobs, and the
CLI/console menu is a wall for anyone who isn't the person who wrote it. This is
the front door a teammate can actually use.

Why background jobs + polling: a dub takes minutes, far longer than any sane HTTP
request. So the browser POSTs once to start a job, then polls a status endpoint;
the work happens on a worker thread that reports progress through a callback.

Run:   python app.py      then open http://127.0.0.1:5000
"""
import asyncio
import json
import os
import re
import threading
import time
import traceback
import uuid
from urllib.parse import urlparse, parse_qs

from flask import (Flask, request, jsonify, render_template_string, Response,
                   send_file)

app = Flask(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
# Finished dubs land in a real folder next to the app (not a temp dir) so they
# survive the browser tab, and so a teammate can find them without the GUI.
OUT_DIR = os.path.join(_HERE, "dubs")

# Languages offered in the dropdown -> (label, GoogleTranslator code).
LANGUAGES = [
    ("Spanish", "es"), ("French", "fr"), ("German", "de"),
    ("Hindi", "hi"), ("Bengali", "bn"), ("Urdu", "ur"),
    ("Tamil", "ta"), ("Telugu", "te"), ("Arabic", "ar"),
    ("Chinese (Simplified)", "zh-CN"), ("Japanese", "ja"),
    ("Korean", "ko"), ("Russian", "ru"), ("Portuguese", "pt"),
    ("Italian", "it"), ("English", "en"),
]

# Default read-aloud voice (edge-tts) per language — what the dubber speaks with.
VOICES = {
    "es": "es-ES-ElviraNeural", "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",  "hi": "hi-IN-SwaraNeural",
    "bn": "bn-IN-TanishaaNeural", "ur": "ur-PK-UzmaNeural",
    "ta": "ta-IN-PallaviNeural", "te": "te-IN-ShrutiNeural",
    "ar": "ar-EG-SalmaNeural",  "zh-CN": "zh-CN-XiaoxiaoNeural",
    "ja": "ja-JP-NanamiNeural", "ko": "ko-KR-SunHiNeural",
    "ru": "ru-RU-SvetlanaNeural", "pt": "pt-BR-FranciscaNeural",
    "it": "it-IT-ElsaNeural",   "en": "en-US-AriaNeural",
}

# Voices the text->speech panel offers, so a script isn't stuck with one narrator.
# Curated from the live `edge_tts.list_voices()` catalogue (checked 2026-07-26) and
# hardcoded on purpose: the picker then works offline and can't shift under us.
VOICE_CHOICES = {
    "es": [("Elvira (F)", "es-ES-ElviraNeural"), ("Alvaro (M)", "es-ES-AlvaroNeural"),
           ("Ximena (F)", "es-ES-XimenaNeural")],
    "fr": [("Denise (F)", "fr-FR-DeniseNeural"), ("Henri (M)", "fr-FR-HenriNeural"),
           ("Eloise (F)", "fr-FR-EloiseNeural")],
    "de": [("Katja (F)", "de-DE-KatjaNeural"), ("Conrad (M)", "de-DE-ConradNeural"),
           ("Amala (F)", "de-DE-AmalaNeural")],
    "hi": [("Swara (F)", "hi-IN-SwaraNeural"), ("Madhur (M)", "hi-IN-MadhurNeural")],
    "bn": [("Tanishaa (F)", "bn-IN-TanishaaNeural"), ("Bashkar (M)", "bn-IN-BashkarNeural")],
    "ur": [("Uzma (F)", "ur-PK-UzmaNeural"), ("Asad (M)", "ur-PK-AsadNeural")],
    "ta": [("Pallavi (F)", "ta-IN-PallaviNeural"), ("Valluvar (M)", "ta-IN-ValluvarNeural")],
    "te": [("Shruti (F)", "te-IN-ShrutiNeural"), ("Mohan (M)", "te-IN-MohanNeural")],
    "ar": [("Salma (F)", "ar-EG-SalmaNeural"), ("Shakir (M)", "ar-EG-ShakirNeural")],
    "zh-CN": [("Xiaoxiao (F)", "zh-CN-XiaoxiaoNeural"), ("Yunxi (M)", "zh-CN-YunxiNeural"),
              ("Yunyang (M)", "zh-CN-YunyangNeural")],
    "ja": [("Nanami (F)", "ja-JP-NanamiNeural"), ("Keita (M)", "ja-JP-KeitaNeural")],
    "ko": [("SunHi (F)", "ko-KR-SunHiNeural"), ("InJoon (M)", "ko-KR-InJoonNeural")],
    "ru": [("Svetlana (F)", "ru-RU-SvetlanaNeural"), ("Dmitry (M)", "ru-RU-DmitryNeural")],
    "pt": [("Francisca (F)", "pt-BR-FranciscaNeural"), ("Antonio (M)", "pt-BR-AntonioNeural")],
    "it": [("Elsa (F)", "it-IT-ElsaNeural"), ("Diego (M)", "it-IT-DiegoNeural"),
           ("Isabella (F)", "it-IT-IsabellaNeural")],
    "en": [("Aria (F, US)", "en-US-AriaNeural"), ("Guy (M, US)", "en-US-GuyNeural"),
           ("Neerja (F, India)", "en-IN-NeerjaNeural"), ("Prabhat (M, India)", "en-IN-PrabhatNeural")],
}


# --- find the video id ----------------------------------------------------
def yt_id(url: str) -> str | None:
    url = (url or "").strip()
    if re.fullmatch(r"[\w-]{11}", url):  # already a bare id
        return url
    u = urlparse(url)
    if u.netloc.endswith("youtu.be"):
        return u.path.lstrip("/").split("/")[0] or None
    if "youtube.com" in u.netloc:
        if u.path.startswith("/watch"):
            return parse_qs(u.query).get("v", [None])[0]
        parts = [p for p in u.path.split("/") if p]
        if parts and parts[0] in ("shorts", "embed", "v") and len(parts) > 1:
            return parts[1]
    return None


# --- translate (chunked; Google caps a call at ~5000 chars) --------------
def translate(text: str, target: str) -> str:
    from deep_translator import GoogleTranslator
    tr = GoogleTranslator(source="auto", target=target)
    chunks, cur = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(cur) + len(sentence) + 1 > 4500:
            chunks.append(cur)
            cur = ""
        cur += (" " if cur else "") + sentence
    if cur:
        chunks.append(cur)
    return " ".join(tr.translate(c) for c in chunks if c.strip())


# --- read-aloud (edge-tts; chunked, MP3 bytes concatenated) --------------
async def _synth_chunk(text: str, voice: str, rate: str) -> bytes:
    import edge_tts
    buf = bytearray()
    async for part in edge_tts.Communicate(text, voice, rate=rate).stream():
        if part["type"] == "audio":
            buf.extend(part["data"])
    return bytes(buf)


def synthesize(text: str, lang: str, voice: str | None = None,
               rate: str = "+0%") -> bytes:
    """Speak `text`. `voice` overrides the language default; `rate` is edge-tts's
    signed percentage ("+15%" / "-10%") so a script can be paced without a re-record."""
    voice = voice or VOICES.get(lang, VOICES["en"])
    chunks, cur = [], ""
    for sentence in re.split(r"(?<=[.!?。।])\s+", text):
        if len(cur) + len(sentence) + 1 > 3000:
            chunks.append(cur)
            cur = ""
        cur += (" " if cur else "") + sentence
    if cur:
        chunks.append(cur)

    parts = [c for c in chunks if c.strip()]

    async def run():
        # Generate chunks concurrently (order preserved by gather), but cap
        # how many hit Microsoft at once so long videos don't get throttled.
        sem = asyncio.Semaphore(6)

        async def one(c):
            async with sem:
                return await _synth_chunk(c, voice, rate)

        audio_parts = await asyncio.gather(*(one(c) for c in parts))
        return b"".join(audio_parts)

    return asyncio.run(run())


# ============================================================================
# Dub jobs — a dub takes minutes, so it runs on a worker thread and the page polls
# ============================================================================
# In-memory registry: the GUI is a single local process, so there's nothing to
# persist. The finished FILES are on disk in OUT_DIR; only the live status is here.
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
MAX_RUNNING = 2          # a dub is CPU-heavy; more than two at once just thrashes

# Footage choice -> the dub.py preset that suits it. Talking heads can't take much
# video stretching (visible), faceless footage can, and "audio only" skips video
# entirely (fastest, and it's what you want if you're editing the dub in yourself).
FOOTAGE = {
    "talking":  dict(faces=True,  download=True,  ext="mp4"),
    "faceless": dict(broll=True,  download=True,  ext="mp4"),
    "audio":    dict(                             ext="mp3"),
}


def _running_count() -> int:
    return sum(1 for j in JOBS.values() if j["status"] in ("queued", "running"))


def _run_job(job_id: str, opts: dict) -> None:
    """Worker thread: run the pipeline, funnelling its progress into the registry."""
    # Imported here, not at module scope, for two reasons: dub.py imports THIS
    # module (so a top-level import would be circular), and dub.py exits at import
    # time when ffmpeg is missing — which must fail this one job, not the server.
    def fail(msg: str) -> None:
        with LOCK:
            JOBS[job_id].update(status="error", error=msg, finished=time.time())
            JOBS[job_id]["log"].append("ERROR: " + msg)

    def progress(pct, msg) -> None:
        with LOCK:
            j = JOBS[job_id]
            if pct is not None:
                j["pct"] = max(j["pct"], pct)   # never let the bar go backwards
            j["message"] = msg.strip()
            j["log"].append(msg.rstrip())
            del j["log"][:-300]                 # keep the tail, not the whole run

    def transcript(rows) -> None:
        # Arrives before the voice step, so the page can show the script while the
        # audio is still rendering.
        with LOCK:
            JOBS[job_id]["transcript"] = rows

    try:
        import dub
    except SystemExit as e:                     # ffmpeg missing -> dub.py sys.exits
        return fail(str(e) or "ffmpeg not found.")
    except Exception as e:
        return fail(f"Couldn't load the dubber: {e}")

    with LOCK:
        JOBS[job_id].update(status="running", message="Starting...")
    try:
        dub.run_dub(dub.options(**opts), progress=progress,
                    on_transcript=transcript)
    except dub.DubError as e:
        return fail(str(e))
    except Exception as e:
        traceback.print_exc()                   # full trace to the console...
        return fail(f"{type(e).__name__}: {e}")  # ...short version to the browser
    with LOCK:
        JOBS[job_id].update(status="done", pct=100, finished=time.time(),
                            message="Done.")


@app.post("/api/dub")
def api_dub_start():
    """Start a dub. Returns a job id the page then polls."""
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    lang = body.get("lang", "hi")
    footage = body.get("footage", "faceless")
    source = body.get("source", "whisper")
    hing = bool(body.get("hinglish"))

    if not yt_id(url):
        return jsonify(ok=False, error="That doesn't look like a YouTube link."), 400
    if lang not in dict((c, l) for l, c in LANGUAGES):
        return jsonify(ok=False, error=f"Unknown language '{lang}'."), 400
    if footage not in FOOTAGE:
        return jsonify(ok=False, error=f"Unknown footage type '{footage}'."), 400
    if source not in ("whisper", "captions"):
        return jsonify(ok=False, error=f"Unknown transcript source '{source}'."), 400
    if hing and lang != "hi":
        return jsonify(ok=False, error="Hinglish only applies to Hindi."), 400
    with LOCK:
        if _running_count() >= MAX_RUNNING:
            return jsonify(ok=False, error="Two dubs are already running — they're "
                                           "CPU-heavy, so wait for one to finish."), 429

    preset = dict(FOOTAGE[footage])
    ext = preset.pop("ext")
    os.makedirs(OUT_DIR, exist_ok=True)
    name = f"dub_{yt_id(url)}_{lang}_{time.strftime('%H%M%S')}.{ext}"
    opts = dict(url=url, lang=lang, source=source, hinglish=hing,
                out=os.path.join(OUT_DIR, name), **preset)

    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = dict(id=job_id, status="queued", pct=0, message="Queued...",
                            log=[], error=None, name=name, transcript=None,
                            path=opts["out"], started=time.time(), finished=None,
                            title=f"{name}  ({lang}{', Hinglish' if hing else ''}, {footage})")
    # daemon: closing the app shouldn't be blocked by a half-finished dub.
    threading.Thread(target=_run_job, args=(job_id, opts), daemon=True).start()
    return jsonify(ok=True, id=job_id)


@app.get("/api/dub/<job_id>")
def api_dub_status(job_id: str):
    with LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify(ok=False, error="No such job."), 404
        elapsed = int((j["finished"] or time.time()) - j["started"])
        return jsonify(ok=True, status=j["status"], pct=j["pct"],
                       message=j["message"], error=j["error"], name=j["name"],
                       elapsed=elapsed, log=j["log"][-40:],
                       # Only a flag here. The transcript can be hundreds of
                       # sentences and this endpoint is polled every second.
                       has_transcript=bool(j.get("transcript")),
                       ready=j["status"] == "done" and os.path.exists(j["path"]))


@app.get("/api/dub/<job_id>/transcript")
def api_dub_transcript(job_id: str):
    """The dub's script: every sentence with its timestamps, the original English
    and what was actually spoken. Fetched once, when the poll says it exists."""
    with LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify(ok=False, error="No such job."), 404
        rows = j.get("transcript")
    if not rows:
        return jsonify(ok=False, error="The transcript isn't ready yet."), 404
    return jsonify(ok=True, rows=rows, name=j["name"])


@app.get("/api/dub/<job_id>/file")
def api_dub_file(job_id: str):
    with LOCK:
        j = JOBS.get(job_id)
    if not j:
        return jsonify(ok=False, error="No such job."), 404
    if not os.path.exists(j["path"]):
        return jsonify(ok=False, error="The file isn't there (yet)."), 404
    # Same endpoint serves the in-page preview and the download; ?download=1 just
    # flips the disposition so the browser saves instead of streaming.
    return send_file(j["path"], as_attachment=bool(request.args.get("download")),
                     download_name=j["name"], conditional=True)


# ============================================================================
# Text -> speech, and the caption translator
# ============================================================================
@app.get("/")
def index():
    return render_template_string(PAGE, languages=LANGUAGES,
                                  voices_json=json.dumps(VOICE_CHOICES))


@app.post("/api/text/translate")
def api_text_translate():
    """Translate pasted text, so a script can be written in English and spoken in
    another language. Returned as text (not audio) so it can be edited first."""
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    lang = body.get("lang", "en")
    if not text:
        return jsonify(ok=False, error="Nothing to translate."), 400
    try:
        return jsonify(ok=True, text=translate(text, lang))
    except Exception as e:
        return jsonify(ok=False, error=f"Translation failed ({e})."), 500


@app.post("/api/audio")
def api_audio():
    body = request.get_json(silent=True) or {}
    text = body.get("text", "")
    lang = body.get("lang", "en")
    voice = body.get("voice") or None
    try:                                   # -50..+50 %, clamped: past that it slurs
        speed = max(-50, min(50, int(body.get("rate", 0))))
    except (TypeError, ValueError):
        speed = 0
    if not text.strip():
        return jsonify(ok=False, error="No text to read aloud."), 400
    try:
        audio = synthesize(text, lang, voice, f"{speed:+d}%")
    except Exception as e:
        return jsonify(ok=False, error=f"Audio generation failed ({e})."), 500
    return Response(audio, mimetype="audio/mpeg",
                    headers={"Content-Disposition": "inline; filename=speech.mp3"})


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dubsync</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --line:#2a2f3a; --fg:#e8eaed;
          --muted:#9aa3b2; --accent:#6c8cff; --ok:#4ade80; --err:#ff8080; }
  * { box-sizing:border-box; }
  body { margin:0; font:16px/1.6 system-ui,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--fg); }
  .wrap { max-width:900px; margin:0 auto; padding:32px 20px 80px; }
  h1 { font-size:1.6rem; margin:0 0 4px; }
  p.sub { color:var(--muted); margin:0 0 24px; }
  .tabs { display:flex; gap:6px; border-bottom:1px solid var(--line); margin-bottom:24px; }
  .tab { background:none; border:0; border-bottom:2px solid transparent; color:var(--muted);
         padding:10px 16px; font-size:15px; font-weight:600; cursor:pointer; }
  .tab.on { color:var(--fg); border-bottom-color:var(--accent); }
  .panel { background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:18px; margin-bottom:16px; }
  label.field { display:block; font-size:.78rem; text-transform:uppercase;
                letter-spacing:.06em; color:var(--muted); margin:0 0 6px; }
  input, select, textarea { background:#0f1115; color:var(--fg); border:1px solid var(--line);
                  border-radius:8px; padding:11px 12px; font-size:15px; font-family:inherit; }
  input[type=text], textarea { width:100%; }
  input[type=range] { accent-color:var(--accent); border:0; background:none; }
  button.go { background:var(--accent); color:#fff; border:0; border-radius:8px;
           padding:12px 22px; font-size:15px; font-weight:600; cursor:pointer; }
  button.go:disabled { opacity:.55; cursor:default; }
  .row { display:flex; gap:14px; flex-wrap:wrap; align-items:flex-end; }
  .row > div { flex:1 1 190px; }
  .choices { display:flex; gap:8px; flex-wrap:wrap; }
  .choice { flex:1 1 150px; border:1px solid var(--line); border-radius:10px; padding:10px 12px;
            cursor:pointer; background:#0f1115; }
  .choice.on { border-color:var(--accent); background:#161c33; }
  .choice input { display:none; }
  .choice b { display:block; font-size:14px; }
  .choice span { font-size:12px; color:var(--muted); }
  .status { margin:14px 2px; color:var(--muted); min-height:22px; }
  .status.err { color:var(--err); }
  .status.ok { color:var(--ok); }
  .bar { height:8px; background:#0f1115; border:1px solid var(--line); border-radius:99px;
         overflow:hidden; margin:6px 0 10px; }
  .bar i { display:block; height:100%; width:0; background:var(--accent);
           transition:width .4s ease; }
  .log { font:12px/1.5 ui-monospace,Consolas,monospace; color:var(--muted);
         background:#0b0d11; border:1px solid var(--line); border-radius:8px;
         padding:10px; max-height:190px; overflow:auto; white-space:pre-wrap; }
  @media (max-width:680px){ .row > div { flex-basis:100%; } }
  .mini { background:transparent; color:var(--accent); border:1px solid var(--line);
          padding:6px 12px; font-size:12px; font-weight:600; border-radius:8px;
          cursor:pointer; text-decoration:none; display:inline-block; }
  h2.small { font-size:.8rem; text-transform:uppercase; letter-spacing:.06em;
             color:var(--muted); margin:0 0 10px; display:flex;
             justify-content:space-between; align-items:center; }
  .hidden { display:none; }
  .hint { font-size:13px; color:var(--muted); margin:8px 2px 0; }
  audio, video { width:100%; margin-top:4px; border-radius:8px; }
  /* transcript: the script of the dub that just ran, in sync order */
  .tsc { max-height:430px; overflow:auto; background:#0b0d11;
         border:1px solid var(--line); border-radius:8px; }
  .tsc .t { display:grid; grid-template-columns:58px 1fr; gap:12px;
            padding:9px 12px; border-bottom:1px solid var(--line); }
  .tsc .t:last-child { border-bottom:0; }
  .tsc .t.idm { border-left:2px solid var(--accent); padding-left:10px; }
  .tsc .ts { color:var(--muted); font:12px/1.6 ui-monospace,Consolas,monospace; }
  .tsc .en { color:var(--muted); font-size:13px; line-height:1.5; }
  .tsc .hi { font-size:15px; line-height:1.6; margin-top:2px; }
  .tsc .tag { display:inline-block; margin-top:5px; padding:1px 7px; font-size:11px;
              color:var(--accent); border:1px solid var(--line); border-radius:6px; }
  .tsc.noen .en { display:none; }
</style>
</head>
<body>
<div class="wrap">
  <h1>dubsync</h1>
  <p class="sub">Dub a YouTube video into another language, in sync with the picture &mdash; on your own machine.</p>

  <div class="tabs">
    <button class="tab on" data-tab="dub">Dub a video</button>
    <button class="tab" data-tab="speak">Text &rarr; speech</button>
  </div>

  <!-- ============================ DUB ============================ -->
  <section id="tab-dub">
    <div class="panel">
      <label class="field" for="dUrl">YouTube link</label>
      <input id="dUrl" type="text" placeholder="https://www.youtube.com/watch?v=..." />

      <div class="row" style="margin-top:16px;align-items:flex-start;">
        <div>
          <label class="field" for="dLang">Dub into</label>
          <select id="dLang" style="width:100%;">
            {% for label, code in languages %}<option value="{{ code }}"{% if code == 'hi' %} selected{% endif %}>{{ label }}</option>{% endfor %}
          </select>
        </div>
        <div id="hingWrap">
          <label class="field">Style</label>
          <label class="choice on" id="hingBox" style="display:block;">
            <input type="checkbox" id="dHing" checked />
            <b>Natural Hinglish</b>
            <span>Hindi-English mix, like a real YouTuber. Needs Ollama running.</span>
          </label>
        </div>
      </div>

      <div style="margin-top:18px;">
        <label class="field">Transcript source</label>
        <div class="choices" id="srcChoices">
          <label class="choice on"><input type="radio" name="src" value="whisper" checked>
            <b>Whisper</b><span>Transcribe the audio &mdash; accurate. Recommended.</span></label>
          <label class="choice"><input type="radio" name="src" value="captions">
            <b>YouTube captions</b><span>Faster, but auto-captions are often wrong.</span></label>
        </div>
      </div>

      <div style="margin-top:18px;">
        <label class="field">What kind of footage?</label>
        <div class="choices" id="footChoices">
          <label class="choice"><input type="radio" name="foot" value="talking">
            <b>Talking head</b><span>A person on camera. Video barely stretched.</span></label>
          <label class="choice on"><input type="radio" name="foot" value="faceless" checked>
            <b>No faces</b><span>POV / gameplay / screen recording.</span></label>
          <label class="choice"><input type="radio" name="foot" value="audio">
            <b>Audio only</b><span>Just the dubbed .mp3 &mdash; fastest.</span></label>
        </div>
      </div>

      <div style="margin-top:20px;">
        <button class="go" id="dGo">Start dubbing</button>
        <span class="hint" id="dHint">A dub takes minutes &mdash; you can leave this tab open.</span>
      </div>
    </div>

    <div class="panel hidden" id="dProgress">
      <h2 class="small"><span id="dStage">Working...</span><span id="dElapsed"></span></h2>
      <div class="bar"><i id="dBar"></i></div>
      <div class="log" id="dLog"></div>
    </div>

    <!-- Appears as soon as the script is final, which is before the audio is
         rendered — no reason to wait on TTS to read what the dub will say. -->
    <div class="panel hidden" id="dScript">
      <h2 class="small"><span>Transcript <span id="dScriptN"></span></span>
        <span>
          <button class="mini" id="dScriptEn">Hide English</button>
          <button class="mini" id="dScriptCopy">Copy</button>
        </span></h2>
      <div class="tsc" id="dScriptBody"></div>
    </div>

    <div class="panel hidden" id="dResult">
      <h2 class="small"><span id="dName" style="text-transform:none;">Done</span>
        <a class="mini" id="dDl" href="#">Download</a></h2>
      <div id="dPlayer"></div>
      <p class="hint" id="dPath"></p>
    </div>
    <div id="dStatus" class="status"></div>
  </section>

  <!-- ============================ SPEAK ============================ -->
  <section id="tab-speak" class="hidden">
    <div class="panel">
      <label class="field" for="sText">Your script</label>
      <textarea id="sText" rows="7" placeholder="Paste or type any text..."></textarea>
      <p class="hint"><span id="sCount">0</span> characters &mdash; long scripts are split and stitched automatically.</p>

      <div class="row" style="margin-top:14px;">
        <div>
          <label class="field" for="sLang">Language</label>
          <select id="sLang" style="width:100%;"></select>
        </div>
        <div>
          <label class="field" for="sVoice">Voice</label>
          <select id="sVoice" style="width:100%;"></select>
        </div>
        <div style="flex:0 0 150px;">
          <label class="field" for="sRate">Speed <span id="sRateVal">0%</span></label>
          <input id="sRate" type="range" min="-50" max="50" step="5" value="0" style="width:100%;padding:0;">
        </div>
      </div>

      <div style="margin-top:18px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
        <button class="go" id="sGo">Generate audio</button>
        <button class="mini" id="sTranslate">Translate the text first</button>
        <span class="hint">Translate rewrites the box above &mdash; edit it, then generate.</span>
      </div>
    </div>

    <div id="sAudioWrap" class="panel hidden">
      <h2 class="small"><span>Result</span><a class="mini" id="sDl" download="speech.mp3" href="#">Download .mp3</a></h2>
      <audio id="sPlayer" controls></audio>
    </div>
    <div id="sStatus" class="status"></div>
  </section>

</div>

<script>
const $ = id => document.getElementById(id);
const VOICES = {{ voices_json|safe }};
const LANGS = [{% for label, code in languages %}["{{ label }}","{{ code }}"],{% endfor %}];

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("on", x === t));
  ["dub","speak"].forEach(n =>
    $("tab-" + n).classList.toggle("hidden", n !== t.dataset.tab));
}));

/* Radio "cards": keep the highlight on the checked one. */
function wireChoices(id) {
  const box = $(id);
  box.addEventListener("change", () => box.querySelectorAll(".choice").forEach(c =>
    c.classList.toggle("on", c.querySelector("input").checked)));
}
wireChoices("srcChoices"); wireChoices("footChoices");
const picked = name => document.querySelector(`input[name="${name}"]:checked`).value;

/* ---------- dub ---------- */
/* Hinglish is a Hindi-only idea, so the option only exists when Hindi is picked. */
function syncHinglish() {
  const hi = $("dLang").value === "hi";
  $("hingWrap").classList.toggle("hidden", !hi);
}
$("dLang").addEventListener("change", syncHinglish); syncHinglish();
$("dHing").addEventListener("change", () => $("hingBox").classList.toggle("on", $("dHing").checked));

let poll = null;
function setStatus(el, msg, kind) { el.className = "status " + (kind || ""); el.textContent = msg; }

async function startDub() {
  const url = $("dUrl").value.trim();
  if (!url) { setStatus($("dStatus"), "Paste a YouTube link first.", "err"); return; }
  const body = {
    url, lang: $("dLang").value, source: picked("src"), footage: picked("foot"),
    hinglish: $("dLang").value === "hi" && $("dHing").checked
  };
  $("dGo").disabled = true;
  setStatus($("dStatus"), "");
  $("dResult").classList.add("hidden");
  $("dScript").classList.add("hidden");
  scriptRows = null;                     /* a new run gets a new script */
  $("dProgress").classList.remove("hidden");
  $("dBar").style.width = "0%"; $("dLog").textContent = ""; $("dStage").textContent = "Starting...";
  try {
    const res = await fetch("/api/dub", { method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(body) });
    const data = await res.json();
    if (!data.ok) {
      setStatus($("dStatus"), data.error, "err");
      $("dGo").disabled = false; $("dProgress").classList.add("hidden"); return;
    }
    watch(data.id);
  } catch (e) {
    setStatus($("dStatus"), "Couldn't start: " + e, "err"); $("dGo").disabled = false;
  }
}

function watch(id) {
  clearInterval(poll);
  poll = setInterval(async () => {
    let d;
    try { d = await (await fetch("/api/dub/" + id)).json(); }
    catch (e) { return; }                      /* a dropped poll is not fatal */
    if (!d.ok) { clearInterval(poll); $("dGo").disabled = false; return; }
    $("dBar").style.width = d.pct + "%";
    $("dStage").textContent = d.message || "Working...";
    $("dElapsed").textContent = fmt(d.elapsed);
    const log = $("dLog");
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
    log.textContent = d.log.join("\\n");
    if (atBottom) log.scrollTop = log.scrollHeight;
    if (d.has_transcript) loadScript(id);
    if (d.status === "done") {
      clearInterval(poll); $("dGo").disabled = false;
      setStatus($("dStatus"), "Done in " + fmt(d.elapsed) + ".", "ok");
      showResult(id, d.name);
    } else if (d.status === "error") {
      clearInterval(poll); $("dGo").disabled = false;
      setStatus($("dStatus"), d.error || "It failed.", "err");
    }
  }, 1200);
}

function fmt(s) {
  s = s || 0;
  return s < 60 ? s + "s" : Math.floor(s/60) + "m " + (s%60) + "s";
}

function showResult(id, name) {
  const src = "/api/dub/" + id + "/file";
  $("dName").textContent = name;
  $("dDl").href = src + "?download=1";
  $("dDl").setAttribute("download", name);
  $("dPlayer").innerHTML = name.endsWith(".mp3")
    ? '<audio controls src="' + src + '"></audio>'
    : '<video controls src="' + src + '"></video>';
  $("dPath").textContent = "Saved in the app's dubs\\\\ folder as " + name;
  $("dResult").classList.remove("hidden");
}

/* ---------- transcript ---------- */
/* Fetched once and cached: the status poll runs every 1.2s but a 20-minute talk
   is ~300 sentences, so the script rides its own endpoint and is asked for once. */
let scriptRows = null;

async function loadScript(id) {
  if (scriptRows) return;
  scriptRows = "loading";                      /* claim it before awaiting */
  try {
    const d = await (await fetch("/api/dub/" + id + "/transcript")).json();
    if (!d.ok) { scriptRows = null; return; }
    scriptRows = d.rows;
    drawScript();
  } catch (e) { scriptRows = null; }           /* a dropped fetch just retries */
}

function ts(s) {
  const m = Math.floor(s / 60), x = Math.floor(s % 60);
  return m + ":" + String(x).padStart(2, "0");
}

function esc(s) {
  return (s || "").replace(/[&<>]/g, c => (
    c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;"));
}

function drawScript() {
  if (!Array.isArray(scriptRows)) return;
  const idioms = scriptRows.reduce((n, r) => n + (r.idioms ? r.idioms.length : 0), 0);
  $("dScriptN").textContent = scriptRows.length + " lines" +
    (idioms ? " \\u00b7 " + idioms + " idiom" + (idioms > 1 ? "s" : "") : "");
  $("dScriptBody").innerHTML = scriptRows.map(r => {
    /* Mark the substituted spans. They are the one part of the spoken line that
       is deliberately NOT a translation of the English above it, so someone
       comparing the two columns should be told why they differ. */
    const tags = (r.idioms || []).map(m =>
      '<span class="tag">' + esc(m.en) + " \\u2192 " + esc(m.hi) + "</span>").join(" ");
    return '<div class="t' + (tags ? " idm" : "") + '">' +
             '<div class="ts">' + ts(r.start) + "</div>" +
             "<div>" +
               '<div class="en">' + esc(r.text) + "</div>" +
               '<div class="hi">' + esc(r.tr) + "</div>" +
               tags +
             "</div>" +
           "</div>";
  }).join("");
  $("dScript").classList.remove("hidden");
}

$("dScriptEn").addEventListener("click", () => {
  const body = $("dScriptBody"), off = body.classList.toggle("noen");
  $("dScriptEn").textContent = off ? "Show English" : "Hide English";
});

$("dScriptCopy").addEventListener("click", async () => {
  if (!Array.isArray(scriptRows)) return;
  const text = scriptRows.map(r => ts(r.start) + "\\t" + r.text + "\\n\\t" + r.tr)
                         .join("\\n\\n");
  try {
    await navigator.clipboard.writeText(text);
    $("dScriptCopy").textContent = "Copied";
  } catch (e) {
    $("dScriptCopy").textContent = "Copy failed";
  }
  setTimeout(() => { $("dScriptCopy").textContent = "Copy"; }, 1500);
});

$("dGo").addEventListener("click", startDub);
$("dUrl").addEventListener("keydown", e => { if (e.key === "Enter") startDub(); });

/* ---------- text -> speech ---------- */
function fillLangs(sel, def) {
  sel.innerHTML = LANGS.map(([l,c]) =>
    '<option value="' + c + '"' + (c === def ? " selected" : "") + '>' + l + '</option>').join("");
}
fillLangs($("sLang"), "en");

function fillVoices() {
  const list = VOICES[$("sLang").value] || [];
  $("sVoice").innerHTML = list.map(([l,v]) =>
    '<option value="' + v + '">' + l + '</option>').join("");
}
$("sLang").addEventListener("change", fillVoices); fillVoices();

$("sRate").addEventListener("input", () => {
  const v = +$("sRate").value;
  $("sRateVal").textContent = (v > 0 ? "+" : "") + v + "%";
});
$("sText").addEventListener("input", () => $("sCount").textContent = $("sText").value.length);

async function speak() {
  const text = $("sText").value.trim();
  if (!text) { setStatus($("sStatus"), "Add some text first.", "err"); return; }
  const b = $("sGo"); b.disabled = true; b.textContent = "Generating...";
  let secs = 0; setStatus($("sStatus"), "Generating audio... (0s)");
  const timer = setInterval(() => setStatus($("sStatus"), "Generating audio... (" + (++secs) + "s)"), 1000);
  try {
    const res = await fetch("/api/audio", { method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ text, lang:$("sLang").value, voice:$("sVoice").value, rate:+$("sRate").value }) });
    if (!res.ok) {
      const j = await res.json().catch(() => ({error:"Audio failed."}));
      setStatus($("sStatus"), j.error, "err"); return;
    }
    const url = URL.createObjectURL(await res.blob());
    $("sPlayer").src = url; $("sDl").href = url;
    $("sAudioWrap").classList.remove("hidden");
    setStatus($("sStatus"), "Audio ready.", "ok");
    $("sPlayer").play().catch(() => {});
  } catch (e) { setStatus($("sStatus"), "Audio error: " + e, "err"); }
  finally { clearInterval(timer); b.disabled = false; b.textContent = "Generate audio"; }
}
$("sGo").addEventListener("click", speak);

$("sTranslate").addEventListener("click", async () => {
  const text = $("sText").value.trim();
  if (!text) { setStatus($("sStatus"), "Add some text first.", "err"); return; }
  const b = $("sTranslate"); b.disabled = true;
  setStatus($("sStatus"), "Translating...");
  try {
    const res = await fetch("/api/text/translate", { method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ text, lang: $("sLang").value }) });
    const d = await res.json();
    if (!d.ok) { setStatus($("sStatus"), d.error, "err"); return; }
    $("sText").value = d.text;
    $("sCount").textContent = d.text.length;
    setStatus($("sStatus"), "Translated — edit if you like, then Generate audio.", "ok");
  } catch (e) { setStatus($("sStatus"), "Translation error: " + e, "err"); }
  finally { b.disabled = false; }
});

</script>
</body>
</html>"""


def _free_port(preferred: int) -> int:
    """Use the usual port, but don't fail if something else has it — a teammate
    shouldn't meet a stack trace because another app owns 5000."""
    import socket
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


if __name__ == "__main__":
    PORT = _free_port(int(os.environ.get("DUBSYNC_PORT", 5000)))
    URL = f"http://127.0.0.1:{PORT}"
    # Open the browser for them — "double-click, get a page" is the whole point of
    # the GUI. DUBSYNC_NO_BROWSER=1 opts out (scripted/headless runs).
    if os.environ.get("DUBSYNC_NO_BROWSER") != "1":
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(URL)).start()
    print(f"dubsync -> {URL}   (keep this window open; Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=PORT, debug=False)
