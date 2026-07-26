"""verify.py — put every idiom entry through the REAL translator and check the splice.

Why this exists
---------------
The idiom dictionary is data, so it can grow without a code change. That is the
point of it, and also the risk: nobody reviews a JSON edit the way they review
code, and a bad entry is silent. It doesn't crash, it just ships a sentence that
is subtly wrong to a listener who has no way to know.

So the growth path has a gate. Add an entry, run this, and it tells you whether
the entry actually does what you think.

What it can and cannot judge
----------------------------
Mechanical faults are decided here and FAIL:
  * the entry doesn't even match its own `sample` (a typo in `en`);
  * the placeholder didn't survive translation, so there is nothing to splice;
  * the spliced text lost the Hindi it was supposed to gain.

Grammatical agreement is NOT decided here, because judging Hindi agreement by
script is its own error-prone project. Instead the slot is INSPECTED: if the
translator put a case-marker or an inflected verb right against the placeholder,
the entry is flagged REVIEW with that context shown, because that is exactly
where a masculine-singular guess breaks a feminine replacement. A human reads
those few lines rather than all of them.

    python verify.py            # every entry
    python verify.py brownie    # only entries whose id contains "brownie"
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

import idioms

# Hindi tokens that only appear because something around the slot had to AGREE
# with what's in it. Their presence next to the mask means the translator made a
# gender/number guess, and that guess is masculine-singular.
_CASE_MARKERS = {"का", "के", "की", "को", "ने", "में", "से", "पर", "तक"}
_AGREEING_TAIL = ("ते", "ती", "ता", "गे", "गी", "गा", "या", "यी", "ये")


def _neighbours(frame: str, mask: str) -> tuple[str, str]:
    """The word immediately before and after the placeholder in the Hindi frame."""
    if mask not in frame:
        return "", ""
    before, after = frame.split(mask, 1)
    b = before.strip().split(" ")[-1].strip("।,.?!") if before.strip() else ""
    a = after.strip().split(" ")[0].strip("।,.?!") if after.strip() else ""
    return b, a


def check(entry: idioms.Entry, data: idioms.IdiomData, translate) -> dict:
    r = {"id": entry.id, "span": entry.span, "status": "OK", "notes": [],
         "plain": "", "frame": "", "spliced": ""}

    sample = entry.sample or (entry.en[0] if entry.en else "")
    if not sample:
        r["status"] = "FAIL"
        r["notes"].append("no sample sentence to test against")
        return r

    masked, marks = data.mark(sample)
    mine = [m for m in marks if m.entry_id == entry.id]
    if not mine:
        r["status"] = "FAIL"
        r["notes"].append(
            f"entry does not match its own sample. Check `en` against: {sample!r}")
        return r

    try:
        r["plain"] = translate(sample)
        frame = translate(masked)
    except Exception as e:                     # network/endpoint problems
        r["status"] = "ERROR"
        r["notes"].append(f"{type(e).__name__}: {e}")
        return r

    r["frame"] = frame
    mk = mine[0]
    if mk.mask not in (frame or ""):
        r["status"] = "FAIL"
        r["notes"].append(
            f"placeholder {mk.mask} did not survive translation, nothing to splice into")
        return r

    spliced, lost = idioms.splice(frame, marks)
    r["spliced"] = spliced
    if lost:
        r["status"] = "FAIL"
        r["notes"].append(f"lost placeholders: {[m.mask for m in lost]}")
        return r
    if entry.hi not in spliced:
        r["status"] = "FAIL"
        r["notes"].append("spliced text does not contain the Hindi replacement")
        return r

    # Agreement is not judged, it is surfaced. A clause-span entry masks its own
    # predicate, so nothing outside it has to agree and this check is skipped.
    if entry.span == "phrase":
        before, after = _neighbours(frame, mk.mask)
        touching = [w for w in (before, after)
                    if w in _CASE_MARKERS or w.endswith(_AGREEING_TAIL)]
        if touching:
            r["status"] = "REVIEW"
            r["notes"].append(
                f"agreement-sensitive slot: the translator put {touching} against the "
                f"placeholder, having guessed masculine-singular. Read the spliced line; "
                f"if `{entry.hi}` is feminine, switch to a masculine equivalent or make "
                f"this a clause entry.")
    return r


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")       # Windows console is cp1252
    from deep_translator import GoogleTranslator

    data = idioms.load()
    entries = data.entries
    if argv:
        needle = argv[0].lower()
        entries = [e for e in entries if needle in e.id.lower()]
    if not entries:
        print("no matching entries")
        return 1

    def translate(s: str) -> str:
        return GoogleTranslator(source="auto", target="hi").translate(s) or s

    print(f"verifying {len(entries)} entr{'y' if len(entries)==1 else 'ies'} "
          f"against the live translator\n")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda e: check(e, data, translate), entries))

    order = {"FAIL": 0, "ERROR": 1, "REVIEW": 2, "OK": 3}
    results.sort(key=lambda r: (order.get(r["status"], 9), r["id"]))

    for r in results:
        print(f"[{r['status']:6}] {r['id']}  ({r['span']})")
        for n in r["notes"]:
            print(f"          ! {n}")
        if r["status"] != "OK" and r["plain"]:
            print(f"          plain  : {r['plain']}")
        if r["spliced"]:
            print(f"          idioms : {r['spliced']}")
        print()

    counts = {k: sum(1 for r in results if r["status"] == k) for k in order}
    print(f"summary: {counts['OK']} ok, {counts['REVIEW']} to review, "
          f"{counts['FAIL']} failed, {counts['ERROR']} errored")
    return 1 if counts["FAIL"] or counts["ERROR"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
