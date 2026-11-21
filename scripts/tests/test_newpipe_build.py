# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure functions of the NewPipe build, the packaging, and the smoke script (against a fake helper).

What this can NOT show: that Gradle builds the extractor or that native-image compiles the helper.
Only the CI run of the 'newpipe' job shows that.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOCK = tomllib.loads((ROOT / "native/sources.lock.toml").read_text())["components"]


def load(path: str):
    spec = importlib.util.spec_from_file_location(Path(path).stem, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = load("scripts/build/build_newpipe.py")


def add(t: tarfile.TarFile, name: str, data: bytes) -> None:
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    t.addfile(ti, io.BytesIO(data))


class PureFunctions(unittest.TestCase):
    def test_build_info_gets_the_pinned_version_and_commit(self):
        src = (ROOT / "newpipe/helper/src/main/java/viddown/newpipe/BuildInfo.java").read_text()
        out = B.patch_build_info(src, "0.27.1", "abc123")
        self.assertIn('EXTRACTOR_VERSION = "0.27.1";', out)
        self.assertIn('EXTRACTOR_COMMIT = "abc123";', out)
        self.assertIn("HELPER_API = 1;", out)

    def test_build_info_patch_refuses_a_file_without_the_constants(self):
        with self.assertRaises(B.BuildError):
            B.patch_build_info("final class BuildInfo { }", "1", "2")

    def test_extract_strips_the_top_directory_and_skips_links(self):
        with tempfile.TemporaryDirectory() as d:
            arc = Path(d) / "a.tar.gz"
            with tarfile.open(arc, "w:gz") as t:
                add(t, "Proj-1.0/gradlew", b"#!/bin/sh\n")
                add(t, "Proj-1.0/extractor/build.gradle.kts", b"x")
                link = tarfile.TarInfo("Proj-1.0/evil")
                link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
                t.addfile(link)
            dest = Path(d) / "out"
            B.extract_stripped(arc, dest)
            self.assertTrue((dest / "gradlew").is_file())
            self.assertTrue((dest / "extractor/build.gradle.kts").is_file())
            self.assertFalse((dest / "evil").exists() or (dest / "evil").is_symlink())

    def test_extract_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            arc = Path(d) / "a.tar.gz"
            with tarfile.open(arc, "w:gz") as t:
                add(t, "Proj-1.0/../../escape.txt", b"x")
            with self.assertRaises(B.BuildError):
                B.extract_stripped(arc, Path(d) / "out")

    def test_extractor_jar_pick(self):
        with tempfile.TemporaryDirectory() as d:
            libs = Path(d)
            for n in ("extractor-v0.27.1.jar", "extractor-v0.27.1-sources.jar", "extractor-v0.27.1-javadoc.jar"):
                (libs / n).write_bytes(b"x")
            self.assertEqual(B.pick_extractor_jar(libs).name, "extractor-v0.27.1.jar")
            (libs / "extractor-other.jar").write_bytes(b"x")
            with self.assertRaises(B.BuildError):
                B.pick_extractor_jar(libs)

    def test_wrapper_properties(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "gradle-wrapper.properties"
            f.write_text("distributionSha256Sum=abc\ndistributionUrl=https\\://services.gradle.org/distributions/gradle-9.0-bin.zip\n")
            info = B.read_wrapper_info(f)
            self.assertEqual(info["distribution_sha256"], "abc")
            self.assertEqual(info["distribution_url"], "https://services.gradle.org/distributions/gradle-9.0-bin.zip")

    def test_the_component_is_pinned_with_two_ways_to_verify_it(self):
        c = LOCK["newpipe-extractor"]
        self.assertEqual(c["kind"], "jvm-library")
        self.assertRegex(c["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual({s["type"] for s in c["sources"]}, {"tarball", "git"})
        self.assertRegex(c["sources"][0]["sha256_uncompressed"], r"^[0-9a-f]{64}$")

    def test_native_image_metadata_registers_every_reflection_target_of_main(self):
        meta = json.loads((ROOT / "newpipe/native-image/META-INF/native-image/viddown/newpipe-helper/reachability-metadata.json").read_text())
        registered = {r["type"] for r in meta["reflection"]}
        for needed in ("org.schabi.newpipe.extractor.stream.StreamInfo", "org.schabi.newpipe.extractor.stream.VideoStream",
                       "org.schabi.newpipe.extractor.stream.AudioStream", "org.schabi.newpipe.extractor.stream.SubtitlesStream",
                       "org.schabi.newpipe.extractor.stream.Description", "org.schabi.newpipe.extractor.playlist.PlaylistInfo",
                       "org.schabi.newpipe.extractor.MediaFormat"):
            self.assertIn(needed, registered)
        self.assertTrue(any("PlaylistProtobufContinuation$PlaylistContinuation" in t for t in registered))


FAKE_HELPER = r"""#!/bin/sh
here="$(cd "$(dirname "$0")" && pwd)"
case "$1" in
  version) echo '{"kind":"version","extractor_version":"%(ver)s","extractor_commit":"%(commit)s"}' ;;
  info) case "$2" in
          *youtu*) echo '{"error":"Connection refused","kind":"ConnectException"}'; exit 2 ;;
          *) echo '{"error":"No service can handle the url","kind":"ExtractionException"}'; exit 2 ;;
        esac ;;
  selftest) echo "{\"kind\":\"selftest\",\"ok\":true,\"checks\":{\"tls\":\"ok: HTTP 200, CA bundle: $here/cacert.pem\"}}" ;;
esac
"""


@unittest.skipIf(os.name == "nt", "the fake helper is a shell script")
class PackageAndSmoke(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.dist, self.src = t / "dist", t / "sources"
        staged = self.dist / "newpipe-linux-x86_64"
        (staged / "bin/newpipe").mkdir(parents=True)
        (staged / "metadata").mkdir()
        ex = LOCK["newpipe-extractor"]
        exe = staged / "bin/newpipe/viddown-newpipe"
        exe.write_text(FAKE_HELPER % {"ver": ex["version"], "commit": ex["commit"]})
        exe.chmod(0o755)
        (staged / "bin/newpipe/cacert.pem").write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n")
        jar = t / "jsoup-1.0.jar"
        with zipfile.ZipFile(jar, "w") as z:
            z.writestr("META-INF/LICENSE", "MIT text")
            z.writestr("org/jsoup/X.class", b"x")
        (staged / "metadata/newpipe-build.json").write_text(json.dumps({
            "classpath": [str(jar)], "maven_dependencies": [{"file": jar.name, "sha256": "ab" * 32}],
            "extractor_jar": {"file": "extractor.jar", "sha256": "cd" * 32}, "native_image": "native-image 25", "jdk17": "17", "agent_config_used": True}))
        for name in ("newpipe-extractor", "py-certifi"):
            e = LOCK[name]
            d = self.src / f"{name}-{e['version']}"
            d.mkdir(parents=True)
            with tarfile.open(d / "a.tar.gz", "w:gz") as tf:
                add(tf, f"{name}-{e['version']}/LICENSE", f"licence of {name}".encode())
            (d / "provenance.json").write_text(json.dumps({
                "version": e["version"], "license": e["license"], "commit": e.get("commit"),
                "source_used": {"name": "x", "url": "https://x"}, "evidence": {}, "archive": "a.tar.gz",
                "archive_sha256": "ef" * 32, "attempts": []}))

    def package(self) -> Path:
        subprocess.run([sys.executable, str(ROOT / "scripts/package_newpipe.py"), "linux-x86_64", "--dist", str(self.dist),
                        "--sources", str(self.src), "--version-tag", "native-vTEST"], check=True, capture_output=True)
        return self.dist / "release/viddown-native-newpipe-linux-x86_64-native-vTEST.zip"

    def test_zip_layout_licences_and_checksums(self):
        z = zipfile.ZipFile(self.package())
        names = set(z.namelist())
        for n in ("bin/newpipe/viddown-newpipe", "bin/newpipe/cacert.pem", "BUILD_INFO.json", "THIRD_PARTY_NOTICES.md",
                  "SHA256SUMS", "licenses/newpipe-extractor/LICENSE", "licenses/py-certifi/LICENSE", "licenses/jars/jsoup-1.0/LICENSE"):
            self.assertIn(n, names)
        self.assertEqual(z.getinfo("bin/newpipe/viddown-newpipe").external_attr >> 16, 0o755)
        info = json.loads(z.read("BUILD_INFO.json"))
        self.assertEqual(info["kind"], "newpipe-helper")
        self.assertIn("newpipe-extractor", info["components"])
        self.assertTrue(info["unverified_notes"])
        notices = z.read("THIRD_PARTY_NOTICES.md").decode()
        self.assertIn("jsoup-1.0.jar", notices)
        self.assertIn("GPL-3.0-or-later", notices)

    def test_smoke_passes_on_a_well_behaved_helper(self):
        zpath = self.package()
        p = subprocess.run([sys.executable, str(ROOT / "scripts/smoke/newpipe_smoke.py"), str(zpath), "--no-probe"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ok: selftest", p.stdout)

    def test_smoke_fails_when_a_youtube_address_is_not_recognised(self):
        # a helper that refuses every address still answers the unsupported-address check correctly,
        # so the smoke test must also try a real YouTube address
        exe = self.dist / "newpipe-linux-x86_64/bin/newpipe/viddown-newpipe"
        exe.write_text(exe.read_text().replace("Connection refused", "No service can handle the url"))
        zpath = self.package()
        p = subprocess.run([sys.executable, str(ROOT / "scripts/smoke/newpipe_smoke.py"), str(zpath), "--no-probe"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("YouTube", p.stderr)
        self.assertIn("does not recognise", p.stderr)

    def test_native_build_enables_the_url_protocols(self):
        # NewPipe Extractor parses addresses with java.net.URL, which needs the handlers in a native build
        src = (ROOT / "scripts/build/build_newpipe.py").read_text()
        self.assertIn("--enable-url-protocols=http,https", src)

    def test_selftest_covers_address_recognition(self):
        src = (ROOT / "newpipe/helper/src/main/java/viddown/newpipe/SelfTest.java").read_text()
        for needed in ('"url-handler"', '"service-urls"', "NewPipe.getServiceByUrl"):
            self.assertIn(needed, src)

    def test_smoke_fails_when_the_binary_reports_another_version(self):
        zpath = self.package()
        bad = self.dist / "bad.zip"
        with zipfile.ZipFile(zpath) as zin, zipfile.ZipFile(bad, "w") as zout:
            for n in zin.namelist():
                data = zin.read(n)
                if n == "bin/newpipe/viddown-newpipe":
                    data = data.replace(LOCK["newpipe-extractor"]["version"].encode(), b"0.0.1")
                zout.writestr(n, data)
        # the hashes inside the zip no longer match either: either check must reject it
        p = subprocess.run([sys.executable, str(ROOT / "scripts/smoke/newpipe_smoke.py"), str(bad), "--no-probe"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("FAIL", p.stderr)


if __name__ == "__main__":
    unittest.main()
