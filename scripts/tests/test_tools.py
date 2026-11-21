# SPDX-License-Identifier: GPL-3.0-or-later
"""The tag of a tool is the tool's own version; a tool is released only when that version has no release yet."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "ci"))
import tools  # noqa: E402


def lock():
    return copy.deepcopy(tools.load_lock())


class Tags(unittest.TestCase):
    def test_each_tool_is_tagged_with_its_own_version(self):
        lk = lock()
        for tool, comp in (("yt-dlp", "yt-dlp"), ("cpython", "cpython"), ("ffmpeg", "ffmpeg"), ("newpipe", "newpipe-extractor")):
            self.assertEqual(tools.base_tag(tool, lk), f"{tool}-v{lk[comp]['version']}")

    def test_tag_never_needs_to_be_typed_and_has_no_date(self):
        for tool in tools.TOOLS:
            self.assertNotIn("native-v", tools.base_tag(tool, lock()))

    def test_version_of_tag_roundtrip_and_suffix(self):
        self.assertEqual(tools.version_of_tag("cpython", "cpython-v3.14.10"), "3.14.10")
        self.assertEqual(tools.version_of_tag("cpython", "cpython-v3.14.10.2"), "3.14.10.2")
        with self.assertRaises(tools.ToolsError):
            tools.version_of_tag("cpython", "ffmpeg-v8.1.3")

    def test_tools_have_the_expected_components(self):
        lk = lock()
        self.assertEqual(tools.components_of("cpython", lk), ["cpython", "openssl", "libffi"])
        yt = tools.components_of("yt-dlp", lk)
        self.assertIn("yt-dlp", yt)
        self.assertIn("py-requests", yt)
        self.assertNotIn("cpython", yt)
        # every component of the lock file belongs to exactly one tool, nothing is shipped unreleased
        seen = [c for t in tools.TOOLS for c in tools.components_of(t, lk)]
        self.assertEqual(sorted(seen), sorted(lk))


class Resolve(unittest.TestCase):
    def setUp(self):
        self.lk = lock()
        self.cid = tools.content_id("cpython", self.lk)
        self.base = tools.base_tag("cpython", self.lk)

    def resolve(self, existing):
        return tools.resolve_tag("cpython", self.lk, existing.get)

    def test_no_release_yet_means_release_under_the_base_tag(self):
        self.assertEqual(self.resolve({}), (self.base, True))

    def test_same_inputs_already_released_means_nothing_to_do(self):
        self.assertEqual(self.resolve({self.base: self.cid}), (self.base, False))

    def test_same_version_but_different_inputs_gets_a_suffix(self):
        self.assertEqual(self.resolve({self.base: "000000000000"}), (self.base + ".2", True))
        self.assertEqual(self.resolve({self.base: "000000000000", self.base + ".2": "111111111111"}), (self.base + ".3", True))

    def test_the_suffixed_release_is_recognised_next_time(self):
        self.assertEqual(self.resolve({self.base: "000000000000", self.base + ".2": self.cid}), (self.base + ".2", False))

    def test_a_manually_made_release_without_content_id_is_not_mistaken_for_ours(self):
        self.assertEqual(self.resolve({self.base: ""}), (self.base + ".2", True))

    def test_content_id_changes_with_openssl_but_not_with_other_tools(self):
        lk2 = lock()
        lk2["openssl"]["version"] = "9.9.9"
        self.assertNotEqual(tools.content_id("cpython", lk2), self.cid)
        self.assertEqual(tools.content_id("ffmpeg", lk2), tools.content_id("ffmpeg", self.lk))
        self.assertEqual(tools.base_tag("cpython", lk2), self.base)      # same tool version -> same base tag

    def test_new_tool_version_gets_a_fresh_tag(self):
        lk2 = lock()
        lk2["ffmpeg"]["version"] = "9.0.0"
        lk2["ffmpeg"]["commit"] = "f" * 40
        self.assertEqual(tools.resolve_tag("ffmpeg", lk2, {tools.base_tag("ffmpeg", self.lk): tools.content_id("ffmpeg", self.lk)}.get),
                         ("ffmpeg-v9.0.0", True))


class Plan(unittest.TestCase):
    def test_validation_build_needs_every_tool_and_looks_nothing_up(self):
        plan = tools.make_plan(lock(), False)
        self.assertTrue(all(p["needed"] for p in plan["tools"].values()))

    def test_only_new_builds_just_the_tools_without_a_release(self):
        lk = lock()
        released = {tools.base_tag(t, lk): tools.content_id(t, lk) for t in ("cpython", "ffmpeg", "newpipe")}
        plan = tools.make_plan(lk, True, released.get)
        self.assertEqual({t: p["needed"] for t, p in plan["tools"].items()},
                         {"yt-dlp": True, "cpython": False, "ffmpeg": False, "newpipe": False})
        self.assertEqual(plan["tools"]["cpython"]["tag"], tools.base_tag("cpython", lk))   # points at the existing release

    def test_required_artifacts_follow_the_plan(self):
        lk = lock()
        allp = tools.required_artifacts(tools.make_plan(lk, False))
        self.assertIn("viddown-native-cpython-x86_64", allp)
        self.assertIn("viddown-native-ffmpeg-arm64-v8a", allp)
        self.assertIn("viddown-native-yt-dlp", allp)
        self.assertIn("smoke-passed-x86_64", allp)
        self.assertFalse([n for n in allp if "newpipe" in n], "NewPipe is experimental and never required")
        released = {tools.base_tag(t, lk): tools.content_id(t, lk) for t in ("cpython", "ffmpeg", "newpipe")}
        only = tools.required_artifacts(tools.make_plan(lk, True, released.get))
        self.assertEqual(sorted(only), ["smoke-passed-x86_64", "viddown-native-yt-dlp"])
        everything = {tools.base_tag(t, lk): tools.content_id(t, lk) for t in tools.TOOLS}
        self.assertEqual(tools.required_artifacts(tools.make_plan(lk, True, everything.get)), [])

    def test_cli_writes_step_outputs_for_the_workflow(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            ex = Path(d) / "existing.json"
            lk = lock()
            ex.write_text(json.dumps({tools.base_tag("ffmpeg", lk): tools.content_id("ffmpeg", lk)}))
            env = {"GITHUB_OUTPUT": str(out), "PATH": "/usr/bin:/bin"}
            subprocess.run([sys.executable, str(ROOT / "scripts/ci/tools.py"), "plan", "--only-new", "true", "--existing", str(ex)],
                           check=True, capture_output=True, env=env)
            kv = dict(line.split("=", 1) for line in out.read_text().splitlines())
            self.assertEqual(kv["ffmpeg_needed"], "false")
            self.assertEqual(kv["cpython_needed"], "true")
            self.assertEqual(kv["any_needed"], "true")
            self.assertEqual(kv["cpython_tag"], tools.base_tag("cpython", lk))
            self.assertEqual(kv["cpython_ver"], tools.tool_version("cpython", lk))
            self.assertEqual(json.loads(kv["plan"])["tools"]["ffmpeg"]["needed"], False)


class GhLookup(unittest.TestCase):
    """The real lookup path (`gh release view`), against the fake gh of the shell tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.notes = d / "notes"
        self.notes.mkdir()
        self.env_before = dict(__import__("os").environ)
        env = __import__("os").environ
        env["PATH"] = f"{ROOT / 'scripts/tests/fakebin'}:{env['PATH']}"
        env["FAKE_LOG"] = str(d / "gh.log")
        env["FAKE_NOTES_DIR"] = str(self.notes)
        env["FAKE_RELEASES"] = "with-id no-id"
        (self.notes / "with-id.md").write_text("Notes...\n<!-- content-id: 0123456789ab -->\n")
        (self.notes / "no-id.md").write_text("A release somebody made by hand\n")

    def tearDown(self):
        env = __import__("os").environ
        env.clear()
        env.update(self.env_before)
        self.tmp.cleanup()

    def test_lookup_reads_the_content_id_from_the_release_notes(self):
        look = tools.gh_lookup("o/r")
        self.assertEqual(look("with-id"), "0123456789ab")
        self.assertEqual(look("no-id"), "")
        self.assertIsNone(look("does-not-exist"))

    def test_plan_with_gh_marks_tools_without_a_release_as_needed(self):
        lk = lock()
        __import__("os").environ["FAKE_RELEASES"] = tools.base_tag("ffmpeg", lk)
        (self.notes / (tools.base_tag("ffmpeg", lk) + ".md")).write_text(f"<!-- content-id: {tools.content_id('ffmpeg', lk)} -->")
        plan = tools.make_plan(lk, True, tools.gh_lookup("o/r"))
        self.assertFalse(plan["tools"]["ffmpeg"]["needed"])
        self.assertTrue(plan["tools"]["cpython"]["needed"])


if __name__ == "__main__":
    unittest.main()
