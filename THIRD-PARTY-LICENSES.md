# Third-Party Licenses & Attribution

dubsync is original code (the sync engine in `dub.py` and the web app in `app.py`).
It stands on open-source software and calls a few free external services. This file
credits all of them. The **portable download** redistributes the components marked
*(bundled)* below, so their full license texts are included in the `licenses/` folder
next to this file. (When running from source, pip installs these itself.)

## Bundled binaries — read these first

- **FFmpeg** *(bundled)* — used for all audio/video processing. This is the
  `8.1.1-full_build` from gyan.dev, compiled with `--enable-gpl --enable-version3`, so it is
  licensed under the **GNU GPL v3**. Full text: `licenses/ffmpeg/LICENSE-GPLv3.txt`.
  Source code for FFmpeg is available from <https://ffmpeg.org/download.html> and the build
  page <https://www.gyan.dev/ffmpeg/builds/>.
- **edge-tts** *(bundled)* — the client for Microsoft's neural voices, licensed under the
  **GNU LGPL v3**. Full text: `licenses/edge-tts/`. As LGPL, it is a replaceable component:
  you may swap in your own build of the library.
- **Python** *(bundled)* — the embeddable CPython runtime, under the **PSF License Agreement**.
  Full text: `licenses/python/LICENSE-PSF.txt` · <https://docs.python.org/3/license.html>.

## External services (not open source, credited as data sources)

The translation and the spoken voice are produced by external services, accessed through their
free public endpoints. They are **not** part of this software and carry their own terms:

- **Google Translate** — translation, via the free `translate.google.com` endpoint (used by `deep-translator`).
- **Microsoft Edge neural voices** — text-to-speech, via the Edge Read-Aloud endpoint (used by `edge-tts`).
- **YouTube** — captions and video, via `youtube-transcript-api` and `yt-dlp`.

## Python packages (bundled in the portable download)

Full license texts for every entry below are in `licenses/<name>/`.

| Package | Version | License | Source |
|---|---|---|---|

| aiohappyeyeballs | 2.6.2 | PSF-2.0 | <https://github.com/aio-libs/aiohappyeyeballs> |
| aiohttp | 3.14.1 | Apache-2.0 AND MIT | <https://github.com/aio-libs/aiohttp> |
| aiosignal | 1.4.0 | Apache 2.0 | <https://github.com/aio-libs/aiosignal> |
| attrs | 26.1.0 | MIT | <https://tidelift.com/subscription/pkg/pypi-attrs?utm_source=pypi-attrs&utm_medium=pypi> |
| beautifulsoup4 | 4.15.0 | MIT License | <https://www.crummy.com/software/BeautifulSoup/bs4/> |
| blinker | 1.9.0 | MIT License | <https://github.com/pallets-eco/blinker/> |
| certifi | 2026.5.20 | MPL-2.0 | <https://github.com/certifi/python-certifi> |
| charset-normalizer | 3.4.7 | MIT | <https://github.com/jawah/charset_normalizer> |
| click | 8.4.1 | BSD-3-Clause | <https://github.com/pallets/click/> |
| colorama | 0.4.6 | BSD License | <https://github.com/tartley/colorama> |
| deep-translator | 1.11.4 | MIT | <https://github.com/nidhaloff/deep_translator> |
| defusedxml | 0.7.1 | PSFL | <https://github.com/tiran/defusedxml> |
| edge-tts | 7.2.8 | GNU Lesser General Public License v3 (LGPLv3) | <https://github.com/rany2/edge-tts> |
| Flask | 3.1.3 | BSD-3-Clause | <https://github.com/pallets/flask/> |
| frozenlist | 1.8.0 | Apache-2.0 | <https://github.com/aio-libs/frozenlist> |
| idna | 3.18 | BSD-3-Clause | <https://github.com/kjd/idna> |
| itsdangerous | 2.2.0 | BSD License | <https://github.com/pallets/itsdangerous/> |
| Jinja2 | 3.1.6 | BSD License | <https://github.com/pallets/jinja/> |
| MarkupSafe | 3.0.3 | BSD-3-Clause | <https://github.com/pallets/markupsafe/> |
| multidict | 6.7.1 | Apache License 2.0 | <https://github.com/aio-libs/multidict> |
| propcache | 0.5.2 | Apache-2.0 | <https://github.com/aio-libs/propcache> |
| requests | 2.34.2 | Apache-2.0 | <https://github.com/psf/requests> |
| soupsieve | 2.8.4 | MIT | <https://github.com/facelessuser/soupsieve> |
| tabulate | 0.10.0 | MIT | <https://github.com/astanin/python-tabulate> |
| typing_extensions | 4.15.0 | PSF-2.0 | <https://github.com/python/typing_extensions> |
| urllib3 | 2.7.0 | MIT | <https://github.com/urllib3/urllib3> |
| Werkzeug | 3.1.8 | BSD-3-Clause | <https://github.com/pallets/werkzeug/> |
| yarl | 1.24.2 | Apache-2.0 | <https://github.com/aio-libs/yarl> |
| youtube-transcript-api | 1.2.4 | MIT | <https://github.com/jdepoix/youtube-transcript-api> |
| yt-dlp | 2026.7.4 | Unlicense | <https://github.com/yt-dlp/yt-dlp> |

> Versions are those built into the portable Windows package; patch versions may differ
> slightly from a fresh `pip install`, but the license of each project is unchanged.

_Generated for dubsync. If anything here is inaccurate, open an issue on the repo._
