#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Package the native NewPipe helper of one desktop platform into a versioned zip with provenance.

usage: package_newpipe.py <platform> [--dist dist] [--sources build/sources] [--version-tag TAG]
       platform: linux-x86_64 | windows-x86_64

Input: dist/newpipe-<platform>/ as staged by scripts/build/build_newpipe.py native.

Layout of the zip (consumed by the VidDown desktop app):
  bin/newpipe/viddown-newpipe[.exe]   the helper, no Java needed
  bin/newpipe/cacert.pem              CA certificates (certifi); the helper finds it next to itself
  BUILD_INFO.json                     components with verified hashes, toolchain, Maven jars with SHA-256
  THIRD_PARTY_NOTICES.md              generated from the lock file, provenance and the jars
  licenses/<component>/...            licence files taken from the verified source archives
  licenses/jars/<jar>/...             licence files found inside the Maven runtime jars
  SHA256SUMS                          hashes of every file in the zip
"""
import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLATFORMS = ("linux-x86_64", "windows-x86_64")
USED_COMPONENTS = ("newpipe-extractor", "py-certifi")   # what ends up inside the executable / next to it
LIC_RX = re.compile(r"^(LICEN[SC]E|COPYING|NOTICE|AUTHORS)([-._].*)?$", re.I)
JAR_LIC_RX = re.compile(r"^(META-INF/)?(LICEN[SC]E|COPYING|NOTICE)([-._].*)?$", re.I)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def jar_licenses(jar: Path) -> dict[str, bytes]:
    """Licence/notice files stored inside a jar (small ones only)."""
    found = {}
    with zipfile.ZipFile(jar) as z:
        for info in z.infolist():
            if not info.is_dir() and JAR_LIC_RX.match(info.filename) and info.file_size < 512_000:
                found[Path(info.filename).name] = z.read(info)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("platform", choices=PLATFORMS)
    ap.add_argument("--dist", default=str(ROOT / "dist"))
    ap.add_argument("--sources", default=str(ROOT / "build" / "sources"))
    ap.add_argument("--version-tag", default="dev")
    a = ap.parse_args()
    dist, src = Path(a.dist), Path(a.sources)
    staged = dist / f"newpipe-{a.platform}"
    meta_file = staged / "metadata" / "newpipe-build.json"
    if not (staged / "bin" / "newpipe").is_dir() or not meta_file.is_file():
        raise SystemExit(f"missing build output: {staged} (run scripts/build/build_newpipe.py native first)")
    meta = json.loads(meta_file.read_text())
    comps = tomllib.loads((ROOT / "native" / "sources.lock.toml").read_text())["components"]

    files: dict[str, bytes] = {}
    for f in sorted((staged / "bin").rglob("*")):
        if f.is_file():
            files[f"bin/{f.relative_to(staged / 'bin').as_posix()}"] = f.read_bytes()

    used = {}
    notices = ["# Third-party notices", "",
               "Generated from native/sources.lock.toml, the provenance of this build and the jars used.", "",
               "## Components built from verified source", "",
               "| component | version | licence | source actually used | archive sha256 |", "|---|---|---|---|---|"]
    for name in USED_COMPONENTS:
        e = comps[name]
        prov_file = src / f"{name}-{e['version']}" / "provenance.json"
        if not prov_file.is_file():
            raise SystemExit(f"no provenance for {name} {e['version']} in {src} (was it fetched by the build?)")
        p = json.loads(prov_file.read_text())
        used[name] = {k: p[k] for k in ("version", "license", "commit", "source_used", "evidence", "archive", "archive_sha256", "attempts")}
        notices.append(f"| {name} | {e['version']} | {e['license']} | {p['source_used']['name']} ({p['source_used']['url']}) | `{p['archive_sha256']}` |")
        with tarfile.open(src / f"{name}-{e['version']}" / p["archive"]) as t:
            for m in t:
                parts = Path(m.name).parts
                if m.isfile() and len(parts) == 2 and LIC_RX.match(parts[1]) and m.size < 512_000:
                    files[f"licenses/{name}/{parts[1]}"] = t.extractfile(m).read()

    deps = meta.get("maven_dependencies", [])
    notices += ["", "## Java libraries inside the executable (resolved by Gradle at build time)", "",
                "Not pinned in the lock file; recorded by SHA-256. Their licence files, where the jar has one, are in licenses/jars/.", "",
                "| jar | sha256 |", "|---|---|"]
    by_name = {Path(c).name: Path(c) for c in meta.get("classpath", [])}
    for d in deps:
        notices.append(f"| {d['file']} | `{d['sha256']}` |")
        jar = by_name.get(d["file"])
        if jar and jar.is_file():
            for fname, data in jar_licenses(jar).items():
                files[f"licenses/jars/{jar.stem}/{fname}"] = data
    notices += ["", "## Source code", "",
                "NewPipe Extractor is GPL-3.0-or-later and is compiled into the executable. Its exact source is the "
                f"tag v{comps['newpipe-extractor']['version']} (commit {comps['newpipe-extractor']['commit']}) of "
                "https://github.com/TeamNewPipe/NewPipeExtractor; the helper code is in the newpipe/ folder of this "
                "repository at the commit named in BUILD_INFO.json."]
    files["THIRD_PARTY_NOTICES.md"] = ("\n".join(notices) + "\n").encode()
    files["LICENSE"] = (ROOT / "LICENSE").read_bytes() if (ROOT / "LICENSE").exists() else b""

    info = {
        "bundle": "viddown-native-bundle", "kind": "newpipe-helper", "version_tag": a.version_tag, "platform": a.platform,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo_commit": git_sha(),
        "components": used,
        "extractor_jar": meta.get("extractor_jar"),
        "maven_dependencies": deps,
        "toolchain": {k: meta.get(k) for k in ("native_image", "jdk17", "gradle_wrapper", "agent_config_used")},
        "unverified_notes": [
            "Maven dependencies were resolved by Gradle at build time and are recorded here by SHA-256; they are not pinned in native/sources.lock.toml.",
            "This zip exists only because the native executable passed its self-test (JavaScript engine, HTML parser, JSON, protobuf, reflection, one HTTPS request) in CI. Fetching real YouTube data from CI is NOT part of that test.",
        ],
    }
    files["BUILD_INFO.json"] = (json.dumps(info, indent=2) + "\n").encode()
    files["SHA256SUMS"] = "".join(f"{sha(b)}  {n}\n" for n, b in sorted(files.items())).encode()

    out = dist / "release"
    out.mkdir(parents=True, exist_ok=True)
    zpath = out / f"viddown-native-newpipe-{a.platform}-{a.version_tag}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in sorted(files.items()):
            zi = zipfile.ZipInfo(n, date_time=(1980, 1, 1, 0, 0, 0))  # reproducible
            zi.external_attr = (0o755 if n.startswith("bin/") and not n.endswith(".pem") else 0o644) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, b)
    (out / (zpath.name + ".sha256")).write_text(f"{sha(zpath.read_bytes())}  {zpath.name}\n")
    print(f"wrote {zpath} ({zpath.stat().st_size} bytes), {len(files)} files, {len(used)} components with provenance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
