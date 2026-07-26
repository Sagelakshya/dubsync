"""gen-licenses.py — rebuild the third-party attribution from what actually ships.

Attribution rots the moment a dependency is added (it did: faster-whisper's native
stack landed with nothing crediting it). So it isn't hand-maintained any more —
this reads the *installed* packages of the portable build, copies each one's real
license text into `licenses/<name>/`, and rewrites the table in
THIRD-PARTY-LICENSES.md. Metadata is the source of truth, never memory.

Run (after build-portable.ps1):
    python gen-licenses.py build\\dubsync\\runtime\\Lib\\site-packages

Hand-written entries in `licenses/` that don't come from pip — ffmpeg, python,
the model weights — are left alone; the script only ever touches package folders
it can see in site-packages.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from email import message_from_string

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "licenses")
DOC = os.path.join(HERE, "THIRD-PARTY-LICENSES.md")

# Where a project actually lives, when the metadata gives several URLs.
_URL_KEYS = ("Home-page", "Source", "Repository", "Homepage", "Source Code", "GitHub")


def _url(msg) -> str:
    if msg.get("Home-page"):
        return msg["Home-page"]
    for raw in msg.get_all("Project-URL") or []:
        label, _, link = raw.partition(",")
        if label.strip() in _URL_KEYS:
            return link.strip()
    for raw in msg.get_all("Project-URL") or []:      # any URL beats none
        return raw.partition(",")[2].strip()
    return ""


def _license(msg) -> str:
    """Newer wheels use the SPDX 'License-Expression'; older ones stuff the whole
    license TEXT into 'License'. Fall back to the Trove classifier, which is short."""
    lic = (msg.get("License-Expression") or "").strip()
    if lic:
        return lic
    lic = (msg.get("License") or "").strip()
    if lic and "\n" not in lic and len(lic) < 60:
        return lic
    for c in msg.get_all("Classifier") or []:
        if c.startswith("License :: "):
            return c.rsplit("::", 1)[-1].strip()
    return "see licenses/ folder"


def collect(site: str) -> list[dict]:
    pkgs = []
    for entry in sorted(os.listdir(site)):
        if not entry.endswith(".dist-info"):
            continue
        info = os.path.join(site, entry)
        meta = os.path.join(info, "METADATA")
        if not os.path.exists(meta):
            continue
        with open(meta, encoding="utf-8", errors="replace") as f:
            msg = message_from_string(f.read())
        name = msg.get("Name") or entry.split("-")[0]
        pkgs.append({"name": name, "version": msg.get("Version", "?"),
                     "license": _license(msg), "url": _url(msg), "info": info})
    return pkgs


_LIC_RE = re.compile(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.I)


def _top_level_dirs(info: str) -> list[str]:
    """The folders a package actually installs — some wheels (onnxruntime) keep
    their LICENSE there rather than in the .dist-info."""
    site = os.path.dirname(info)
    names = []
    tl = os.path.join(info, "top_level.txt")
    if os.path.exists(tl):
        with open(tl, encoding="utf-8", errors="replace") as f:
            names = [line.strip() for line in f if line.strip()]
    return [os.path.join(site, n) for n in names
            if os.path.isdir(os.path.join(site, n))]


def copy_texts(pkgs: list[dict]) -> list[str]:
    """Copy each package's license file(s) into licenses/<name>/. Returns the
    names of packages whose wheel ships no license text at all — those get named
    in the doc rather than quietly looking covered."""
    missing = []
    for p in pkgs:
        found = []
        for base in [p["info"]] + _top_level_dirs(p["info"]):
            depth0 = base.rstrip("\\/").count(os.sep)
            for root, _dirs, files in os.walk(base):
                if root.count(os.sep) - depth0 > 1:     # don't trawl whole packages
                    continue
                found += [os.path.join(root, f) for f in files if _LIC_RE.match(f)]
        if not found:
            missing.append(p["name"])
            continue
        dst = os.path.join(OUT_DIR, p["name"])
        os.makedirs(dst, exist_ok=True)
        for src in found:
            shutil.copy2(src, os.path.join(dst, os.path.basename(src)))
    return missing


_TABLE_HEAD = "| Package | Version | License | Source |\n|---|---|---|---|\n"


def rewrite_doc(pkgs: list[dict], missing: list[str]) -> None:
    """Replace only the generated table, so the hand-written prose above it stays."""
    with open(DOC, encoding="utf-8") as f:
        doc = f.read()
    rows = "".join(
        f"| {p['name']} | {p['version']} | {p['license']} | "
        f"{'<' + p['url'] + '>' if p['url'] else ''} |\n"
        for p in sorted(pkgs, key=lambda p: p["name"].lower()))
    note = ""
    if missing:
        note = ("\n> These wheels ship **no license file** of their own, so there is no "
                "`licenses/` folder for them: **" + ", ".join(sorted(missing)) +
                "**. Their license is the one named in the table; the full text is "
                "in the project's own repository, linked above.\n")
    start = doc.index("| Package | Version | License | Source |")
    end = doc.index("> Versions are those built into", start)
    with open(DOC, "w", encoding="utf-8", newline="\n") as f:
        f.write(doc[:start] + _TABLE_HEAD + rows + note + "\n" + doc[end:])


if __name__ == "__main__":
    site = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "build", "dubsync", "runtime", "Lib", "site-packages")
    if not os.path.isdir(site):
        sys.exit(f"site-packages not found: {site}\nBuild the portable package first.")
    pkgs = collect(site)
    if not pkgs:
        sys.exit(f"No .dist-info folders in {site}")
    missing = copy_texts(pkgs)
    rewrite_doc(pkgs, missing)
    print(f"{len(pkgs)} packages listed; texts copied for {len(pkgs) - len(missing)}.")
    if missing:
        print("No license file in the wheel (named in the doc): " + ", ".join(missing))
    print("Wrote THIRD-PARTY-LICENSES.md and licenses/.")
