#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the packaged native NewPipe helper the way the app would find it.

usage: newpipe_smoke.py <viddown-native-newpipe-PLATFORM-TAG.zip> [--no-probe]

Gating checks (exit 1 if one fails):
  1. every file matches SHA256SUMS
  2. "version" reports the extractor version and commit pinned in native/sources.lock.toml
  3. "info" on an address no service handles fails the way the app expects: exit 2, JSON with "error"
  4. "info" on a YouTube address must NOT fail because the address is not recognised. It is run through a
     dead proxy, so it fails fast and offline with a network error, and only a "No service can handle" /
     "Malformed url" answer fails this check. (Check 3 alone passes even when the helper refuses every
     address, which is how a build that recognised nothing once got released.)
  5. "selftest" WITHOUT --cacert passes, and its TLS check used the cacert.pem next to the executable
     (this is the packaged layout: it proves the helper finds its CA bundle by itself)

Informational (never fails): one real YouTube fetch. CI addresses are often blocked by YouTube, so
the outcome is only written to the job summary.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PROBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"   # the first video ever uploaded to YouTube
PARSE_FAILURES = ("No service can handle", "Malformed url")   # the helper did not even recognise the address


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run_helper(exe: Path, *args: str, timeout: int = 180) -> tuple[int, dict | None, str]:
    p = subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=timeout)
    last = next((l for l in reversed(p.stdout.strip().splitlines()) if l.strip().startswith("{")), None)
    try:
        data = json.loads(last) if last else None
    except json.JSONDecodeError:
        data = None
    return p.returncode, data, p.stderr[-2000:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("--no-probe", action="store_true", help="skip the informational YouTube fetch")
    a = ap.parse_args()
    lock = tomllib.loads((ROOT / "native" / "sources.lock.toml").read_text())["components"]["newpipe-extractor"]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(a.zip) as z:
            z.extractall(root)
        exe = next(iter((root / "bin" / "newpipe").glob("viddown-newpipe*")), None)
        if exe is None:
            fail("no viddown-newpipe executable under bin/newpipe")
        exe.chmod(0o755)

        # 1. hashes
        for line in (root / "SHA256SUMS").read_text().splitlines():
            digest, _, name = line.partition("  ")
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
                fail(f"SHA256SUMS mismatch for {name}")
        print("ok: SHA256SUMS")

        # 2. version
        rc, data, err = run_helper(exe, "version")
        if rc != 0 or not data:
            fail(f"'version' exit {rc}: {err}")
        if data.get("extractor_version") != lock["version"] or data.get("extractor_commit") != lock["commit"]:
            fail(f"binary says {data.get('extractor_version')}/{data.get('extractor_commit')}, lock says {lock['version']}/{lock['commit']}")
        print(f"ok: version {data['extractor_version']} ({data['extractor_commit'][:10]})")

        # 3. error contract
        rc, data, err = run_helper(exe, "info", "https://example.invalid/not-a-video")
        if rc != 2 or not data or "error" not in data:
            fail(f"'info' on an unsupported address: exit {rc}, json {data}, stderr {err}")
        print(f"ok: unsupported address -> exit 2, error '{str(data['error'])[:60]}'")

        # 4. a YouTube address must be recognised (offline: the dead proxy makes the request fail at once)
        for addr in (PROBE_URL, "https://youtu.be/jNQXAC9IVRw"):
            rc, data, err = run_helper(exe, "info", addr, "--noplaylist", "--proxy", "http://127.0.0.1:9", timeout=120)
            msg = str((data or {}).get("error", ""))
            if any(f in msg for f in PARSE_FAILURES):
                fail(f"'info' on the YouTube address {addr}: the helper does not recognise it: {msg[:200]}")
            if rc not in (0, 2) or not data:
                fail(f"'info' on the YouTube address {addr}: exit {rc}, json {data}, stderr {err}")
        print("ok: YouTube addresses are recognised (the request itself fails offline, as intended)")

        # 5. self-test, CA bundle discovered next to the executable
        rc, data, err = run_helper(exe, "selftest")
        if rc != 0 or not data or not data.get("ok"):
            fail(f"selftest exit {rc}: {json.dumps(data.get('checks') if data else None)} {err}")
        expected = str(root / "bin" / "newpipe" / "cacert.pem")
        tls = str(data["checks"].get("tls", ""))
        if os.path.normcase(expected) not in os.path.normcase(tls):
            fail(f"TLS check did not use the packaged cacert.pem: {tls}")
        print(f"ok: selftest {json.dumps(data['checks'])}")

        # informational probe
        summary = []
        if not a.no_probe:
            try:
                rc, data, err = run_helper(exe, "info", PROBE_URL, "--noplaylist", timeout=120)
                if rc == 0 and data:
                    v, au = len(data.get("video_streams") or []), len(data.get("audio_streams") or [])
                    summary.append(f"YouTube probe ({PROBE_URL}): OK, '{data.get('title')}', {v} video + {au} audio streams")
                else:
                    summary.append(f"YouTube probe: failed (exit {rc}): {(data or {}).get('error', err)[:300]}")
            except Exception as e:   # timeouts etc.: informational only
                summary.append(f"YouTube probe: could not run ({e})")
            print("info: " + summary[-1])
        out = os.environ.get("GITHUB_STEP_SUMMARY")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"### NewPipe helper smoke test ({Path(a.zip).name})\n\n- gating checks: all passed\n")
                for s in summary:
                    f.write(f"- (informational, not gating) {s}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
