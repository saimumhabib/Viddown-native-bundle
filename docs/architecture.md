# Native bundle: architecture and decisions

Scope of THIS repo: build, from source and per ABI, the native/Python parts the VidDown app needs
for its yt-dlp engine, and publish them as versioned, checksummed zips with full provenance.
The app repo consumes these zips. NewPipe Extractor is a JVM library; for desktop it is built here too, together
with a small helper (`newpipe/helper`), into a native Windows/Linux executable (see "NewPipe helper" below), so the
desktop app needs no Java for it. (Earlier decision: keep it as a submodule in the app repo. Replaced because the
app's own copy was pinned to an old release and YouTube changes often.)

## What is built

| output | how | notes |
|---|---|---|
| `libviddown_python.so` | tiny C launcher (`native/launcher/viddown_python.c`) calling `Py_BytesMain` | an executable named lib*.so so Android installs it in `nativeLibraryDir`, the only place an app may exec from (targetSdk >= 29) |
| `libpython3.14.so`, stdlib zip | CPython `Android/android.py` from the pinned source | shared libpython; stdlib is extracted by the app to its files dir |
| `libssl_python.so`, `libcrypto_python.so` | OpenSSL 3.5.9 from source + CPython's patches | renamed so they never clash with Android's own libssl/libcrypto |
| libffi (static) | from source | for `_ctypes` |
| `libffmpeg.so`, `libffprobe.so` | FFmpeg 8.1.3 from source, `--disable-everything` + minimal list | LGPL gate in the build script fails if GPL/nonfree sneaks in |
| `ytdlp-site-packages.zip` | pure-Python sources of yt-dlp + deps, copied from verified archives | no pip, no build backends from the network |

## Runtime layout the app must reproduce

```
<filesDir>/python/lib/python3.14/   <- extract assets/python-lib-3.14-<abi>.zip HERE (zip root = contents of python3.14/)
<filesDir>/site/                    <- extract assets/ytdlp-site-packages.zip here
<nativeLibraryDir>/                 <- jniLibs: libviddown_python.so, libpython3.14.so, libssl_python.so, ...
env: PYTHONHOME=<filesDir>/python  PYTHONPATH=<filesDir>/site  LD_LIBRARY_PATH=<nativeLibraryDir>
     SSL_CERT_FILE=<filesDir>/site/certifi/cacert.pem  HOME/TMPDIR=<app-private dirs>
```

Extracting the stdlib zip anywhere else makes Python abort with "Failed to import encodings module"
(this is exactly what the first smoke test run hit). The emulator smoke test uses the same layout.

## Why a separate process (not an embedded interpreter in the app process)

* Cancel = kill the process tree; a crash in Python/FFmpeg cannot take the app down.
* Python is loaded only when yt-dlp is the selected engine (the engine model requires this).
* Avoids JNI/GIL lifetime issues in a long-lived app process.

## Known limitations and open decisions

1. **JavaScript runtime.** Current yt-dlp lists `yt-dlp-ejs` and needs an external JS runtime (deno /
   node / bun / quickjs) for some extractors (notably YouTube). The pure-Python solver package is
   bundled, the runtime is **not**. Candidate: build QuickJS(-ng) from source as one more component.
   Decision needed in Phase 7; until then some sites will report a clear "JS runtime missing" error.
2. **Native Python deps deferred:** `brotli` and `pycryptodomex` need compilation. yt-dlp works
   without them (pure-Python AES fallback, no brotli encoding), just slower for encrypted HLS.
3. **Missing stdlib modules:** `bz2`, `lzma`, `_zstd`, `sqlite3` (their C libraries are not built).
4. **FFmpeg is remux-only:** no network protocols, no HLS demuxer, no mp3 encoder (needs libmp3lame).
   Extracting audio works as stream copy; `--audio-format mp3` will not.
5. **ABIs.** CPython's own CI covers arm64-v8a and x86_64. armeabi-v7a and x86 are built but marked
   experimental until they pass the smoke test. arm64-v8a can be built in CI but the smoke test runs
   on an x86_64 emulator, so arm64 binaries still need a run on a real device / ARM emulator.
6. **minSdk 24** (CPython's default `ANDROID_API_LEVEL`).
7. **Execution from the app sandbox** (nativeLibraryDir, `extractNativeLibs=true`, dlopen of stdlib
   `.so` from the files dir) is verified by the instrumented test in the app repo, not here.
8. **GitHub archive tarballs** are not guaranteed byte-stable, hence the `sha256_uncompressed` pin and
   the commit check; PyPI/release assets use plain `sha256`.

## NewPipe helper (desktop)

| piece | how | notes |
|---|---|---|
| `newpipe-extractor` | pinned tag archive + git commit, verified like every other component | GPL-3.0-or-later; compiled into the executable, so the zip carries the licence and the exact tag/commit |
| extractor jar + runtime jars | the pinned source's own Gradle wrapper builds `:extractor:jar`; an init script copies the runtime classpath | needs JDK 17 (the extractor's toolchain). `git rev-parse` is called by its build, so the build makes a throw-away git repo of the verified archive |
| helper | `newpipe/helper`: `info`, `selftest`, `version`; same JSON as the jar helper the app already uses | `Main.call()` reads optional getters by reflection |
| executable | GraalVM CE `native-image` (JDK 25), Linux on ubuntu-22.04 (older glibc), Windows on windows-2022 | no cross-compiling: one runner per platform |
| TLS | the helper loads certifi's `cacert.pem` (shipped next to it) into its own `SSLContext` | does not depend on where a GraalVM version initialises the default trust store |
| reflection / resources | `newpipe/native-image/.../reachability-metadata.json` (hand written) + the JVM self-test run under the tracing agent | the agent only sees what the self-test touches |

Things found while building this, worth knowing:

- NewPipe Extractor **0.27** turned `Description` into a record: `getContent()` became `content()`. The helper in the app
  repo calls `getContent()` by reflection and silently returns *no description* with 0.27. The helper here tries both.
  In a native build a missing reflection registration fails the same silent way (the detail is just `null`), which is why
  `selftest` checks the reflection path and why the metadata lists every class `Main.call()` can be applied to.
- Rhino runs in interpreted mode inside the extractor (no bytecode generation), which is what makes it usable in a
  native image at all.
- The Windows executable needs the Microsoft Visual C++ runtime (normally present; the app's Python bundle needs it too).

Not covered: arm64 for Windows/Linux, macOS, a musl/static Linux build.

## Licensing (preliminary, not legal advice; re-checked in the app repo)

| component | licence | note |
|---|---|---|
| this repo's scripts | GPL-3.0-or-later | the app is GPLv3 (NewPipeExtractor) |
| NewPipe Extractor | GPL-3.0-or-later | compiled into the native helper; licence + exact tag/commit shipped in the zip |
| jsoup | MIT | Java library inside the helper (licence file taken from the jar when it has one) |
| Rhino | MPL-2.0 | Java library inside the helper |
| protobuf-javalite | BSD-3-Clause | Java library inside the helper |
| nanojson, jsr305 | see the jars | licence files are extracted from the jars into `licenses/jars/`; check them before a public release |
| CPython | PSF-2.0 | ship licence text (done by `package.py`) |
| OpenSSL 3.x | Apache-2.0 | |
| libffi | MIT | |
| FFmpeg | LGPL-2.1-or-later *as configured* | no `--enable-gpl/--enable-nonfree/--enable-version3`; users must be able to replace the library; source offer needed |
| yt-dlp | Unlicense | |
| mutagen | GPL-2.0-or-later | compatible with GPLv3 distribution |
| certifi | MPL-2.0 | |
| idna, websockets | BSD-3-Clause | |
| urllib3, charset-normalizer | MIT | |
| requests, yt-dlp-ejs | Apache-2.0 / Unlicense | |
| vendored patches | MIT (beeware) | see `patches/README.md` |
