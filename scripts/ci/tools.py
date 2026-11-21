#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Which tools exist, what tag each one is released under, and which ones still need a release.

Every TOOL is released on its own, under a tag taken from ITS OWN version in native/sources.lock.toml:

    yt-dlp   -> yt-dlp-v2026.10.26     (yt-dlp + its pure-Python dependencies; no ABI)
    cpython  -> cpython-v3.14.10       (CPython + OpenSSL + libffi + stdlib; one zip per ABI)
    ffmpeg   -> ffmpeg-v8.1.3          (FFmpeg + FFprobe; one zip per ABI)
    newpipe  -> newpipe-v0.27.1        (NewPipe helper, desktop; one zip per platform)

Nobody types a tag. The tag is always  <tool>-v<version>.

If that tag already exists AND its release was made from the same inputs (same CONTENT ID), the tool is up
to date: nothing is built and nothing is released for it. If the tag exists but was made from different
inputs (for example OpenSSL changed while CPython kept its version), the next free tag is used:
<tool>-v<version>.2, .3, ...

The content id is stored in the release notes as an HTML comment:  <!-- content-id: 0123456789ab -->

usage:
  tools.py tag <tool>                         base tag from the lock file
  tools.py plan [--only-new true|false] [--repo owner/name] [--existing FILE.json] [--summary-md FILE]
        prints the plan as JSON; with GITHUB_OUTPUT set it also writes step outputs
        (plan, any_needed, and <tool>_needed / <tool>_tag / <tool>_ver for ytdlp, cpython, ffmpeg, newpipe).
        --only-new false: every tool is "needed" (a validation build, no release is looked up).
        --only-new true : a tool is needed only when its release does not exist yet (looked up with `gh`,
                          or in --existing, a JSON file {tag: content_id} used by the tests).
  tools.py required-artifacts                 PLAN_JSON in the environment -> artifact names that must exist
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOCK = ROOT / "native" / "sources.lock.toml"

# tag_component: the lock-file component whose version names the tag.
# select: components that go INTO the zip (and so decide the content id).
TOOLS: dict[str, dict] = {
    "yt-dlp": {"tag_component": "yt-dlp", "kinds": ("python-source", "python-pure"), "abi": False, "key": "ytdlp"},
    "cpython": {"tag_component": "cpython", "names": ("cpython", "openssl", "libffi"), "abi": True, "key": "cpython"},
    "ffmpeg": {"tag_component": "ffmpeg", "names": ("ffmpeg",), "abi": True, "key": "ffmpeg"},
    "newpipe": {"tag_component": "newpipe-extractor", "names": ("newpipe-extractor",), "abi": False, "key": "newpipe"},
}
ANDROID_TOOLS = ("yt-dlp", "cpython", "ffmpeg")           # the ones the x86_64 emulator smoke test covers
REQUIRED_ABIS = ("arm64-v8a", "x86_64")                    # armeabi-v7a and x86 are experimental
ALL_ABIS = ("arm64-v8a", "x86_64", "armeabi-v7a", "x86")
CONTENT_ID_RX = re.compile(r"content-id:\s*([0-9a-f]{12})")


class ToolsError(Exception):
    pass


def load_lock(path: Path = LOCK) -> dict:
    return tomllib.loads(Path(path).read_text())["components"]


def components_of(tool: str, lock: dict) -> list[str]:
    spec = TOOLS[tool]
    if "names" in spec:
        missing = [n for n in spec["names"] if n not in lock]
        if missing:
            raise ToolsError(f"lock file has no component {missing} (needed by tool '{tool}')")
        return list(spec["names"])
    return sorted(n for n, c in lock.items() if c.get("kind") in spec["kinds"])


def content_id(tool: str, lock: dict) -> str:
    """Fingerprint of everything that goes into this tool's zips (name, version, source commit)."""
    lines = [f"{n} {lock[n]['version']} {lock[n].get('commit', '')}" for n in sorted(components_of(tool, lock))]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


def tool_version(tool: str, lock: dict) -> str:
    return lock[TOOLS[tool]["tag_component"]]["version"]


def base_tag(tool: str, lock: dict) -> str:
    return f"{tool}-v{tool_version(tool, lock)}"


def version_of_tag(tool: str, tag: str) -> str:
    """'cpython-v3.14.10.2' -> '3.14.10.2' (what goes into the zip names)."""
    prefix = f"{tool}-v"
    if not tag.startswith(prefix):
        raise ToolsError(f"tag '{tag}' does not belong to tool '{tool}'")
    return tag[len(prefix):]


def resolve_tag(tool: str, lock: dict, lookup) -> tuple[str, bool]:
    """(tag, needed). lookup(tag) -> content id of that release, "" if it has none, None if it does not exist."""
    want = content_id(tool, lock)
    base = base_tag(tool, lock)
    tag, i = base, 1
    while i < 100:
        have = lookup(tag)
        if have is None:
            return tag, True            # free: release it under this tag
        if have == want:
            return tag, False           # already released from exactly these inputs
        i += 1
        tag = f"{base}.{i}"
    raise ToolsError(f"more than 100 releases for {base}")


def gh_lookup(repo: str):
    def lookup(tag: str):
        p = subprocess.run(["gh", "release", "view", tag, "--repo", repo, "--json", "body", "--jq", ".body"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            if "not found" in (p.stderr + p.stdout).lower():
                return None
            raise ToolsError(f"could not look up release {tag}: {(p.stderr or p.stdout).strip()}")
        m = CONTENT_ID_RX.search(p.stdout)
        return m.group(1) if m else ""
    return lookup


def file_lookup(path: str):
    data = json.loads(Path(path).read_text())
    return lambda tag: data.get(tag)


def make_plan(lock: dict, only_new: bool, lookup=None) -> dict:
    tools = {}
    for tool in TOOLS:
        cid = content_id(tool, lock)
        if only_new:
            tag, needed = resolve_tag(tool, lock, lookup)
        else:
            tag, needed = base_tag(tool, lock), True
        tools[tool] = {"tag": tag, "version": version_of_tag(tool, tag), "needed": needed, "content_id": cid,
                       "components": {n: lock[n]["version"] for n in components_of(tool, lock)}}
    return {"schema": 1, "only_new": only_new, "tools": tools}


def required_artifacts(plan: dict) -> list[str]:
    """Artifacts that MUST exist for this plan to count as built: the tools that need a release, for the
    required ABIs, and (for the Android tools) the marker uploaded only after the emulator smoke test passed."""
    names: list[str] = []
    for tool, p in plan["tools"].items():
        if not p["needed"] or tool == "newpipe":     # NewPipe is experimental: never required
            continue
        if TOOLS[tool]["abi"]:
            names += [f"viddown-native-{tool}-{abi}" for abi in REQUIRED_ABIS]
        else:
            names.append(f"viddown-native-{tool}")
    if any(plan["tools"][t]["needed"] for t in ANDROID_TOOLS):
        names.append("smoke-passed-x86_64")
    return names


def summary_md(plan: dict) -> str:
    out = ["### Tool releases", "", "| tool | version | tag | action |", "|---|---|---|---|"]
    for tool, p in plan["tools"].items():
        if plan["only_new"]:
            action = "build + release" if p["needed"] else f"up to date (released as `{p['tag']}`)"
        else:
            action = "build only (no release lookup)"
        out.append(f"| {tool} | {p['version']} | `{p['tag']}` | {action} |")
    return "\n".join(out) + "\n"


def write_outputs(plan: dict, path: str) -> None:
    with open(path, "a") as f:
        f.write("plan=" + json.dumps(plan, separators=(",", ":")) + "\n")
        f.write("any_needed=" + str(any(p["needed"] for p in plan["tools"].values())).lower() + "\n")
        for tool, p in plan["tools"].items():
            k = TOOLS[tool]["key"]
            f.write(f"{k}_needed={str(p['needed']).lower()}\n{k}_tag={p['tag']}\n{k}_ver={p['version']}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tag")
    t.add_argument("tool", choices=sorted(TOOLS))
    t.add_argument("--lock", default=str(LOCK))
    p = sub.add_parser("plan")
    p.add_argument("--only-new", default="false", choices=["true", "false"])
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--existing", help="JSON {tag: content_id}; replaces the gh lookup (tests)")
    p.add_argument("--lock", default=str(LOCK))
    p.add_argument("--summary-md")
    sub.add_parser("required-artifacts")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "tag":
            print(base_tag(a.tool, load_lock(Path(a.lock))))
        elif a.cmd == "plan":
            only_new = a.only_new == "true"
            lookup = None
            if only_new:
                if a.existing:
                    lookup = file_lookup(a.existing)
                elif a.repo:
                    lookup = gh_lookup(a.repo)
                else:
                    raise ToolsError("--only-new true needs --repo (or GITHUB_REPOSITORY) or --existing")
            plan = make_plan(load_lock(Path(a.lock)), only_new, lookup)
            print(json.dumps(plan, indent=2))
            if os.environ.get("GITHUB_OUTPUT"):
                write_outputs(plan, os.environ["GITHUB_OUTPUT"])
            if a.summary_md:
                with open(a.summary_md, "a") as f:
                    f.write(summary_md(plan))
        elif a.cmd == "required-artifacts":
            raw = os.environ.get("PLAN_JSON", "")
            if not raw.strip():
                raise ToolsError("PLAN_JSON is empty")
            print("\n".join(required_artifacts(json.loads(raw))))
    except ToolsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
