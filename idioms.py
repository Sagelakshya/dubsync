"""idioms.py — replace an English idiom with a real HINDI idiom, deterministically.

Why this exists
---------------
Machine translation is confidently literal about figurative language. Given a
complete, well-formed sentence, Google gets grammar and word sense right, but it
still renders "you can see the blood run from their face" as people visibly
BLEEDING, "brownie points" as ब्राउनी पॉइंट, and "old chestnut" as पुराना चेस्टनट.
A listener doesn't hear a translation artifact, they hear a sentence that makes
no sense.

The fix is NOT to flatten the idiom into plain language first ("they go pale").
That is accurate and reads like a textbook. A Hindi speaker says
चेहरे का रंग उड़ जाना. So this maps an English idiom to *the Hindi idiom used in
the same situation* — never to its literal words, and never to a paraphrase.

How it works — mask, translate, splice
--------------------------------------
The mapping is English->Hindi but the translator sits in between, so we never
try to find the literal artifact in the Hindi output and patch it (Google's
literal rendering varies by sentence, so that hunt is unreliable). Instead:

1. **mark()**  — detect idioms in the ENGLISH and swap each for `__IDIOMn__`.
2. *translate* — the caller's own translate function runs on the masked text.
   Google carries the placeholder through untouched and builds the Hindi frame
   around it.
3. **splice()** — put the Hindi idiom into the slot the placeholder left.

Detection happens once, on the English, and produces a `Mark` per hit. That tag
is the unit every later stage uses: it drives the mask, it lets a downstream
restyle pass be checked for having eaten the idiom, and it records what was
replaced so a transcript view can show it.

Measured design decisions (probes against the live Google endpoint)
-------------------------------------------------------------------
* `__IDIOMn__` is the placeholder because it survives translation intact and,
  unlike a bare alphanumeric token like `ZQXW1`, it does NOT attract a spurious
  Hindi case-marker. `ZQXW1` reads to the translator as a proper noun and comes
  back as "ZQXW1 को देख सकते हैं"; a stray को in front of a spliced verb phrase
  is broken Hindi. Multiple numbered masks in one sentence keep their numbers.
* **Prefer a span that carries its own grammar.** The one real failure mode is
  agreement: Google inflects the words AROUND the hole, and with no noun to agree
  with it assumes masculine-singular. So a feminine replacement breaks its frame
  ("सभी शाबाशी किसे *मिलते* हैं" — शाबाशी is feminine, needs मिलती). Two ways out,
  both used below: mask the whole clause so nothing outside needs to agree
  (`"span": "clause"`), or, when a short phrase entry is worth having for reuse,
  choose a MASCULINE Hindi equivalent, because masculine is what the translator
  already assumes (high-water mark -> सर्वोच्च शिखर, not पराकाष्ठा).
  `verify.py` exists so this is caught by a script and not by a listener.

Reusable on purpose
-------------------
Nothing here imports dubsync. It is a plain EN->HI asset: the dictionary is a
JSON data file (add entries without touching code, no commit needed to grow it)
and the API takes text and a translate callable. Use it for subtitles, copy, or
any EN->HI work, or run it from the command line.

    import idioms
    hi = idioms.translate_text(en, lambda s: my_translator(s))
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

# The placeholder shape is load-bearing, not cosmetic — see the module docstring.
# Underscore-flanked so the translator treats it as opaque punctuation-ish filler
# rather than as a proper noun it should case-mark.
MASK = "__IDIOM{}__"
_MASK_RE = re.compile(r"__IDIOM(\d+)__")

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "data", "idioms_en_hi.json")


@dataclass
class Entry:
    """One idiom mapping, as authored in the JSON."""
    id: str
    en: list[str]                 # English surface forms that should all map here
    hi: str                       # the Hindi idiom that replaces them
    span: str = "phrase"          # "phrase" | "clause" (see docstring: agreement)
    sense: str = ""               # plain-English meaning, for whoever edits this
    sample: str = ""              # a real sentence, used by verify.py
    source: str = ""              # where the example came from


@dataclass
class Mark:
    """One detected idiom occurrence — the tag that every later stage uses."""
    n: int                        # 1-based; matches __IDIOMn__
    entry_id: str
    en: str                       # the exact English text that was replaced
    hi: str                       # the Hindi idiom to splice in
    start: int                    # offset in the ORIGINAL english text
    end: int

    @property
    def mask(self) -> str:
        return MASK.format(self.n)


class IdiomData:
    """The loaded dictionary plus its compiled matcher.

    Kept as an object rather than module globals so a caller can load a different
    data file (another domain, another target language) without patching state.
    """

    def __init__(self, entries: list[Entry]):
        self.entries = entries
        self._by_id = {e.id: e for e in entries}
        # (compiled regex, entry) sorted so the LONGEST surface form is tried
        # first. This is what makes a clause entry win over a phrase entry that
        # sits inside it: "who gets all the brownie points" must beat the bare
        # "brownie points", because the clause form is the one with correct
        # agreement.
        pairs: list[tuple[str, Entry]] = []
        for e in entries:
            for surface in e.en:
                pairs.append((surface, e))
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        self._matchers = [(_compile_surface(s), e) for s, e in pairs]

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, entry_id: str) -> Entry | None:
        return self._by_id.get(entry_id)

    # -- step 1: detect on the English and tag ------------------------------
    def mark(self, text: str) -> tuple[str, list[Mark]]:
        """Find idioms in `text` and replace each with `__IDIOMn__`.

        Returns the masked English and the tags. Matches never overlap: the first
        (longest) match claims its span and later ones must fall outside it.
        """
        if not text or not self._matchers:
            return text, []

        claimed: list[tuple[int, int]] = []
        hits: list[tuple[int, int, Entry, str]] = []
        for rx, entry in self._matchers:
            for m in rx.finditer(text):
                s, e = m.start(), m.end()
                if any(s < ce and e > cs for cs, ce in claimed):
                    continue          # overlaps something already taken
                claimed.append((s, e))
                hits.append((s, e, entry, m.group(0)))

        if not hits:
            return text, []

        # Number the masks in reading order so __IDIOM1__ is the first idiom in
        # the sentence. Purely for legibility when a human reads a masked line.
        hits.sort(key=lambda h: h[0])
        marks: list[Mark] = []
        out: list[str] = []
        prev = 0
        for i, (s, e, entry, matched) in enumerate(hits, 1):
            marks.append(Mark(n=i, entry_id=entry.id, en=matched, hi=entry.hi,
                              start=s, end=e))
            out.append(text[prev:s])
            out.append(MASK.format(i))
            prev = e
        out.append(text[prev:])
        return "".join(out), marks


def _compile_surface(surface: str) -> re.Pattern:
    """Build a tolerant matcher for one English surface form.

    Tolerant about the things that legitimately vary in a transcript and would
    otherwise cause a silent miss:
      * any run of whitespace where the entry has a space (line-wrapped captions);
      * straight vs curly apostrophes (Whisper emits both);
      * case.
    Deliberately NOT tolerant about word order or morphology — an entry means the
    exact phrase it spells. List the variants explicitly instead of guessing, so
    that what matches stays inspectable.
    """
    parts = [re.escape(w) for w in surface.split()]
    body = r"\s+".join(parts)
    body = body.replace(r"\'", "['’]").replace("'", "['’]")
    # \b only where the edge is actually a word character; an entry may start or
    # end on punctuation, and \b next to punctuation would never match.
    left = r"\b" if surface[:1].isalnum() else ""
    right = r"\b" if surface[-1:].isalnum() else ""
    return re.compile(left + body + right, re.IGNORECASE)


# ============================================================================
# loading
# ============================================================================
_cache: IdiomData | None = None


def load(path: str | None = None, *, reload: bool = False) -> IdiomData:
    """Load the dictionary. Cached, because a dub calls this once per sentence."""
    global _cache
    if _cache is not None and path is None and not reload:
        return _cache
    p = path or _DATA
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        # An absent data file means "no idioms configured", not a crash. This has
        # to stay importable in a packaged build where the file may be trimmed.
        data = IdiomData([])
    else:
        data = IdiomData([Entry(**e) for e in raw.get("entries", [])])
    if path is None:
        _cache = data
    return data


# ============================================================================
# step 3: splice the Hindi back in
# ============================================================================
def splice(translated: str, marks: list[Mark]) -> tuple[str, list[Mark]]:
    """Replace each `__IDIOMn__` in the translated text with its Hindi idiom.

    Returns `(text, lost)` where `lost` is any mark whose placeholder did not
    survive translation. The caller MUST handle a non-empty `lost`: a dropped
    mask means the sentence now has a hole where its idiom should be, which is
    worse than an over-literal translation. Reported rather than patched over,
    because quietly shipping a damaged line is the exact failure mode this
    project already fixed once in the Hinglish pass.
    """
    if not marks:
        return translated, []
    out = translated
    lost: list[Mark] = []
    for mk in marks:
        if mk.mask in out:
            out = out.replace(mk.mask, mk.hi)
        else:
            lost.append(mk)
    # Strip any placeholder we didn't have a mark for, so a stray token can never
    # reach a text-to-speech voice and get read aloud as "underscore idiom one".
    out = _MASK_RE.sub("", out).replace("  ", " ").strip()
    return out, lost


def survived(text: str, marks: list[Mark]) -> list[Mark]:
    """Which marks are NOT present in `text` any more.

    For guarding a stage that runs AFTER the splice (dubsync's Gemma restyle):
    the idiom was put in deliberately, so if a later rewrite dropped it we want
    to know rather than assume. Measured at 7/7 survival with gemma3:4b, so this
    is cheap insurance, not a hot path.
    """
    return [mk for mk in marks if mk.hi not in text]


# ============================================================================
# convenience: the whole cycle for one string
# ============================================================================
def translate_text(text: str, translate_fn, *, data: IdiomData | None = None
                   ) -> str:
    """mask -> translate -> splice, for a single string.

    `translate_fn` is any callable taking English and returning Hindi, so this
    works with deep_translator, an API client, or a stub in a test. If a
    placeholder goes missing we re-translate the ORIGINAL text unmasked: that
    gives the plain (over-literal) Hindi rather than a sentence with a hole.
    """
    data = data or load()
    masked, marks = data.mark(text)
    if not marks:
        return translate_fn(text)
    out, lost = splice(translate_fn(masked), marks)
    if lost:
        return translate_fn(text)
    return out


# ============================================================================
# CLI:  python idioms.py "some english text"     |     python idioms.py --list
# ============================================================================
def _main(argv: list[str]) -> int:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")     # Windows console is cp1252
    data = load()

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip().splitlines()[0])
        print("\nusage:")
        print('  python idioms.py "english text"   mask, translate, splice (needs deep_translator)')
        print('  python idioms.py --mark "text"    show the masked English + tags only')
        print("  python idioms.py --list           list every entry")
        return 0

    if argv[0] == "--list":
        print(f"{len(data)} entries in {_DATA}\n")
        for e in sorted(data.entries, key=lambda x: x.id):
            print(f"  {e.id}  [{e.span}]")
            print(f"    en   : {' | '.join(e.en)}")
            print(f"    hi   : {e.hi}")
            if e.sense:
                print(f"    sense: {e.sense}")
        return 0

    if argv[0] == "--mark":
        masked, marks = data.mark(" ".join(argv[1:]))
        print(masked)
        for mk in marks:
            print(f"  {mk.mask} = {mk.entry_id}: {mk.en!r} -> {mk.hi}")
        return 0

    text = " ".join(argv)
    from deep_translator import GoogleTranslator
    tr = lambda s: GoogleTranslator(source="auto", target="hi").translate(s) or s
    masked, marks = data.mark(text)
    print(f"EN     : {text}")
    print(f"masked : {masked}")
    print(f"plain  : {tr(text)}")
    print(f"IDIOMS : {translate_text(text, tr, data=data)}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
