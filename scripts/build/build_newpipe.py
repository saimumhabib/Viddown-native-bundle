#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the NewPipe helper as a native executable (Windows or Linux) FROM SOURCE.

usage:
  build_newpipe.py extractor --platform PLATFORM --jdk17 DIR
      fetch + verify the pinned NewPipe Extractor and certifi, build the extractor with its own
      Gradle wrapper, compile newpipe/helper against it.      (needs a JDK 17 and git)
  build_newpipe.py native --platform PLATFORM
      run the self-test on the JVM under the native-image tracing agent, compile with GraalVM
      native-image, run the self-test again in the finished executable, stage the files in
      dist/newpipe-PLATFORM.                                  (needs GraalVM on PATH)

PLATFORM is linux-x86_64 or windows-x86_64. Run on the matching OS: native-image does not cross-compile.

Two phases because the JDK that Gradle needs (17, what NewPipe Extractor's build asks for) is not the
JDK that native-image needs (GraalVM), and CI sets them up one after the other.

STATUS: written and unit-tested for its pure functions; the Gradle and native-image steps are verified
only by CI runs (see PROGRESS.txt).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PLATFORMS = {"linux-x86_64": "", "windows-x86_64": ".exe"}
MAIN_CLASS = "viddown.newpipe.Main"
EXE_NAME = "viddown-newpipe"
HELPER_SRC = ROOT / "newpipe" / "helper" / "src" / "main" / "java"
NATIVE_META = ROOT / "newpipe" / "native-image"
INIT_SCRIPT = ROOT / "newpipe" / "gradle" / "copy-runtime-libs.init.gradle"
JAVA_RELEASE = "17"


class BuildError(Exception):
    pass


def log(msg: str) -> None:
    print(f"==> {msg}", file=sys.stderr, flush=True)


def paths(platform: str) -> dict[str, Path]:
    b = ROOT / "build" / "newpipe" / platform
    return {
        "build": b, "extractor_src": b / "extractor-src", "helper_src": b / "helper-src",
        "helper_classes": b / "helper-classes", "cacert": b / "cacert.pem", "state": b / "state.json",
        "agent": b / "agent-config", "native_out": b / "native-out",
        "sources": ROOT / "build" / "sources", "cache": ROOT / ".cache" / "sources",
        "dist": ROOT / "dist" / f"newpipe-{platform}",
    }


def run(cmd: list, cwd: Path | None = None, env: dict | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    log("$ " + " ".join(str(c) for c in cmd))
    try:
        p = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, text=True,
                           stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT if capture else None)
    except FileNotFoundError as e:
        raise BuildError(f"command not found: {cmd[0]}") from e
    return p


def check(p: subprocess.CompletedProcess, what: str) -> None:
    if p.returncode != 0:
        raise BuildError(f"{what} failed with exit code {p.returncode}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------- pure helpers (unit-tested)

def extract_stripped(archive: Path, dest: Path) -> None:
    """Unpack a tarball without its top-level directory. Only plain files and directories are written."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tarfile.open(archive) as t:
        for m in t:
            parts = Path(m.name).parts[1:]
            if not parts or not (m.isfile() or m.isdir()):
                continue  # pax header, the top directory itself, symlinks
            if ".." in parts or Path(*parts).is_absolute():
                raise BuildError(f"unsafe path in {archive.name}: {m.name}")
            m.name = "/".join(parts)
            t.extract(m, dest, filter="data")


def patch_build_info(java_source: str, version: str, commit: str) -> str:
    """Set the two EXTRACTOR_* constants of BuildInfo.java."""
    for const, value in (("EXTRACTOR_VERSION", version), ("EXTRACTOR_COMMIT", commit)):
        java_source, n = re.subn(rf'(static final String {const} = )"[^"]*";', rf'\g<1>"{value}";', java_source)
        if n != 1:
            raise BuildError(f"BuildInfo.java: constant {const} not found exactly once")
    return java_source


def pick_extractor_jar(libs_dir: Path) -> Path:
    jars = [j for j in sorted(libs_dir.glob("extractor-*.jar")) if not re.search(r"-(sources|javadoc)\.jar$", j.name)]
    if len(jars) != 1:
        raise BuildError(f"expected exactly one extractor jar in {libs_dir}, found {[j.name for j in jars]}")
    return jars[0]


def read_wrapper_info(wrapper_properties: Path) -> dict[str, str]:
    props = {}
    for line in wrapper_properties.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            props[k.strip()] = v.strip().replace("\\:", ":")
    return {"distribution_url": props.get("distributionUrl", ""), "distribution_sha256": props.get("distributionSha256Sum", "")}


def classpath_string(entries: list[str]) -> str:
    return os.pathsep.join(entries)


# ----------------------------------------------------------------- helpers using the repo

def lock() -> dict:
    return tomllib.loads((ROOT / "native" / "sources.lock.toml").read_text())["components"]


def fetch(name: str, p: dict[str, Path]) -> Path:
    c = run([sys.executable, ROOT / "scripts" / "fetch_source.py", "--cache-dir", p["cache"], "fetch", name, "--out", p["sources"]])
    check(c, f"fetching '{name}' from a verified source")
    out = subprocess.run([sys.executable, ROOT / "scripts" / "manifest_get.py", "archive", name, p["sources"]],
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def version_line(cmd: list) -> str:
    try:
        p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=60)
        return (p.stdout + p.stderr).strip().splitlines()[0] if (p.stdout + p.stderr).strip() else "unknown"
    except Exception:
        return "unknown"


# ----------------------------------------------------------------- phase 1: extractor + helper

def phase_extractor(platform: str, jdk17: Path) -> None:
    p = paths(platform)
    p["build"].mkdir(parents=True, exist_ok=True)
    comps = lock()
    ex = comps["newpipe-extractor"]

    log(f"NewPipe Extractor {ex['version']} (tag commit {ex['commit']})")
    extract_stripped(fetch("newpipe-extractor", p), p["extractor_src"])
    cert_archive = fetch("py-certifi", p)
    with tarfile.open(cert_archive) as t:
        member = next((m for m in t if m.isfile() and m.name.endswith("/certifi/cacert.pem")), None)
        if member is None:
            raise BuildError("certifi archive has no certifi/cacert.pem")
        p["cacert"].write_bytes(t.extractfile(member).read())

    # The extractor's build.gradle.kts runs `git rev-parse HEAD`; a source archive has no .git.
    src = p["extractor_src"]
    # core.longpaths=true: on Windows, git refuses paths over 260 characters ("Filename too long") and
    # the extractor's test mocks are deeper than that. Passed with -c on every call (so it does not
    # depend on any runner config) and also stored in the repo's own config for Gradle's git calls.
    git = ["git", "-c", "core.longpaths=true", "-c", "user.name=viddown-ci", "-c", "user.email=ci@localhost"]
    steps = (
        ("git init", [*git, "init", "-q"]),
        ("git config core.longpaths", [*git, "config", "core.longpaths", "true"]),
        ("git add", [*git, "add", "-A"]),
        ("git commit", [*git, "commit", "-q", "-m", f"NewPipeExtractor v{ex['version']} (verified archive)"]),
    )
    for what, cmd in steps:
        check(run(cmd, cwd=src), what)

    env = dict(os.environ, JAVA_HOME=str(jdk17), PATH=str(jdk17 / "bin") + os.pathsep + os.environ["PATH"])
    wrapper = [str(src / "gradlew.bat")] if platform.startswith("windows") else ["sh", str(src / "gradlew")]
    gradle_args = ["--no-daemon", "--console=plain", "--init-script", str(INIT_SCRIPT), ":extractor:jar", ":extractor:copyRuntimeLibs"]
    check(run([*wrapper, *gradle_args], cwd=src, env=env), "Gradle build of the extractor")

    ext_jar = pick_extractor_jar(src / "extractor" / "build" / "libs")
    libs = sorted((src / "extractor" / "build" / "runtime-libs").glob("*.jar"))
    if not libs:
        raise BuildError("copyRuntimeLibs produced no jars")
    classpath = [str(ext_jar), *map(str, libs)]

    # helper sources, with the version of the pinned extractor written into BuildInfo.java
    if p["helper_src"].exists():
        shutil.rmtree(p["helper_src"])
    shutil.copytree(HELPER_SRC, p["helper_src"])
    bi = next(p["helper_src"].rglob("BuildInfo.java"))
    bi.write_text(patch_build_info(bi.read_text(), ex["version"], ex["commit"]))
    sources = sorted(p["helper_src"].rglob("*.java"))
    if p["helper_classes"].exists():
        shutil.rmtree(p["helper_classes"])
    p["helper_classes"].mkdir(parents=True)
    javac = jdk17 / "bin" / ("javac.exe" if platform.startswith("windows") else "javac")
    check(run([javac, "--release", JAVA_RELEASE, "-encoding", "UTF-8", "-Xlint:all", "-d", p["helper_classes"],
               "-cp", classpath_string(classpath), *sources]), "compiling the helper")

    state = {
        "platform": platform, "extractor_version": ex["version"], "extractor_commit": ex["commit"],
        "classpath": classpath, "helper_classes": str(p["helper_classes"]),
        "maven_dependencies": [{"file": j.name, "sha256": sha256_file(j)} for j in libs],
        "extractor_jar": {"file": ext_jar.name, "sha256": sha256_file(ext_jar)},
        "gradle_wrapper": read_wrapper_info(src / "gradle" / "wrapper" / "gradle-wrapper.properties"),
        "jdk17": version_line([jdk17 / "bin" / "java", "-version"]),
    }
    p["state"].write_text(json.dumps(state, indent=2) + "\n")
    log(f"helper compiled; {len(libs)} runtime jars; state in {p['state']}")


# ----------------------------------------------------------------- phase 2: native image

def parse_selftest(output: str) -> dict:
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise BuildError("the self-test printed no JSON")


def run_selftest(cmd: list, what: str) -> dict:
    p = run(cmd, capture=True)
    sys.stderr.write(p.stdout or "")
    result = parse_selftest(p.stdout or "") if p.returncode in (0, 2) else {}
    if p.returncode != 0 or not result.get("ok"):
        raise BuildError(f"self-test ({what}) did not pass: {json.dumps(result.get('checks', result))}")
    log(f"self-test ({what}): ok {json.dumps(result['checks'])}")
    return result


def phase_native(platform: str) -> None:
    p = paths(platform)
    state = json.loads(p["state"].read_text())
    cp = classpath_string([*state["classpath"], state["helper_classes"]])
    java, ni = shutil.which("java"), shutil.which("native-image")
    if not java or not ni:
        raise BuildError("java and native-image must be on PATH (GraalVM)")
    selftest_args = [MAIN_CLASS, "selftest", "--cacert", p["cacert"]]

    # 1. the self-test on the JVM, recorded by the tracing agent (finds what the static metadata missed)
    if p["agent"].exists():
        shutil.rmtree(p["agent"])
    agent_ok = False
    try:
        run_selftest([java, f"-agentlib:native-image-agent=config-output-dir={p['agent']}", "-cp", cp, *selftest_args], "JVM + tracing agent")
        agent_ok = any(p["agent"].glob("*.json"))
    except BuildError as e:
        log(f"WARNING: agent run failed ({e}); checking whether the self-test passes without the agent")
        run_selftest([java, "-cp", cp, *selftest_args], "plain JVM")
        log("WARNING: continuing with the static metadata only")

    # 2. compile
    if p["native_out"].exists():
        shutil.rmtree(p["native_out"])
    p["native_out"].mkdir(parents=True)
    # -o with a plain file name + cwd = the output folder: the documented way (a path in -o is not documented).
    # --enable-url-protocols=http,https: java.net.http (our downloader) does not need URL handlers, but
    # NewPipe Extractor parses every address with java.net.URL, which does. Without them a native build
    # refuses all addresses ("No service can handle the url"); selftest "url-handler" and "service-urls"
    # check this.
    cmd = [ni, "-cp", classpath_string([cp, str(NATIVE_META)]), "--no-fallback", "-Os", "--color=never",
           "--enable-url-protocols=http,https", "-o", EXE_NAME]
    if agent_ok:
        cmd.append(f"-H:ConfigurationFileDirectories={p['agent']}")
    cmd.append(MAIN_CLASS)
    check(run(cmd, cwd=p["native_out"]), "native-image")
    exe = p["native_out"] / (EXE_NAME + PLATFORMS[platform])
    if not exe.is_file():
        raise BuildError(f"native-image did not produce {exe}")

    # 3. the finished executable must pass the same checks
    run_selftest([exe, "selftest", "--cacert", p["cacert"]], "native executable")

    # 4. stage
    out = p["dist"]
    if out.exists():
        shutil.rmtree(out)
    (out / "bin" / "newpipe").mkdir(parents=True)
    (out / "metadata").mkdir()
    shutil.copy2(exe, out / "bin" / "newpipe" / exe.name)
    shutil.copy2(p["cacert"], out / "bin" / "newpipe" / "cacert.pem")
    state.update(native_image=version_line([ni, "--version"]), agent_config_used=agent_ok)
    (out / "metadata" / "newpipe-build.json").write_text(json.dumps(state, indent=2) + "\n")
    log(f"staged {out} ({exe.stat().st_size} bytes)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["extractor", "native"])
    ap.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    ap.add_argument("--jdk17", help="JDK 17 home (phase 'extractor')")
    a = ap.parse_args(argv)
    try:
        if a.phase == "extractor":
            if not a.jdk17:
                raise BuildError("--jdk17 is required for phase 'extractor'")
            phase_extractor(a.platform, Path(a.jdk17))
        else:
            phase_native(a.platform)
    except BuildError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
