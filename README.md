# viddown-native-bundle

Builds, **from source** and per Android ABI, the native/Python runtime used by the VidDown app's
yt-dlp engine: CPython, OpenSSL, libffi, FFmpeg/FFprobe, and yt-dlp with its pure-Python
dependencies. It also builds the desktop **NewPipe helper**: NewPipe Extractor compiled with GraalVM
native-image into one Windows and one Linux executable that needs no Java. No prebuilt wrappers, no third-party binaries (`youtubedl-android`, `ffmpeg-kit`, etc.
are not used). Every upstream source is pinned and hash-verified and fetched through an ordered
list of official sources with automatic fallback ([docs/sources.md](docs/sources.md)).

The VidDown app repo consumes the zips this repo publishes.

## Status (honest)

| part | state |
|---|---|
| `fetch_source.py` (fallback, hash/commit verification, cache, health check) | **implemented and tested** locally: 31 unit tests (fetch script + update helper) against a local server that simulates refused connection, 404, 500, timeout, truncated file, wrong checksum, tampered cache, moved tag |
| `update_source.py`, `sources.lock.toml` | **run against the real upstreams**; 14 components in the lock (13 + newpipe-extractor); sources the sandbox could not reach are `UNPINNED` (skipped until pinned); the main hashes were cross-checked against independent downloads |
| yt-dlp + pure dependencies bundle | **assembled and imported on a host Python** (1754 extractors load) |
| OpenSSL/libffi patches | **dry-run applied** to the pinned sources |
| FFmpeg configure component names | **validated** against the real FFmpeg 8.1.3 source |
| `package.py` (BUILD_INFO, notices, licence files, checksums) | **run** with real provenance |
| CPython / OpenSSL / libffi / FFmpeg **cross-builds for Android** | **written, NOT yet run.** Needs the Android NDK; first CI run decides |
| emulator smoke test | **written, NOT yet run** |
| auto-update pipeline | gate and release logic **tested locally** (17 gate + 6 per-tool release scenarios with a fake `gh` and real git repos, tag/plan/packaging unit tests, 14 policy tests, actionlint + shellcheck clean). The GitHub wiring itself (reusable-workflow call, PR refresh, real merge) is **not yet run** |
| `newpipe-extractor` component | **pinned against the real upstream** (v0.27.1; tag archive hash and commit verified, the archive's own commit equals the lock's); a version bump passes the lock policy, adding it was flagged as a new component |
| NewPipe helper sources (`newpipe/helper`) | **compiled against the real v0.27.1 sources** and the self-test **run on a JVM** here. Caveat: the sandbox had only Ubuntu's older jsoup / Rhino / protobuf, and Rhino needed a one-line shim, so this shows the helper's own logic and the extractor API match, not the real dependency set |
| `build_newpipe.py` (pure functions), `package_newpipe.py`, `newpipe_smoke.py` | **run**: 11 unit tests, including packaging and the smoke script against a fake helper |
| NewPipe: Gradle build of the extractor, GraalVM **native-image** (Linux + Windows), the self-test inside the native executable | **written, NOT yet run.** The sandbox cannot reach Maven or GraalVM. Reflection / resource metadata for native-image is a first guess; the first CI run decides |
| ABIs verified | **none yet.** This table will list only ABIs that passed the smoke test |

## Layout

```
native/source-templates.toml   where each component may come from (ordered) + how to find new versions
native/sources.lock.toml       generated pins (versions, URLs, sha256, tag commits)
native/launcher/               tiny C launcher that runs the interpreter as a process
patches/                       vendored OpenSSL/libffi patches (MIT, provenance in patches/README.md)
scripts/fetch_source.py        fetch + verify + fallback + health check
scripts/update_source.py       propose version/hash updates
scripts/check_lock_update.py   safety policy a machine-made lock change must satisfy
scripts/ci/gate_update.sh      decides: merge the update PR or explain why not
scripts/ci/tools.py            the tools, their tags (<tool>-v<version>) and which of them still need a release
scripts/ci/publish_releases.py one GitHub Release per tool + the rolling `index` release (components.json)
scripts/ci/release_update.sh   releases for a merged update PR (tree-equality check, then publish_releases.py)
scripts/ci/release_notes.py    per-tool release notes that state only what was verified
scripts/build/                 build_python.sh, build_ffmpeg.sh, build_python_packages.sh
scripts/package.py             ONE zip per tool: BUILD_INFO.json + THIRD_PARTY_NOTICES.md + licences + SHA256SUMS
scripts/build/build_newpipe.py NewPipe helper: pinned extractor -> Gradle -> helper -> native-image (Windows/Linux)
scripts/package_newpipe.py     zip of the native NewPipe helper with the same provenance files
scripts/smoke/                 emulator smoke test, NewPipe helper smoke test, local fixture server
newpipe/helper/                the helper's Java sources (info / selftest / version), CA-bundle loading
newpipe/native-image/          reachability metadata for native-image (reflection, resources)
newpipe/gradle/                init script that copies the extractor's runtime jars
.github/workflows/             ci, build-native, source-health (weekly), update-sources (weekly)
```

## Use

```sh
python3 -m unittest discover -s scripts/tests -v     # fallback tests, no network needed
python3 scripts/fetch_source.py list                 # components, order, pin status
python3 scripts/fetch_source.py fetch yt-dlp         # fetch + verify one component
python3 scripts/fetch_source.py check                # is every source reachable and matching its hash?
python3 scripts/update_source.py discover            # newer upstream versions?
```

Native builds run in GitHub Actions (`build-native`): Actions -> build-native -> Run workflow.
Locally you need Linux, `ANDROID_HOME` with `cmdline-tools/latest` (see CPython's `Android/README.md` for
the exact requirements), and the packages listed in the workflow, then `scripts/build/build_python.sh <abi>`.

## Output: one release PER TOOL

Every tool is built, packaged and released on its own, under a tag that **is the tool's own version** from
`native/sources.lock.toml`. Nobody types a tag.

| release tag | zips | contains |
|---|---|---|
| `yt-dlp-v<version>` | `viddown-native-yt-dlp-<version>.zip` | yt-dlp + its pure-Python dependencies (`assets/ytdlp-site-packages.zip`); no ABI |
| `cpython-v<version>` | `viddown-native-cpython-<abi>-<version>.zip` | CPython + OpenSSL + libffi + stdlib (`jniLibs/<abi>/*.so`, `assets/python-lib-*.zip`) |
| `ffmpeg-v<version>` | `viddown-native-ffmpeg-<abi>-<version>.zip` | FFmpeg + FFprobe (`jniLibs/<abi>/libffmpeg.so`, `libffprobe.so`) |
| `newpipe-v<version>` | `viddown-native-newpipe-<platform>-<version>.zip` | NewPipe helper for desktop (below) |

Every zip has `BUILD_INFO.json` (version, **source actually used** and verified hashes per component),
`THIRD_PARTY_NOTICES.md`, `licenses/` and `SHA256SUMS`; each release also has `SHA256SUMS.txt`. The three Android zips
extract into ONE tree (`jniLibs/<abi>/`, `assets/`), which is how the emulator smoke test runs them together.

Rules (`scripts/ci/tools.py`):

- A tool is built and released only if no release exists yet for its version **made from the same inputs**. A release
  records a `content-id` (a hash of the versions + commits of everything inside it) in its notes. yt-dlp moves often
  and is a small pure-Python zip, so a yt-dlp update no longer rebuilds CPython or FFmpeg.
- Same tool version but different inputs (for example OpenSSL changed, CPython did not): the tag gets `.2`, `.3`, ...
  (`cpython-v3.14.10.2`).
- The emulator smoke test always runs the three Android tools **together**. A tool that was not rebuilt is taken from
  its existing release, so the tested combination is the one an app would download.
- Pull requests build everything and never release.
- `index` is a rolling release holding `components.json`: for every tool the newest tag, version and asset name
  pattern (`base_url` + `asset`, with `{abi}`/`{platform}` placeholders). An app needs one small download to learn
  what to fetch: `https://github.com/<owner>/<repo>/releases/download/index/components.json`.

## Output (NewPipe helper, desktop)

`viddown-native-newpipe-<linux-x86_64|windows-x86_64>-<version>.zip` (release `newpipe-v<version>`) containing `bin/newpipe/viddown-newpipe[.exe]`,
`bin/newpipe/cacert.pem` (certifi, found by the helper next to itself), `BUILD_INFO.json`,
`THIRD_PARTY_NOTICES.md`, `licenses/` (including the licence files found inside the Java jars) and `SHA256SUMS`.
Commands: `info <url> [--proxy URL] [--limit N] [--noplaylist]`, `selftest`, `version`.

How a version gets in: `update-sources` finds the newest `vX.Y.Z` tag of NewPipe Extractor like it does for the other
components, pins it, builds that exact commit and, if the PR is merged, `newpipe-v<version>` is released. The
`newpipe` job is **experimental** for now (like armeabi-v7a): if it fails, the Android update still goes ahead and the
release of the other tools still goes ahead and the helper is simply not released that time. A zip exists only if the native executable passed its self-test (JavaScript
engine, HTML parser, JSON, protobuf, reflection, one HTTPS request) and the packaged-layout check. A real YouTube fetch
is **not** part of that gate: CI addresses are often blocked, so it is only reported in the job summary.

The Java libraries NewPipe Extractor depends on (jsoup, Rhino, protobuf-javalite, nanojson, jsr305) are resolved by
Gradle at build time from Maven Central / JitPack, not pinned in the lock: their SHA-256 is recorded in
`BUILD_INFO.json`. Gradle itself is verified by the extractor's own wrapper checksum.

## Updates (automatic, with a manual option)

`update-sources` (Mondays, or "Run workflow") does the whole chain:

1. finds newer yt-dlp / CPython (3.14.x) / FFmpeg (8.1.x) / NewPipe Extractor versions; dependency versions come from
   yt-dlp's own `pin` list and CPython's Android dependency list; computes the hashes;
2. opens (or refreshes) the PR `auto/update-sources`;
3. **builds that exact commit** for all ABIs (`build-native` called as a reusable workflow) including the
   x86_64 emulator smoke tests (API 24 and 34);
4. a **gate** merges the PR only if ALL hold: both required ABIs (arm64-v8a, x86_64) produced their
   artifact (x86_64 only after the smoke tests passed), unit tests pass, the lock change satisfies the
   safety policy (`scripts/check_lock_update.py`: no moved tag, no changed hash for an existing version,
   no series jump, no downgrade, no new/removed component or source, at least 2 verifiable sources),
   and the PR branch did not move while building. Otherwise the PR stays open and a comment lists why.

Switches (default is SAFE = never merge by itself):

| how it runs | auto-merge is on when |
|---|---|
| scheduled | repository variable `AUTO_MERGE_SOURCES` = `true` (Settings > Secrets and variables > Actions > Variables) |
| manual "Run workflow" | the `auto_merge` box is ticked |

5. **Automatic release.** After an automatic merge, the `release` job publishes one GitHub Release **per tool whose
   version is new** in that PR (`yt-dlp-v...`, `cpython-v...`, `ffmpeg-v...`, `newpipe-v...`), from the files that were
   **already built and smoke-tested** for that PR, each with `SHA256SUMS.txt` and honest notes (only x86_64 is stated as
   executed in CI; other ABIs as "built, not run-tested"), and refreshes `index`. The gate only requires the artifacts of
   the tools that changed. It releases only if the merged code's git tree is identical to the tree that was built; if
   `main` moved meanwhile it does not release and says so on the PR (then run `build-native`). It reuses the build, so no
   second hour-long build. If a required zip is missing, **nothing** is released (all or nothing).
   Switch: scheduled runs release unless the repository variable `AUTO_RELEASE_SOURCES` is `false`; manual runs
   use the `auto_release` box (default on). A release happens only after an automatic merge; if you merge by
   hand, run `build-native`.

Manual updating stays possible at any time: run `update-sources` without `auto_merge` (it opens the PR and
builds it, you merge), or use `python3 scripts/update_source.py pin --component NAME --version X --write`.
Manual release: run the `build-native` workflow ("Run workflow", the `release` box is on by default). It builds only the
tools whose version has no release yet and releases them with their own version as tag; no tag to push or type.

Caveats: arm64-v8a is built but not run in CI (the emulator is x86_64), so auto-merge proves arm64 compiles,
not that it runs. If `main` has branch protection that requires reviews, GitHub refuses the merge and the
gate says so in a comment.

## Known limitations

See [docs/architecture.md](docs/architecture.md): no JavaScript runtime bundled yet, `brotli` and
`pycryptodomex` deferred, some stdlib modules absent, FFmpeg is remux-only, unverified ABIs.

## Licence

GPL-3.0-or-later (see `LICENSE`). Third-party licences: `docs/architecture.md` and, per build,
`THIRD_PARTY_NOTICES.md` inside each zip.
