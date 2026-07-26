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

Long audio is transcribed in CHUNKS. faster-whisper builds the mel spectrogram for
the whole file in one go, as a complex128 array — a 20-minute talk asks for ~344 MB
in a single allocation and dies with MemoryError on any machine that is even
moderately busy (found the hard way on a 20-min TED talk). Cutting the audio into
5-minute pieces bounds that to well under 100 MB and costs nothing in accuracy,
because each cut is snapped to the quietest moment nearby rather than falling
mid-word.
"""
from __future__ import annotations

SR = 16000                  # Whisper's sample rate; everything here is in these samples
CHUNK_SECONDS = 300.0       # 5 minutes per pass — small enough for a loaded laptop
SNAP_SECONDS = 10.0         # how far to hunt around a cut for a quiet moment

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


def release() -> None:
    """Drop the cached model and free its memory.

    Worth calling as soon as transcription is done: the rest of a dub (the local
    Hinglish model, then ffmpeg) wants that memory, and in the web GUI the server
    process outlives the job — without this, a model stays resident until the app
    is closed. The cost is reloading it on the next dub, which is seconds.
    """
    import gc
    _MODELS.clear()
    gc.collect()


def _quiet_cut(audio, target: int, search: int) -> int:
    """Move a cut point to the quietest 20 ms frame within `search` samples of it,
    so a chunk boundary lands in a pause instead of slicing a word in half."""
    import numpy as np
    lo, hi = max(0, target - search), min(len(audio), target + search)
    window = audio[lo:hi]
    frame = SR // 50                                    # 20 ms
    n = len(window) // frame
    if n < 3:
        return target
    rms = np.sqrt((window[:n * frame].reshape(n, frame).astype("float32") ** 2)
                  .mean(axis=1))
    return lo + int(rms.argmin()) * frame


def _chunks(audio, chunk_samples: int) -> list[tuple[int, int]]:
    """[(start, end)] sample ranges covering the audio, cut at quiet moments."""
    if len(audio) <= chunk_samples * 1.2:               # short: don't bother splitting
        return [(0, len(audio))]
    bounds, pos = [0], 0
    while len(audio) - pos > chunk_samples * 1.2:
        pos = _quiet_cut(audio, pos + chunk_samples, int(SNAP_SECONDS * SR))
        bounds.append(pos)
    bounds.append(len(audio))
    return list(zip(bounds, bounds[1:]))


def _is_oom(e: Exception) -> bool:
    """Out-of-memory arrives under several names here: numpy raises MemoryError,
    but CTranslate2's allocator surfaces as RuntimeError('mkl_malloc: failed to
    allocate memory') and oneDNN/CUDA have their own wording. Treat them alike —
    what matters is that a smaller piece might still fit."""
    if isinstance(e, MemoryError):
        return True
    text = str(e).lower()
    return any(k in text for k in ("malloc", "out of memory", "bad_alloc",
                                   "allocate memory", "cannot allocate"))


def _transcribe_piece(model, audio, lang: str | None) -> list[dict]:
    """One pass of the ASR. If it runs out of memory, halve the piece and retry —
    a busy machine can fail an allocation that a smaller one would satisfy, and
    half a chunk is always cheaper than losing the whole dub."""
    try:
        # vad_filter drops long silences so segments hug real speech; language=None
        # lets Whisper auto-detect (we default to "en" since sources are English).
        segments, _info = model.transcribe(audio, language=lang, vad_filter=True,
                                           beam_size=5)
        out = []
        for s in segments:                  # this generator is what runs the ASR
            text = (s.text or "").strip()
            if text:
                out.append({"text": text, "start": float(s.start),
                            "dur": float(max(s.end - s.start, 0.0))})
        return out
    except Exception as e:
        if not _is_oom(e):
            raise
        if len(audio) < 30 * SR:
            # Down to 30 seconds and still short of memory: no amount of splitting
            # fixes that, so say what will — a cryptic mkl_malloc helps nobody.
            raise MemoryError(
                "Not enough free memory to transcribe. Close some apps (a browser "
                "with many tabs is usually the culprit), or use a smaller model "
                "(--whisper-model tiny or base)."
            ) from e
        mid = _quiet_cut(audio, len(audio) // 2, int(SNAP_SECONDS * SR))
        print("  [whisper] low memory — splitting this piece and retrying.")
        first = _transcribe_piece(model, audio[:mid], lang)
        second = _transcribe_piece(model, audio[mid:], lang)
        shift = mid / SR
        return first + [{**c, "start": c["start"] + shift} for c in second]


def _run(audio, model_size: str, lang: str | None, device: str,
         on_progress=None) -> list[dict]:
    model = _get_model(model_size, device)
    pieces = _chunks(audio, int(CHUNK_SECONDS * SR))
    cues: list[dict] = []
    for i, (start, end) in enumerate(pieces, 1):
        if on_progress and len(pieces) > 1:
            on_progress(i, len(pieces))
        shift = start / SR                              # chunk-local -> whole-file time
        cues += [{**c, "start": c["start"] + shift}
                 for c in _transcribe_piece(model, audio[start:end], lang)]
    return cues


def transcribe(media_path: str, *, model_size: str = "small",
               lang: str | None = "en", on_progress=None) -> list[dict]:
    """Transcribe an audio OR video file into timed sentence cues.

    `media_path` may be a plain audio file or a full video — the audio track is
    decoded here (PyAV), once, then transcribed in chunks so memory stays flat
    whatever the length. `on_progress(done, total)` reports chunks, since this is
    the slowest step of a dub and a long video otherwise looks stalled.
    Prefers the GPU; if CUDA/cuDNN isn't usable (common on Windows), it falls back
    to CPU rather than failing the whole dub.
    """
    from faster_whisper.audio import decode_audio
    audio = decode_audio(media_path, sampling_rate=SR)
    if _cuda_available():
        try:
            return _run(audio, model_size, lang, "cuda", on_progress)
        except Exception as e:
            print(f"  [whisper] GPU path failed ({type(e).__name__}); using CPU.")
    return _run(audio, model_size, lang, "cpu", on_progress)


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
