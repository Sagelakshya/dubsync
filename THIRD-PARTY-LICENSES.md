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
- **PyAV (`av`)** *(bundled)* — the Python package is **BSD-3-Clause**, but its wheels ship their
  own copies of the **FFmpeg shared libraries (LGPL v2.1+)**, separate from the `ffmpeg.exe` above.
  Full text: `licenses/av/` · <https://github.com/PyAV-Org/PyAV>.

## Speech recognition (transcription)

Transcription is done locally by **faster-whisper** (MIT, <https://github.com/SYSTRAN/faster-whisper>),
which runs on **CTranslate2** (MIT) with **ONNX Runtime** (MIT) for voice-activity detection. These
are bundled and listed in the table below.

The **model weights are not bundled** — they are downloaded on first use from the Hugging Face Hub
(`Systran/faster-whisper-<size>`, MIT) and cached in your user profile. They are conversions of
**OpenAI's Whisper** models, released by OpenAI under the **MIT License**
(<https://github.com/openai/whisper>). Whisper is the work of OpenAI; dubsync only calls it.

## Hinglish (optional, not bundled)

The Hinglish style pass calls a **local Ollama** server running **Gemma 3** if you have installed
them yourself. Neither is bundled or redistributed here. Gemma is used under Google's
**Gemma Terms of Use** (<https://ai.google.dev/gemma/terms>); Ollama is MIT
(<https://github.com/ollama/ollama>). Without them the tool still produces accurate Hindi.

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
| aiohappyeyeballs | 2.7.1 | PSF-2.0 | <https://github.com/aio-libs/aiohappyeyeballs> |
| aiohttp | 3.14.3 | Apache-2.0 AND MIT | <https://github.com/aio-libs/aiohttp> |
| aiosignal | 1.4.0 | Apache 2.0 | <https://github.com/aio-libs/aiosignal> |
| anyio | 4.14.2 | MIT | <https://anyio.readthedocs.io/en/latest/> |
| attrs | 26.1.0 | MIT | <https://github.com/python-attrs/attrs> |
| av | 18.0.0 | BSD-3-Clause | <https://github.com/PyAV-Org/PyAV> |
| beautifulsoup4 | 4.15.0 | MIT License | <https://www.crummy.com/software/BeautifulSoup/bs4/> |
| blinker | 1.9.0 | MIT License | <https://github.com/pallets-eco/blinker/> |
| certifi | 2026.7.22 | MPL-2.0 | <https://github.com/certifi/python-certifi> |
| charset-normalizer | 3.4.9 | MIT | <https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md> |
| click | 8.4.2 | BSD-3-Clause | <https://github.com/pallets/click/> |
| colorama | 0.4.6 | BSD License | <https://github.com/tartley/colorama> |
| ctranslate2 | 4.8.1 | MIT | <https://opennmt.net> |
| deep-translator | 1.11.4 | MIT | <https://github.com/nidhaloff/deep_translator> |
| defusedxml | 0.7.1 | PSFL | <https://github.com/tiran/defusedxml> |
| edge-tts | 7.2.8 | GNU Lesser General Public License v3 (LGPLv3) | <https://github.com/rany2/edge-tts> |
| faster-whisper | 1.2.1 | MIT | <https://github.com/SYSTRAN/faster-whisper> |
| filelock | 3.32.0 | MIT | <https://github.com/tox-dev/py-filelock> |
| Flask | 3.1.3 | BSD-3-Clause | <https://github.com/pallets/flask/> |
| flatbuffers | 25.12.19 | Apache 2.0 | <https://google.github.io/flatbuffers/> |
| frozenlist | 1.8.0 | Apache-2.0 | <https://github.com/aio-libs/frozenlist> |
| fsspec | 2026.6.0 | BSD-3-Clause | <https://github.com/fsspec/filesystem_spec> |
| h11 | 0.16.0 | MIT | <https://github.com/python-hyper/h11> |
| hf-xet | 1.5.2 | Apache-2.0 | <https://github.com/huggingface/xet-core> |
| httpcore | 1.0.9 | BSD-3-Clause | <https://www.encode.io/httpcore/> |
| httpx | 0.28.1 | BSD-3-Clause | <https://github.com/encode/httpx> |
| huggingface_hub | 1.24.0 | Apache-2.0 | <https://github.com/huggingface/huggingface_hub> |
| idna | 3.18 | BSD-3-Clause | <https://github.com/kjd/idna> |
| itsdangerous | 2.2.0 | BSD License | <https://github.com/pallets/itsdangerous/> |
| Jinja2 | 3.1.6 | BSD License | <https://github.com/pallets/jinja/> |
| MarkupSafe | 3.0.3 | BSD-3-Clause | <https://github.com/pallets/markupsafe/> |
| multidict | 6.7.1 | Apache License 2.0 | <https://github.com/aio-libs/multidict> |
| numpy | 2.5.1 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | <https://numpy.org> |
| onnxruntime | 1.28.0 | MIT License | <https://onnxruntime.ai> |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause | <https://github.com/pypa/packaging> |
| propcache | 0.5.2 | Apache-2.0 | <https://github.com/aio-libs/propcache> |
| protobuf | 7.35.1 | 3-Clause BSD License | <https://developers.google.com/protocol-buffers/> |
| PyYAML | 6.0.3 | MIT | <https://pyyaml.org/> |
| requests | 2.34.2 | Apache-2.0 | <https://github.com/psf/requests> |
| soupsieve | 2.9.1 | MIT | <https://github.com/facelessuser/soupsieve> |
| tabulate | 0.10.0 | MIT | <https://github.com/astanin/python-tabulate> |
| tokenizers | 0.23.1 | Apache Software License | <https://github.com/huggingface/tokenizers> |
| tqdm | 4.69.1 | MPL-2.0 AND MIT | <https://tqdm.github.io> |
| typing_extensions | 4.16.0 | PSF-2.0 | <https://github.com/python/typing_extensions> |
| urllib3 | 2.7.0 | MIT | <https://github.com/urllib3/urllib3/blob/main/CHANGES.rst> |
| Werkzeug | 3.1.8 | BSD-3-Clause | <https://github.com/pallets/werkzeug/> |
| yarl | 1.24.5 | Apache-2.0 | <https://github.com/aio-libs/yarl> |
| youtube-transcript-api | 1.2.4 | MIT | <https://github.com/jdepoix/youtube-transcript-api> |
| yt-dlp | 2026.7.4 | Unlicense | <https://github.com/yt-dlp/yt-dlp> |

> These wheels ship **no license file** of their own, so there is no `licenses/` folder for them: **ctranslate2, flatbuffers, tokenizers**. Their license is the one named in the table; the full text is in the project's own repository, linked above.

> Versions are those built into the portable Windows package; patch versions may differ
> slightly from a fresh `pip install`, but the license of each project is unchanged.

_Generated for dubsync. If anything here is inaccurate, open an issue on the repo._
