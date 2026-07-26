"""hinglish.py — produce natural spoken Hinglish for dub.py, two ways.

Why this exists
---------------
dub.py's default translate step (Google Translate) produces accurate but *shuddh*
(formal, Sanskritised) Hindi. Real Indian YouTubers don't talk like that — they
speak Hinglish: Hindi grammar with the everyday English words mixed in
(बिज़नेस, ऑनलाइन, वीडियो, प्रॉब्लम...). This module gives the dub that spoken
register without changing the meaning, so it sounds like a person, not a textbook.

Two backends (Sage05 wanted both — one for his GPU box, one to hand a teammate)
------------------------------------------------------------------------------
* "ollama"  — OFFLINE. Keeps dub.py's Google-Hindi, then restyles it locally:
    1. a deterministic loanword DICTIONARY swaps the common formal words for the
       English ones people say (no model, no VRAM, and the SAME word maps the
       same way every time — an LLM won't guarantee that);
    2. local Gemma-3-4B cleans up the rest, ALL lines in ONE batched call so a
       100-sentence video is one round trip, not 100 (which on an 8 GB card is a
       VRAM/uptime gamble). Gemma only *rewrites* correct Hindi — it never
       translates from scratch, which is where small models invent words.
  Free, private, but needs Ollama + a GPU.

* "sarvam" — API. Sends the ENGLISH line straight to Sarvam's translate endpoint
  with mode="code-mixed" (a real, documented setting built for exactly this) and
  gets Hinglish back in one call. Best quality, needs no GPU — just
  SARVAM_API_KEY — so it's the portable option for a teammate's laptop. One call
  per sentence (threaded), which keeps every line locked to its own timestamp.

Robustness: any backend failure degrades gracefully, per line — the ollama path
falls back to the dictionary-swapped Hindi; the sarvam path falls back to plain
Google Hindi. The dub is never allowed to crash over *style*.

Both entry points operate on dub.py's `groups` list in place, setting each
group's "tr" (the text that gets spoken) — so integration is a single call.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# ============================================================================
# Layer 1 (offline path): deterministic loanword dictionary
# ============================================================================
# formal/Sanskritised Hindi (what Google emits)  ->  the English word Indians
# actually say, written in Devanagari (so the Hindi TTS voice pronounces it with
# the natural Indian-English accent). Conservative and curated: only unambiguous
# domain words — anything context-sensitive is left to the model. Extend freely;
# a new pair takes effect immediately, no retraining.
LOANWORDS: dict[str, str] = {
    # business / commerce
    "व्यवसाय": "बिज़नेस", "व्यापार": "बिज़नेस", "ग्राहक": "कस्टमर",
    "ग्राहकों": "कस्टमर्स", "उत्पाद": "प्रोडक्ट", "उत्पादों": "प्रोडक्ट्स",
    "सेवा": "सर्विस", "बाज़ार": "मार्केट", "कीमत": "प्राइस", "भुगतान": "पेमेंट",
    "खाता": "अकाउंट", "छूट": "डिस्काउंट", "मुनाफ़ा": "प्रॉफिट",
    # content / youtube
    "दर्शक": "ऑडियंस", "दर्शकों": "ऑडियंस", "विज्ञापन": "ऐड", "विज्ञापनों": "ऐड्स",
    "टिप्पणी": "कमेंट", "टिप्पणियाँ": "कमेंट्स", "समुदाय": "कम्युनिटी",
    "उपयोगी": "हेल्पफुल", "उदाहरण": "एग्ज़ाम्पल", "विशेषता": "फीचर",
    # tech / apps
    "अनुप्रयोग": "ऐप", "उपकरण": "टूल", "प्रणाली": "सिस्टम", "प्रक्रिया": "प्रोसेस",
    "संस्करण": "वर्ज़न", "सुविधा": "फीचर", "पृष्ठ": "पेज", "संपादन": "एडिटिंग",
    "जानकारी": "इन्फॉर्मेशन", "संदेश": "मैसेज", "सूची": "लिस्ट", "खोज": "सर्च",
    "सुरक्षा": "सिक्योरिटी",
    # generic register — the words that most make Hindi sound "textbook"
    "समस्या": "प्रॉब्लम", "समस्याओं": "प्रॉब्लम्स", "समाधान": "सोल्यूशन",
    "चरण": "स्टेप", "कदम": "स्टेप", "लक्ष्य": "गोल", "योजना": "प्लान",
    "रणनीति": "स्ट्रैटेजी", "परिणाम": "रिज़ल्ट", "अनुभव": "एक्सपीरियंस",
    "स्तर": "लेवल", "महत्वपूर्ण": "इम्पॉर्टेन्ट", "सरल": "सिंपल", "आसान": "ईज़ी",
    "उन्नत": "एडवांस्ड", "नवीनतम": "लेटेस्ट", "मुफ़्त": "फ्री", "सलाह": "टिप",
    "युक्ति": "ट्रिक",
}

# Match a key only as a standalone token — not glued to more Devanagari on either
# side. Conservative on purpose: swaps "व्यवसाय को" (space before को) but leaves
# an inflected "व्यवसायों" alone rather than risk corrupting it. Gemma mops those up.
_DEVA = r"ऀ-ॿ"
_KEYS = sorted(LOANWORDS, key=len, reverse=True)   # longest first for correct overlap
_DICT_RE = re.compile(
    r"(?<![" + _DEVA + r"])(" + "|".join(re.escape(k) for k in _KEYS) + r")(?![" + _DEVA + r"])"
)


def dict_pass(text: str) -> str:
    """Swap the formal words we have a fixed mapping for. Deterministic, instant."""
    return _DICT_RE.sub(lambda m: LOANWORDS[m.group(1)], text)


# ============================================================================
# Layer 2 (offline path): local Gemma restyle, batched into one call
# ============================================================================
# Gemma is given the English AND a rough Google-Hindi draft per line. The English
# is ground truth: this lets it CORRECT Google's word-sense errors (the elephant
# "trunks"->"चड्डी"/underwear bug that shipped in the first cut), not merely
# restyle. Rules 2-3 are the lessons from testing: from-scratch Gemma injected
# filler ("यार") and leaked romanisation, so we forbid both explicitly.
# Rules 1 & 2 are the hard-won ones. Rule 1 stops Gemma from *rewriting* a correct
# translation and flipping the meaning ("adopted BY graduates" -> "adopt graduates",
# a real regression we hit). Rule 2 lets it fix a wrong WORD (चड्डी->सूँड) without
# touching structure. The rest is the Hinglish register + the anti-echo guards.
_SYS = (
    "You turn each line into natural spoken Hinglish — Hindi grammar with the "
    "common English words Indians actually say, the way a YouTuber talks. Per "
    "line you get the English original and a rough Hindi draft.\n"
    "RULES (1 and 2 are absolute — never break them for style):\n"
    "1. NEVER change who does what to whom. Keep the exact subject, object, and "
    "DIRECTION of every action from the English. If someone is adopted BY others, "
    "they are NOT the one adopting. Do not flip, add, or drop meaning.\n"
    "2. You MAY fix a clearly wrong WORD in the draft using the English as truth "
    "(an elephant's 'trunk' is सूँड, not चड्डी) — but fix only the wrong word, "
    "never restructure the sentence.\n"
    "3. Make it sound SPOKEN, not textbook: swap stiff/Sanskritised words for the "
    "everyday English word written in Devanagari (व्यवसाय→बिज़नेस, वास्तव में→सच में, "
    "बढ़ा→ग्रो, कृपया→प्लीज़), and use वो/ये rather than वह/यह.\n"
    "4. Do NOT add greetings, filler, or words like 'यार' that aren't in the English.\n"
    "5. Write ONLY Devanagari — no Latin letters, no romanisation in brackets.\n"
    "6. Return the SAME line numbers, one rewritten line each, same count, "
    "and NOTHING else."
)

# Collapse junk before it reaches a model or the TTS: a caption like
# "(baaaaaaaa...hh!!)" arrives as a 200-char run that bloats the prompt and makes
# the voice read gibberish. Squash any char repeated 4+ times down to 2.
_RUN_RE = re.compile(r"(.)\1{3,}")


def _collapse_runs(text: str) -> str:
    return _RUN_RE.sub(r"\1\1", text)


def _number_pairs(english: list[str], hindi: list[str]) -> str:
    return "\n".join(f"{i+1}. EN: {e} | HI: {h}"
                     for i, (e, h) in enumerate(zip(english, hindi)))


_NUM_RE = re.compile(r"^\s*(\d+)[.)]\s*(.*)$")


def _parse_numbered(reply: str, n: int) -> list[str] | None:
    """Recover lines by their number. None if the shape is wrong (missing/extra),
    so the caller can fall back safely instead of shipping a misaligned dub."""
    got: dict[int, str] = {}
    for raw in reply.splitlines():
        m = _NUM_RE.match(raw)
        if m:
            content = m.group(2).strip()
            # The model sometimes echoes our "EN: ... | HI:" scaffold back. Keep
            # only what follows the last "HI:" so the label never reaches TTS.
            if "HI:" in content:
                content = content.rsplit("HI:", 1)[-1].strip()
            got[int(m.group(1))] = content
    if len(got) != n or set(got) != set(range(1, n + 1)):
        return None
    return [got[i] for i in range(1, n + 1)]


def _call_ollama(english: list[str], hindi: list[str], model: str,
                 host: str) -> list[str] | None:
    body = json.dumps({
        "model": model,
        "stream": False,
        "options": {"temperature": 0},   # deterministic: same input -> same output
        "messages": [
            {"role": "system", "content": _SYS},
            {"role": "user", "content": "Rewrite each line:\n" + _number_pairs(english, hindi)},
        ],
    }).encode()
    req = urllib.request.Request(host.rstrip("/") + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            reply = json.load(r)["message"]["content"]
    except Exception:
        return None
    return _parse_numbered(reply, len(hindi))


def restyle_offline(english_lines: list[str], hindi_lines: list[str], *,
                    model: str = "gemma3:4b", host: str | None = None) -> list[str]:
    """Dictionary swap + one batched Gemma pass that uses the English as ground
    truth to FIX Google's mistranslations (not just restyle). Falls back to the
    dictionary-swapped Hindi if Gemma is down or returns the wrong shape."""
    if not hindi_lines:
        return hindi_lines
    english = [_collapse_runs(e) for e in english_lines]
    swapped = [_collapse_runs(dict_pass(t)) for t in hindi_lines]
    host = host or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    out = _call_ollama(english, swapped, model, host)
    if out is None:
        print("  [hinglish] Gemma unavailable/failed — using dictionary-only "
              "Hinglish (accurate, slightly formal).")
        return swapped
    # Per-line guard: keep Gemma's line only if it's non-empty AND actually in
    # Devanagari — if it echoed English (no Devanagari), fall back to the draft.
    return [o if (o.strip() and re.search(r"[ऀ-ॿ]", o)) else s
            for o, s in zip(out, swapped)]


# ============================================================================
# Sarvam API backend: English -> Hinglish directly (mode="code-mixed")
# ============================================================================
_SARVAM_URL = "https://api.sarvam.ai/translate"


def _call_sarvam(text: str, api_key: str, model: str, mode: str,
                 output_script: str) -> str | None:
    """One /translate call. Per Sarvam's contract: mayura:v1 max 1000 chars,
    sarvam-translate:v1 max 2000. Returns translated_text, or None on failure."""
    body = json.dumps({
        "input": text[:1000] if model == "mayura:v1" else text[:2000],
        "source_language_code": "auto" if model == "mayura:v1" else "en-IN",
        "target_language_code": "hi-IN",
        "mode": mode,                     # "code-mixed" == Hinglish
        "model": model,
        "output_script": output_script,   # "fully-native" -> Devanagari for the TTS voice
    }).encode()
    req = urllib.request.Request(_SARVAM_URL, data=body, headers={
        "Content-Type": "application/json", "api-subscription-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["translated_text"]
    except Exception:
        return None


def translate_sarvam(english_lines: list[str], *, api_key: str,
                     model: str = "mayura:v1", mode: str = "code-mixed",
                     output_script: str = "fully-native") -> list[str]:
    """English -> Hinglish via Sarvam, one threaded call per sentence (keeps every
    line locked to its timestamp). If a line fails, fall back to Google Hindi for
    that line so the dub still speaks Hindi, never raw English."""
    if not english_lines:
        return english_lines
    english_lines = [_collapse_runs(e) for e in english_lines]

    def one(text: str) -> str:
        r = _call_sarvam(text, api_key, model, mode, output_script)
        if r:
            return r
        try:                              # per-line safety net: plain Google Hindi
            from deep_translator import GoogleTranslator
            return GoogleTranslator(source="auto", target="hi").translate(text) or text
        except Exception:
            return text

    with ThreadPoolExecutor(max_workers=4) as pool:   # modest: respect API rate limits
        return list(pool.map(one, english_lines))


# ============================================================================
# Public entry point — operates on dub.py's `groups` in place
# ============================================================================
def apply(groups: list[dict], *, engine: str = "ollama", model: str | None = None,
          host: str | None = None, api_key: str | None = None,
          mode: str = "code-mixed", output_script: str = "fully-native") -> None:
    """Set each group's spoken text ("tr") to Hinglish.

    engine="ollama": requires groups already carry Google-Hindi in "tr"
                     (dub.py runs its translate step first); restyles that.
    engine="sarvam": uses each group's original English "text"; dub.py should
                     SKIP its Google translate step for this engine.
    """
    if not groups:
        return
    if engine == "ollama":
        out = restyle_offline([g["text"] for g in groups], [g["tr"] for g in groups],
                              model=model or "gemma3:4b", host=host)
    elif engine == "sarvam":
        key = api_key or os.environ.get("SARVAM_API_KEY")
        if not key:
            raise SystemExit(
                "hinglish engine 'sarvam' needs an API key: set SARVAM_API_KEY "
                "(get one free at dashboard.sarvam.ai — ~65 short videos of free "
                "credit) or pass --sarvam-key.")
        out = translate_sarvam([g["text"] for g in groups], api_key=key,
                               model=model or "mayura:v1", mode=mode,
                               output_script=output_script)
    else:
        raise ValueError(f"unknown hinglish engine '{engine}' (use ollama|sarvam)")
    for g, t in zip(groups, out):
        g["tr"] = t


# ============================================================================
# self-test:  python hinglish.py [ollama|sarvam]
# ============================================================================
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")        # Windows console is cp1252
    eng = sys.argv[1] if len(sys.argv) > 1 else "ollama"
    # groups mimic dub.py: "text" = original English, "tr" = Google Hindi.
    groups = [
        {"text": "Today I want to talk about how you can grow your business online without spending too much money on ads.",
         "tr": "आज मैं इस बारे में बात करना चाहता हूं कि आप विज्ञापनों पर बहुत अधिक पैसा खर्च किए बिना अपना व्यवसाय ऑनलाइन कैसे बढ़ा सकते हैं।"},
        {"text": "So the first step is to understand your audience and the problem you are solving.",
         "tr": "तो पहला कदम अपने दर्शकों को समझना है और वह समस्या जो आप हल कर रहे हैं।"},
        {"text": "If you found this video helpful, please like and subscribe.",
         "tr": "अगर आपको यह वीडियो उपयोगी लगा, तो कृपया इसे लाइक और सब्सक्राइब करें।"},
    ]
    print("--- dictionary pass only ---")
    for g in groups:
        print(dict_pass(g["tr"]))
    print(f"\n--- full pipeline (engine={eng}) ---")
    apply(groups, engine=eng)
    for g in groups:
        print(g["tr"])
