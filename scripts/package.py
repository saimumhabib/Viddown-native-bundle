#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Package ONE TOOL (not the whole bundle) into a versioned zip with provenance.

usage: package.py <yt-dlp|cpython|ffmpeg> [<abi>] --version-tag VERSION
                  [--dist dist] [--sources build/sources] [--build build]

Each tool is released on its own (tag = <tool>-v<version>, see scripts/ci/tools.py), so each gets its own zip:

  viddown-native-yt-dlp-VERSION.zip            no ABI; yt-dlp + its pure-Python dependencies
  viddown-native-cpython-ABI-VERSION.zip       CPython + OpenSSL + libffi + the stdlib for that ABI
  viddown-native-ffmpeg-ABI-VERSION.zip        FFmpeg + FFprobe for that ABI

Layout of every zip (consumed by the VidDown app repo; the three Android zips extract into one tree):
  jniLibs/<abi>/*.so            executables + shared libs (extractNativeLibs=true in the app)   [cpython, ffmpeg]
  assets/*.zip, assets/*.txt    Python stdlib for this ABI [cpython] / yt-dlp site-packages [yt-dlp]
  metadata/*                    build metadata (FFmpeg configure line, python-packages.json)
  BUILD_INFO.json               the tool's components: version, source ACTUALLY used, verified hashes
  THIRD_PARTY_NOTICES.md        generated from the lock file + provenance
  licenses/<component>/...      licence files taken from the verified source archives
  SHA256SUMS                    hashes of every file in the zip
"""
import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import tomllib
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "ci"))
import tools  # noqa: E402  (which components belong to which tool)

FFMPEG_BINARIES = {"libffmpeg.so", "libffprobe.so"}
LIC_RX = re.compile(r"^(LICEN[SC]E|COPYING|NOTICE|AUTHORS)([-._].*)?$", re.I)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def collect(tool: str, abi: str | None, dist: Path) -> dict[str, bytes]:
    """The files of this tool, taken from what the build scripts left in dist/."""
    files: dict[str, bytes] = {}
    if tool == "yt-dlp":
        roots = [dist / "shared"]
    else:
        roots = [dist / abi]
    for part in roots:
        if not part.is_dir():
            raise SystemExit(f"missing build output: {part}")
        for f in sorted(part.rglob("*")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(part))
            if tool == "ffmpeg":
                keep = (rel.startswith("jniLibs/") and f.name in FFMPEG_BINARIES) or rel.startswith("metadata/")
            elif tool == "cpython":
                keep = (rel.startswith("jniLibs/") and f.name not in FFMPEG_BINARIES) or rel.startswith("assets/")
            else:
                keep = True
            if keep:
                files[rel] = f.read_bytes()
    if not files:
        raise SystemExit(f"no files for tool '{tool}' in {', '.join(map(str, roots))}")
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tool", choices=["yt-dlp", "cpython", "ffmpeg"])
    ap.add_argument("abi", nargs="?")
    ap.add_argument("--dist", default=str(ROOT / "dist"))
    ap.add_argument("--sources", default=str(ROOT / "build" / "sources"))
    ap.add_argument("--build", default=str(ROOT / "build"))
    ap.add_argument("--version-tag", required=True, help="the tool's version, as in the release tag <tool>-v<version>")
    a = ap.parse_args()
    if tools.TOOLS[a.tool]["abi"] and not a.abi:
        ap.error(f"{a.tool} is built per ABI: give the abi (arm64-v8a, x86_64, ...)")
    if not tools.TOOLS[a.tool]["abi"] and a.abi:
        ap.error(f"{a.tool} has no ABI; do not give one")
    dist, src = Path(a.dist), Path(a.sources)
    comps = tomllib.loads((ROOT / "native" / "sources.lock.toml").read_text())["components"]
    mine = tools.components_of(a.tool, comps)

    files = collect(a.tool, a.abi, dist)

    # components of THIS tool = those with a provenance.json from this build
    used, notices = {}, ["# Third-party notices", "",
                         f"Generated from native/sources.lock.toml and the provenance of this build ({a.tool}).", "",
                         "| component | version | licence | source actually used | archive sha256 |", "|---|---|---|---|---|"]
    for name in mine:
        e = comps[name]
        prov = src / f"{name}-{e['version']}" / "provenance.json"
        if not prov.is_file():
            continue
        p = json.loads(prov.read_text())
        used[name] = {k: p[k] for k in ("version", "license", "commit", "source_used", "evidence", "archive", "archive_sha256", "attempts")}
        notices.append(f"| {name} | {e['version']} | {e['license']} | {p['source_used']['name']} ({p['source_used']['url']}) | `{p['archive_sha256']}` |")
        with tarfile.open(src / f"{name}-{e['version']}" / p["archive"]) as t:
            for m in t:
                parts = Path(m.name).parts
                if m.isfile() and len(parts) == 2 and LIC_RX.match(parts[1]) and m.size < 512_000:
                    files[f"licenses/{name}/{parts[1]}"] = t.extractfile(m).read()
    files["THIRD_PARTY_NOTICES.md"] = ("\n".join(notices) + "\n").encode()
    files["LICENSE"] = (ROOT / "LICENSE").read_bytes() if (ROOT / "LICENSE").exists() else b""

    ndk = None
    if a.abi:
        for env_sh in (Path(a.build) / "work" / a.abi / "cpython" / "Android" / "android-env.sh",
                       Path(a.build) / "work" / a.abi / "cpython-env" / "Android" / "android-env.sh"):
            if env_sh.exists():
                m = re.search(r"^ndk_version=(\S+)", env_sh.read_text(), re.M)
                ndk = m.group(1) if m else None
                break

    info = {
        "bundle": "viddown-native-bundle", "tool": a.tool, "version_tag": a.version_tag, "release_tag": f"{a.tool}-v{a.version_tag}",
        "content_id": tools.content_id(a.tool, comps), "abi": a.abi,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo_commit": git_sha(), "android_ndk": ndk, "android_api_level": 24 if a.abi else None,
        "components": used,
        "unverified_notes": [
            "Built by CI; an ABI is only claimed as verified once its emulator smoke test passed.",
        ],
    }
    files["BUILD_INFO.json"] = (json.dumps(info, indent=2) + "\n").encode()
    files["SHA256SUMS"] = "".join(f"{sha(b)}  {n}\n" for n, b in sorted(files.items())).encode()

    out = dist / "release"
    out.mkdir(parents=True, exist_ok=True)
    name = f"viddown-native-{a.tool}-{a.abi + '-' if a.abi else ''}{a.version_tag}.zip"
    zpath = out / name
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in sorted(files.items()):
            zi = zipfile.ZipInfo(n, date_time=(1980, 1, 1, 0, 0, 0))  # reproducible
            zi.external_attr = (0o755 if n.startswith("jniLibs/") else 0o644) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, b)
    (out / (zpath.name + ".sha256")).write_text(f"{sha(zpath.read_bytes())}  {zpath.name}\n")
    print(f"wrote {zpath} ({zpath.stat().st_size} bytes), {len(files)} files, {len(used)} components with provenance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
