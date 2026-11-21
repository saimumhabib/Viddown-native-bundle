#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write the markdown notes of ONE tool's release.

usage: release_notes.py --lock native/sources.lock.toml --tool TOOL --tag TAG --assets DIR
                        [--pr N] [--run-url URL] [--repo owner/name]

TOOL is yt-dlp, cpython, ffmpeg or newpipe (see tools.py). Only states what is true: x86_64 is the one ABI
whose binaries are executed in CI (emulator smoke test, together with the CURRENT versions of the other two
Android tools); every other ABI is "built, not run-tested". ABIs are read from the zip names found in --assets.
The NewPipe helper zips exist only if the native executable passed its self-test in CI, and the notes say
what that test did and did not cover.

The notes end with an invisible  <!-- content-id: ... -->  line. tools.py reads it back to decide whether a
tag was made from the same inputs (then nothing is released again) or from different ones (then the next
free tag <tool>-v<version>.2 is used).
"""
import argparse
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tools  # noqa: E402

RUN_TESTED = {"x86_64": "emulator smoke test on Android API 24 and 34 passed"}
ABI_RX = {t: re.compile(rf"^viddown-native-{t}-(arm64-v8a|armeabi-v7a|x86_64|x86)-.+\.zip$") for t in ("cpython", "ffmpeg")}
NEWPIPE_RX = re.compile(r"^viddown-native-newpipe-(linux-x86_64|windows-x86_64)-.+\.zip$")

WHAT = {
    "yt-dlp": "yt-dlp and its pure-Python dependencies, **built from source** in GitHub Actions (hash-verified archives, no pip). "
              "Pure Python: the same zip works on every ABI. It needs the matching CPython release to run.",
    "cpython": "CPython, OpenSSL and libffi **built from source** in GitHub Actions for Android, one zip per ABI "
               "(shared libpython, the stdlib and a launcher). No prebuilt third-party binaries.",
    "ffmpeg": "FFmpeg and FFprobe **built from source** in GitHub Actions for Android, one zip per ABI, "
              "with a minimal LGPL-only configuration (remux / merge of files yt-dlp already downloaded; not a downloader).",
    "newpipe": "The NewPipe helper for desktop: NewPipe Extractor compiled to a native Windows/Linux executable "
               "(GraalVM native-image), no Java needed on the user's machine.",
}


def build_notes(tool: str, lock: dict, tag: str, assets: list[str], pr: str | None, run_url: str | None,
                repo: str | None) -> str:
    comps = lock["components"]
    mine = tools.components_of(tool, comps)
    out = [f"# {tag}", "", WHAT[tool], ""]
    src = []
    if pr:
        src.append(f"update PR #{pr}" + (f" (https://github.com/{repo}/pull/{pr})" if repo else ""))
    if run_url:
        src.append(f"build run {run_url}")
    if src:
        out += ["Produced by: " + ", ".join(src) + ".", ""]
    out += ["Each tool of this bundle is released on its own; the tag is the tool's own version "
            f"(`{tool}-v<version>`). The other tools have their own releases.", ""]
    out += ["## Components in this release", "", "| component | version | licence |", "|---|---|---|"]
    for name in mine:
        c = comps[name]
        out.append(f"| {name} | {c['version']} | {c['license']} |")
    out += ["", "## What was verified", ""]
    if tool in ("cpython", "ffmpeg"):
        abis = sorted(m.group(1) for n in assets if (m := ABI_RX[tool].match(n)))
        for abi in abis:
            if abi in RUN_TESTED:
                out.append(f"- **{abi}**: built, and executed in CI: {RUN_TESTED[abi]} "
                           "(python/ssl, yt-dlp, ffmpeg/ffprobe together).")
            else:
                out.append(f"- **{abi}**: built from source, **not run-tested in CI** "
                           "(the CI emulator is x86_64; this ABI needs a run on a device).")
    elif tool == "yt-dlp":
        out.append("- Assembled from hash-verified sources; reports its own version in the x86_64 emulator smoke test "
                   f"(API 24 and 34), including inspecting and downloading from a local test server.")
    else:
        plats = sorted(m.group(1) for n in assets if (m := NEWPIPE_RX.match(n)))
        if plats:
            for plat in plats:
                out.append(f"- **NewPipe helper, {plat}** (desktop, no Java needed): compiled from source with GraalVM "
                           "native-image and executed in CI: self-test of the JavaScript engine, HTML parser, JSON, protobuf and "
                           "reflection plus one HTTPS request, then the packaged files and their CA bundle lookup were checked. "
                           "Fetching real YouTube data from CI is **not** part of that gate (it is only reported in the job summary).")
        else:
            out.append("- **NewPipe helper (desktop)**: no zip - its CI build did not produce a passing native executable.")
    out += ["", "## Verify your download", "",
            "- `SHA256SUMS.txt` lists the hash of every zip in this release.",
            "- Each zip contains `BUILD_INFO.json`: for every component the source that was ACTUALLY used and its "
            "verified hashes, plus `THIRD_PARTY_NOTICES.md` and the licence texts.", "",
            f"<!-- content-id: {tools.content_id(tool, comps)} -->", ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", required=True)
    ap.add_argument("--tool", required=True, choices=sorted(tools.TOOLS))
    ap.add_argument("--tag", required=True)
    ap.add_argument("--assets", required=True)
    ap.add_argument("--pr")
    ap.add_argument("--run-url")
    ap.add_argument("--repo")
    a = ap.parse_args()
    lock = tomllib.loads(Path(a.lock).read_text())
    names = [f.name for f in sorted(Path(a.assets).glob(f"viddown-native-{a.tool}-*.zip"))]
    print(build_notes(a.tool, lock, a.tag, names, a.pr, a.run_url, a.repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
