"""hinglish.py — produce natural spoken Hinglish for dub.py, locally.

Why this exists
---------------
dub.py's default translate step (Google Translate) produces accurate but *shuddh*
(formal, Sanskritised) Hindi. Real Indian YouTubers don't talk like that — they
speak Hinglish: Hindi grammar with the everyday English words mixed in
(बिज़नेस, ऑनलाइन, वीडियो, प्रॉब्लम...). This module gives the dub that spoken
register without changing the meaning, so it sounds like a person, not a textbook.

How it works — two layers, both offline
---------------------------------------
1. a deterministic loanword DICTIONARY swaps the common formal words for the
   English ones people say (no model, no VRAM, and the SAME word maps the same
   way every time — an LLM won't guarantee that);
2. local Gemma-3-4B cleans up the rest, ALL lines in ONE batched call so a
   100-sentence video is one round trip, not 100 (which on an 8 GB card is a
   VRAM/uptime gamble). Gemma only *rewrites* correct Hindi — it never
   translates from scratch, which is where small models invent words.

Free, private, unlimited; needs Ollama running (`ollama pull gemma3:4b`).

Why there is no API backend: a Sarvam `mode="code-mixed"` engine was built and
tested live, then REMOVED (2026-07-26). Its code-mix is too aggressive — it
transliterates words a Hindi speaker would keep in Hindi (एलिफ़ेंट्स, गाइज़,
कूल), which reads as parody rather than speech. The local Gemma restyle is more
natural, free, offline and unlimited, so it is the only path. Don't re-add an
API here without a listening test against Gemma first.

Robustness: failure degrades gracefully, per line — if Gemma is down or returns
the wrong shape we fall back to the dictionary-swapped Hindi. The dub is never
allowed to crash over *style*.

The entry point operates on dub.py's `groups` list in place, setting each
group's "tr" (the text that gets spoken) — so integration is a single call.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

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
# Public entry point — operates on dub.py's `groups` in place
# ============================================================================
def apply(groups: list[dict], *, model: str | None = None,
          host: str | None = None) -> None:
    """Set each group's spoken text ("tr") to Hinglish.

    Expects the groups to already carry Google-Hindi in "tr" (dub.py runs its
    translate step first) and the English original in "text" — the restyle uses
    the English as ground truth, so both are required.
    """
    if not groups:
        return
    out = restyle_offline([g["text"] for g in groups], [g["tr"] for g in groups],
                          model=model or "gemma3:4b", host=host)
    for g, t in zip(groups, out):
        g["tr"] = t


# ============================================================================
# self-test:  python hinglish.py
# ============================================================================
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")        # Windows console is cp1252
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
    print("\n--- full pipeline (dictionary + local Gemma) ---")
    apply(groups)
    for g in groups:
        print(g["tr"])
