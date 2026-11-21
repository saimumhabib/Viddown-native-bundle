# SPDX-License-Identifier: GPL-3.0-or-later
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "ci"))
import tools  # noqa: E402


class ReleaseNotes(unittest.TestCase):
    def notes(self, tool, names, tag=None, **extra):
        with tempfile.TemporaryDirectory() as d:
            for n in names:
                (Path(d) / n).write_bytes(b"x")
            cmd = [sys.executable, str(ROOT / "scripts/ci/release_notes.py"), "--lock", str(ROOT / "native/sources.lock.toml"),
                   "--tool", tool, "--tag", tag or f"{tool}-v1", "--assets", d]
            for k, v in extra.items():
                cmd += [f"--{k.replace('_', '-')}", v]
            return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout

    def test_only_x86_64_is_claimed_as_executed(self):
        t = self.notes("cpython", ["viddown-native-cpython-arm64-v8a-3.14.10.zip", "viddown-native-cpython-x86_64-3.14.10.zip",
                                   "viddown-native-cpython-x86-3.14.10.zip", "SHA256SUMS.txt"])
        self.assertIn("**x86_64**: built, and executed in CI", t)
        self.assertIn("**arm64-v8a**: built from source, **not run-tested in CI**", t)
        self.assertIn("**x86**: built from source, **not run-tested in CI**", t)
        self.assertNotIn("armeabi-v7a", t)  # not present, so not mentioned

    def test_a_release_lists_only_its_own_components(self):
        t = self.notes("cpython", ["viddown-native-cpython-x86_64-3.14.10.zip"], pr="7", repo="o/r", run_url="https://x/run/1")
        for c in ("| cpython |", "| openssl |", "| libffi |"):
            self.assertIn(c, t)
        for c in ("| yt-dlp |", "| ffmpeg |", "| newpipe-extractor |"):
            self.assertNotIn(c, t)
        self.assertIn("pull/7", t)
        self.assertIn("https://x/run/1", t)
        self.assertIn("BUILD_INFO.json", t)

    def test_ytdlp_release_lists_the_pure_python_dependencies(self):
        t = self.notes("yt-dlp", ["viddown-native-yt-dlp-2026.10.26.zip"])
        self.assertIn("| yt-dlp |", t)
        self.assertIn("| py-requests |", t)
        self.assertNotIn("| cpython |", t)

    def test_newpipe_helper_is_listed_per_platform_with_its_limits(self):
        t = self.notes("newpipe", ["viddown-native-newpipe-linux-x86_64-0.27.1.zip", "viddown-native-newpipe-windows-x86_64-0.27.1.zip"])
        self.assertIn("**NewPipe helper, linux-x86_64**", t)
        self.assertIn("**NewPipe helper, windows-x86_64**", t)
        self.assertIn("**not** part of that gate", t)          # the YouTube fetch is not claimed
        self.assertIn("| newpipe-extractor |", t)

    def test_newpipe_zips_are_not_mistaken_for_android_abis(self):
        t = self.notes("cpython", ["viddown-native-newpipe-linux-x86_64-0.27.1.zip"])
        self.assertNotIn("**x86_64**: built", t)
        self.assertNotIn("**x86**: built", t)

    def test_notes_carry_the_content_id_that_tools_py_reads_back(self):
        t = self.notes("ffmpeg", ["viddown-native-ffmpeg-x86_64-8.1.3.zip"])
        lock = tools.load_lock()
        m = tools.CONTENT_ID_RX.search(t)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), tools.content_id("ffmpeg", lock))


if __name__ == "__main__":
    unittest.main()
