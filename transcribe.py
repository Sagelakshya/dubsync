"""transcribe.py — turn a video's AUDIO into accurate, timestamped sentences.

Why this exists
---------------
YouTube auto-captions are error-prone and, worse, often fragment a sentence
mid-thought — which strips the context the translator needs ("long trunks" with
no "elephant" nearby -> चड्डी/underwear instead of सूंड). Transcribing the audio
ourselves with Whisper gives accurate text, real punctuation, and whole sentence
boundaries, so downstream translation gets clean, complete sentences. It also
works when a video has NO captions at all (which the caption path can't).

Uses faster-whisper (CTranslate2) — the same engine the Second Brain project
already runs on this machine, so no new heavyweight stack. Output matches
dub.py's caption format exactly — [{text, start, dur}, ...] — so it's a drop-in
replacement for fetch_cues().
"""
from __future__ import annotations

# Cache loaded models by (size, device) so a second call in the same process is free.
_MODELS: dict[tuple[str, str], object] = {}


def _cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _get_model(model_size: str, device: str):
    from faster_whisper import WhisperModel
    key = (model_size, device)
    if key not in _MODELS:
        compute = "float16" if device == "cuda" else "int8"
        _MODELS[key] = WhisperModel(model_size, device=device, compute_type=compute)
    return _MODELS[key]


def _run(media_path: str, model_size: str, lang: str | None, device: str) -> list[dict]:
    model = _get_model(model_size, device)
    # vad_filter drops long silences so segments hug real speech; language=None lets
    # Whisper auto-detect (we default to "en" since the tool dubs English sources).
    segments, _info = model.transcribe(media_path, language=lang, vad_filter=True,
                                       beam_size=5)
    cues: list[dict] = []
    for s in segments:                      # this generator is what actually runs the ASR
        text = (s.text or "").strip()
        if text:
            cues.append({"text": text, "start": float(s.start),
                         "dur": float(max(s.end - s.start, 0.0))})
    return cues


def transcribe(media_path: str, *, model_size: str = "small",
               lang: str | None = "en") -> list[dict]:
    """Transcribe an audio OR video file into timed sentence cues.

    faster-whisper decodes the audio track directly (via PyAV), so `media_path`
    may be a plain audio file or a full video — no separate extraction needed.
    Prefers the GPU; if CUDA/cuDNN isn't usable (common on Windows), it falls back
    to CPU rather than failing the whole dub.
    """
    if _cuda_available():
        try:
            return _run(media_path, model_size, lang, "cuda")
        except Exception as e:
            print(f"  [whisper] GPU path failed ({type(e).__name__}); using CPU.")
    return _run(media_path, model_size, lang, "cpu")


# quick check:  python transcribe.py <audio_or_video_file>
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        sys.exit("usage: python transcribe.py <audio_or_video_file> [model_size]")
    size = sys.argv[2] if len(sys.argv) > 2 else "small"
    cues = transcribe(sys.argv[1], model_size=size)
    print(f"{len(cues)} segments:")
    for c in cues[:20]:
        print(f"  [{c['start']:6.2f} +{c['dur']:4.1f}]  {c['text']}")
