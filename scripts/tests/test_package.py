# SPDX-License-Identifier: GPL-3.0-or-later
"""package.py makes one zip PER TOOL out of what the build scripts left in dist/."""
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


class Package(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.dist = d / "dist"
        abi = cls.dist / "x86_64"
        for rel in ("jniLibs/x86_64/libviddown_python.so", "jniLibs/x86_64/libpython3.14.so", "jniLibs/x86_64/libssl_python.so",
                    "jniLibs/x86_64/libcrypto_python.so", "jniLibs/x86_64/libffmpeg.so", "jniLibs/x86_64/libffprobe.so",
                    "assets/python-lib-3.14-x86_64.zip", "assets/python-version.txt", "metadata/ffmpeg-configure-x86_64.txt"):
            (abi / rel).parent.mkdir(parents=True, exist_ok=True)
            (abi / rel).write_bytes(rel.encode())
        for rel in ("assets/ytdlp-site-packages.zip", "metadata/python-packages.json"):
            (cls.dist / "shared" / rel).parent.mkdir(parents=True, exist_ok=True)
            (cls.dist / "shared" / rel).write_bytes(rel.encode())
        cls.d = d

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def package(self, *args):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/package.py"), *args, "--dist", str(self.dist),
                            "--sources", str(self.d / "src"), "--build", str(self.d / "build")], capture_output=True, text=True)
        return r

    def names(self, zip_name):
        with zipfile.ZipFile(self.dist / "release" / zip_name) as z:
            return {n for n in z.namelist() if not n.startswith("licenses/")}

    def test_ytdlp_zip_has_only_the_site_packages(self):
        self.assertEqual(self.package("yt-dlp", "--version-tag", "2026.10.26").returncode, 0)
        n = self.names("viddown-native-yt-dlp-2026.10.26.zip")
        self.assertIn("assets/ytdlp-site-packages.zip", n)
        self.assertFalse([x for x in n if x.startswith("jniLibs/")])

    def test_cpython_zip_has_python_and_openssl_but_not_ffmpeg(self):
        self.assertEqual(self.package("cpython", "x86_64", "--version-tag", "3.14.10").returncode, 0)
        n = self.names("viddown-native-cpython-x86_64-3.14.10.zip")
        for f in ("jniLibs/x86_64/libviddown_python.so", "jniLibs/x86_64/libssl_python.so", "assets/python-lib-3.14-x86_64.zip"):
            self.assertIn(f, n)
        self.assertNotIn("jniLibs/x86_64/libffmpeg.so", n)
        self.assertNotIn("metadata/ffmpeg-configure-x86_64.txt", n)

    def test_ffmpeg_zip_has_only_ffmpeg(self):
        self.assertEqual(self.package("ffmpeg", "x86_64", "--version-tag", "8.1.3").returncode, 0)
        n = self.names("viddown-native-ffmpeg-x86_64-8.1.3.zip")
        self.assertIn("jniLibs/x86_64/libffmpeg.so", n)
        self.assertIn("jniLibs/x86_64/libffprobe.so", n)
        self.assertNotIn("jniLibs/x86_64/libviddown_python.so", n)
        self.assertFalse([x for x in n if x.startswith("assets/")])

    def test_build_info_names_the_tool_and_its_release_tag(self):
        self.package("ffmpeg", "x86_64", "--version-tag", "8.1.3")
        with zipfile.ZipFile(self.dist / "release" / "viddown-native-ffmpeg-x86_64-8.1.3.zip") as z:
            info = json.loads(z.read("BUILD_INFO.json"))
        self.assertEqual((info["tool"], info["release_tag"], info["abi"]), ("ffmpeg", "ffmpeg-v8.1.3", "x86_64"))
        self.assertEqual(len(info["content_id"]), 12)

    def test_abi_rules(self):
        self.assertNotEqual(self.package("cpython", "--version-tag", "1").returncode, 0)          # needs an ABI
        self.assertNotEqual(self.package("yt-dlp", "x86_64", "--version-tag", "1").returncode, 0)  # has none
        self.assertNotEqual(self.package("ffmpeg", "x86_64").returncode, 0)                        # tag is required


if __name__ == "__main__":
    unittest.main()
