#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Publish one GitHub Release per tool that needs one, then refresh the rolling `index` release.

usage: publish_releases.py --plan PLAN_JSON --assets DIR --lock native/sources.lock.toml --repo owner/name
                           [--target COMMIT] [--pr N] [--run-url URL] [--tmp DIR]

PLAN_JSON is the output of `tools.py plan` (a JSON string or the path of a file). For every tool with
"needed": true the files viddown-native-<tool>-*-<version>.zip are taken from --assets and released under the
tag the plan names (<tool>-v<version>, tools.py explains the .2/.3 suffix). Needs the `gh` CLI.

Exit code: 0 = everything that had to be released was released (or nothing had to be); 2 = refused, nothing
published, the reason is printed (markdown) on stdout; anything else = unexpected error.
A refusal is all-or-nothing for the REQUIRED tools (yt-dlp, cpython, ffmpeg). NewPipe is experimental: without
its zips it is just left out, like before.

`index` is a rolling release holding components.json: for every tool the tag and the asset names of the
newest release, so an app needs ONE small download to learn what to fetch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_notes  # noqa: E402
import tools  # noqa: E402

INDEX_TAG = "index"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {(p.stderr or p.stdout).strip()}")
    return p


def assets_of(tool: str, version: str, assets_dir: Path) -> list[Path]:
    if tool == "yt-dlp":                       # no ABI in the name
        return sorted(assets_dir.glob(f"viddown-native-yt-dlp-{version}.zip"))
    return sorted(assets_dir.glob(f"viddown-native-{tool}-*-{version}.zip"))   # <abi> or <platform> in the middle


def missing_for(tool: str, found: list[Path], version: str) -> list[str]:
    if tool == "yt-dlp":
        return [] if found else [f"the {tool} zip (viddown-native-yt-dlp-{version}.zip)"]
    if tool == "newpipe":
        return [] if found else ["any NewPipe helper zip"]
    names = {p.name for p in found}
    return [f"the {abi} zip of {tool}" for abi in tools.REQUIRED_ABIS
            if f"viddown-native-{tool}-{abi}-{version}.zip" not in names]


def asset_template(tool: str, version: str) -> str:
    if tool == "yt-dlp":
        return f"viddown-native-yt-dlp-{version}.zip"
    if tool == "newpipe":
        return f"viddown-native-newpipe-{{platform}}-{version}.zip"
    return f"viddown-native-{tool}-{{abi}}-{version}.zip"


def build_index(plan: dict, published: set[str], repo: str) -> dict:
    out: dict = {}
    for tool, p in plan["tools"].items():
        exists = (not p["needed"]) or tool in published   # unchanged tools already have their release
        if not exists:
            continue
        entry = {"version": p["version"], "tag": p["tag"], "content_id": p["content_id"],
                 "base_url": f"https://github.com/{repo}/releases/download/{p['tag']}/",
                 "asset": asset_template(tool, p["version"])}
        if tools.TOOLS[tool]["abi"]:
            entry["abis_required"] = list(tools.REQUIRED_ABIS)
            entry["abis_experimental"] = [a for a in tools.ALL_ABIS if a not in tools.REQUIRED_ABIS]
        if tool == "newpipe":
            entry["platforms"] = ["linux-x86_64", "windows-x86_64"]
        out[tool] = entry
    return {"schema": 1, "repo": repo, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "Each tool is released on its own. Experimental ABIs may be missing from a release.",
            "tools": out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--assets", required=True)
    ap.add_argument("--lock", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--target", default="")
    ap.add_argument("--pr")
    ap.add_argument("--run-url")
    ap.add_argument("--tmp", default="")
    a = ap.parse_args()

    raw = a.plan
    # a JSON string (what the workflow passes) or the path of a file holding it
    plan = json.loads(raw if raw.lstrip().startswith("{") else Path(raw).read_text())
    lock = tomllib.loads(Path(a.lock).read_text())
    assets_dir = Path(a.assets)
    tmp = Path(a.tmp) if a.tmp else Path(tempfile.mkdtemp())
    tmp.mkdir(parents=True, exist_ok=True)

    todo = {t: p for t, p in plan["tools"].items() if p["needed"]}
    if not todo:
        print("Nothing to release: every tool already has a release made from the same inputs.")
        return 0

    # 1. check everything BEFORE publishing anything: a refusal must not leave half a release set behind
    found: dict[str, list[Path]] = {}
    problems: list[str] = []
    skipped: list[str] = []
    for tool, p in todo.items():
        found[tool] = assets_of(tool, p["version"], assets_dir)
        miss = missing_for(tool, found[tool], p["version"])
        if miss and tool == "newpipe":
            skipped.append(tool)
        elif miss:
            problems.append(f"`{p['tag']}`: missing " + ", ".join(miss))
    if problems:
        print("\n".join(f"- {x}" for x in problems))
        return 2

    # 2. publish
    published: list[str] = []
    for tool, p in todo.items():
        if tool in skipped:
            continue
        files = found[tool]
        d = tmp / tool
        d.mkdir(parents=True, exist_ok=True)
        sums = d / "SHA256SUMS.txt"
        sums.write_text("".join(f"{sha256(f)}  {f.name}\n" for f in files))
        notes = d / "notes.md"
        notes.write_text(release_notes.build_notes(tool, lock, p["tag"], [f.name for f in files], a.pr, a.run_url, a.repo))
        cmd = ["release", "create", p["tag"], *map(str, files), str(sums), "--repo", a.repo, "--title", p["tag"],
               "--notes-file", str(notes), "--latest=false"]
        if a.target:
            cmd += ["--target", a.target]
        gh(*cmd)
        published.append(tool)
        print(f"- Released **{p['tag']}**: https://github.com/{a.repo}/releases/tag/{p['tag']}")
    for tool in skipped:
        print(f"- `{todo[tool]['tag']}` was **not** released: the NewPipe helper did not produce a passing native executable.")

    # 3. the index
    index = build_index(plan, set(published), a.repo)
    ipath = tmp / "components.json"
    ipath.write_text(json.dumps(index, indent=2) + "\n")
    if gh("release", "view", INDEX_TAG, "--repo", a.repo, check=False).returncode != 0:
        cmd = ["release", "create", INDEX_TAG, "--repo", a.repo, "--title", "index (components.json)",
               "--notes", "Rolling release: components.json lists, for every tool, the tag and asset names of its newest "
                          "release. Updated automatically; contains no binaries.", "--latest=false"]
        if a.target:
            cmd += ["--target", a.target]
        gh(*cmd)
    gh("release", "upload", INDEX_TAG, str(ipath), "--repo", a.repo, "--clobber")
    print(f"- Updated the `{INDEX_TAG}` release (components.json).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
