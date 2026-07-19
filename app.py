"""YouTube transcript -> translation, as a tiny local web app.

Paste a YouTube link, pick a language, get the captions translated.
Captions only (no audio download / Whisper) -> stays light and dependency-free.

Run:   python app.py      then open http://127.0.0.1:5000
Needs: pip install flask youtube-transcript-api deep-translator
"""
import re
import asyncio
from urllib.parse import urlparse, parse_qs

from flask import Flask, request, jsonify, render_template_string, Response

app = Flask(__name__)

# Languages offered in the dropdown -> (label, GoogleTranslator code).
LANGUAGES = [
    ("Spanish", "es"), ("French", "fr"), ("German", "de"),
    ("Hindi", "hi"), ("Bengali", "bn"), ("Urdu", "ur"),
    ("Tamil", "ta"), ("Telugu", "te"), ("Arabic", "ar"),
    ("Chinese (Simplified)", "zh-CN"), ("Japanese", "ja"),
    ("Korean", "ko"), ("Russian", "ru"), ("Portuguese", "pt"),
    ("Italian", "it"), ("English", "en"),
]

# Read-aloud voices (edge-tts) keyed by the same codes as LANGUAGES above.
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


# --- pull the transcript (supports old + new library APIs) ---------------
def get_transcript(vid: str) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi
    if hasattr(YouTubeTranscriptApi, "get_transcript"):      # classic API
        data = YouTubeTranscriptApi.get_transcript(vid)
        return " ".join(d["text"] for d in data).strip()
    fetched = YouTubeTranscriptApi().fetch(vid)              # newer API
    return " ".join(s.text for s in fetched).strip()


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
async def _synth_chunk(text: str, voice: str) -> bytes:
    import edge_tts
    buf = bytearray()
    async for part in edge_tts.Communicate(text, voice).stream():
        if part["type"] == "audio":
            buf.extend(part["data"])
    return bytes(buf)


def synthesize(text: str, lang: str) -> bytes:
    voice = VOICES.get(lang, VOICES["en"])
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
                return await _synth_chunk(c, voice)

        audio_parts = await asyncio.gather(*(one(c) for c in parts))
        return b"".join(audio_parts)

    return asyncio.run(run())


@app.get("/")
def index():
    return render_template_string(PAGE, languages=LANGUAGES)


@app.post("/api/translate")
def api_translate():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "")
    lang = body.get("lang", "es")

    vid = yt_id(url)
    if not vid:
        return jsonify(ok=False, error="That doesn't look like a YouTube link."), 400
    try:
        original = get_transcript(vid)
    except Exception as e:
        return jsonify(ok=False,
                       error=f"Couldn't get captions for this video ({e}). "
                             "It may have captions disabled."), 400
    if not original:
        return jsonify(ok=False, error="This video has no captions to translate."), 400
    try:
        translated = translate(original, lang)
    except Exception as e:
        return jsonify(ok=False, error=f"Translation failed ({e})."), 500
    return jsonify(ok=True, original=original, translated=translated)


@app.post("/api/audio")
def api_audio():
    body = request.get_json(silent=True) or {}
    text = body.get("text", "")
    lang = body.get("lang", "en")
    if not text.strip():
        return jsonify(ok=False, error="No text to read aloud."), 400
    try:
        audio = synthesize(text, lang)
    except Exception as e:
        return jsonify(ok=False, error=f"Audio generation failed ({e})."), 500
    return Response(audio, mimetype="audio/mpeg",
                    headers={"Content-Disposition": "inline; filename=translation.mp3"})


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YouTube Transcript Translator</title>
<style>
  :root { --bg:#0f1115; --card:#181b22; --line:#2a2f3a; --fg:#e8eaed;
          --muted:#9aa3b2; --accent:#6c8cff; }
  * { box-sizing:border-box; }
  body { margin:0; font:16px/1.6 system-ui,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--fg); }
  .wrap { max-width:880px; margin:0 auto; padding:32px 20px 80px; }
  h1 { font-size:1.5rem; margin:0 0 4px; }
  p.sub { color:var(--muted); margin:0 0 24px; }
  .controls { display:flex; gap:10px; flex-wrap:wrap; align-items:center;
              background:var(--card); border:1px solid var(--line);
              border-radius:12px; padding:14px; }
  input, select { background:#0f1115; color:var(--fg); border:1px solid var(--line);
                  border-radius:8px; padding:11px 12px; font-size:15px; }
  input { flex:1 1 320px; }
  button { background:var(--accent); color:#fff; border:0; border-radius:8px;
           padding:11px 20px; font-size:15px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.55; cursor:default; }
  .status { margin:18px 2px; color:var(--muted); min-height:22px; }
  .status.err { color:#ff8080; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:8px; }
  @media (max-width:680px){ .grid{ grid-template-columns:1fr; } }
  .panel { background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:16px; }
  .panel h2 { font-size:.8rem; text-transform:uppercase; letter-spacing:.06em;
              color:var(--muted); margin:0 0 10px; display:flex;
              justify-content:space-between; align-items:center; }
  .panel .text { white-space:pre-wrap; max-height:60vh; overflow:auto; font-size:15px; }
  .copy { background:transparent; color:var(--accent); border:1px solid var(--line);
          padding:5px 10px; font-size:12px; font-weight:600; }
  .hidden { display:none; }
</style>
</head>
<body>
<div class="wrap">
  <h1>YouTube Transcript Translator</h1>
  <p class="sub">Paste a YouTube link, pick a language, get the captions translated.</p>

  <div class="controls">
    <input id="url" type="text" placeholder="https://www.youtube.com/watch?v=..." />
    <select id="lang">
      {% for label, code in languages %}<option value="{{ code }}">{{ label }}</option>{% endfor %}
    </select>
    <button id="go">Translate</button>
  </div>

  <div id="status" class="status"></div>

  <div id="results" class="grid hidden">
    <div class="panel">
      <h2>Original <button class="copy" data-target="original">Copy</button></h2>
      <div id="original" class="text"></div>
    </div>
    <div class="panel">
      <h2>Translated
        <span>
          <button class="copy" data-target="translated">Copy</button>
          <button class="copy" id="ttsBtn">&#128266; Read aloud</button>
        </span>
      </h2>
      <div id="translated" class="text"></div>
    </div>
  </div>

  <div id="audioWrap" class="hidden" style="margin-top:16px;">
    <audio id="player" controls style="width:100%;"></audio>
    <a id="dl" download="translation.mp3" class="copy"
       style="display:inline-block;margin-top:8px;text-decoration:none;">Download .mp3</a>
  </div>

  <hr style="border:0;border-top:1px solid var(--line);margin:40px 0 24px;">

  <h1 style="font-size:1.25rem;">Or paste your own script</h1>
  <p class="sub">Type or paste any text and turn it straight into speech &mdash; no video needed.</p>

  <div class="panel">
    <textarea id="scriptText" rows="6" placeholder="Paste your script here..."
      style="width:100%;background:#0f1115;color:var(--fg);border:1px solid var(--line);
             border-radius:8px;padding:12px;font:inherit;resize:vertical;"></textarea>
    <div style="display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap;">
      <select id="scriptLang">
        {% for label, code in languages %}<option value="{{ code }}">{{ label }}</option>{% endfor %}
      </select>
      <button id="scriptBtn">&#128266; Generate audio</button>
    </div>
  </div>

  <div id="scriptAudioWrap" class="hidden" style="margin-top:16px;">
    <audio id="scriptPlayer" controls style="width:100%;"></audio>
    <a id="scriptDl" download="script.mp3" class="copy"
       style="display:inline-block;margin-top:8px;text-decoration:none;">Download .mp3</a>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const btn = $("go"), status = $("status"), results = $("results");

async function run() {
  const url = $("url").value.trim();
  const lang = $("lang").value;
  if (!url) { return; }
  btn.disabled = true; status.className = "status";
  status.textContent = "Fetching captions and translating...";
  results.classList.add("hidden");
  try {
    const res = await fetch("/api/translate", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ url, lang })
    });
    const data = await res.json();
    if (!data.ok) { status.className = "status err"; status.textContent = data.error; return; }
    $("original").textContent = data.original;
    $("translated").textContent = data.translated;
    results.classList.remove("hidden");
    $("audioWrap").classList.add("hidden");   // reset audio for the new text
    status.textContent = "Done.";
  } catch (e) {
    status.className = "status err"; status.textContent = "Something went wrong: " + e;
  } finally { btn.disabled = false; }
}

btn.addEventListener("click", run);
$("url").addEventListener("keydown", e => { if (e.key === "Enter") run(); });
document.querySelectorAll(".copy[data-target]").forEach(b =>
  b.addEventListener("click", () => navigator.clipboard.writeText($(b.dataset.target).textContent))
);

async function generateAudio({ text, lang, btn, wrap, player, dl }) {
  text = (text || "").trim();
  if (!text) { status.className = "status err"; status.textContent = "Nothing to read — add some text first."; return; }
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Generating...";
  status.className = "status";
  let secs = 0;
  status.textContent = "Generating audio... (0s)";
  const timer = setInterval(() => {
    secs++; status.textContent = "Generating audio... (" + secs + "s)";
  }, 1000);
  try {
    const res = await fetch("/api/audio", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ text, lang })
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({error:"Audio failed."}));
      status.className = "status err"; status.textContent = j.error; return;
    }
    const url = URL.createObjectURL(await res.blob());
    player.src = url; dl.href = url;
    wrap.classList.remove("hidden");
    status.textContent = "Audio ready.";
    player.play().catch(() => {});
  } catch (e) {
    status.className = "status err"; status.textContent = "Audio error: " + e;
  } finally { clearInterval(timer); btn.disabled = false; btn.textContent = label; }
}

$("ttsBtn").addEventListener("click", () => generateAudio({
  text: $("translated").textContent, lang: $("lang").value,
  btn: $("ttsBtn"), wrap: $("audioWrap"), player: $("player"), dl: $("dl")
}));

$("scriptBtn").addEventListener("click", () => generateAudio({
  text: $("scriptText").value, lang: $("scriptLang").value,
  btn: $("scriptBtn"), wrap: $("scriptAudioWrap"), player: $("scriptPlayer"), dl: $("scriptDl")
}));

$("scriptLang").value = "en";   // sensible default for a typed-in script
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
