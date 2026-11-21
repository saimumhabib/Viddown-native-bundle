#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Scenario tests for scripts/ci/release_update.sh (+ publish_releases.py) with a fake `gh` and real local
# git repositories: ONE release per changed tool, tagged <tool>-v<version>; when nothing is released.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
GIT=(git -c user.name=t -c user.email=t@t)
export PATH="$HERE/fakebin:$PATH" FAKE_LOG="$W/gh.log" FAKE_COMMENT="$W/comment.md" FAKE_NOTES_DIR="$W/notes" GH_TOKEN=x
fails=0
bad() { echo "FAIL $*"; fails=$((fails + 1)); }

# remote with: base commit, PR head commit B (refs/pull/7/head)
git init -q --bare "$W/origin.git"
git init -q -b main "$W/repo" && cd "$W/repo" || exit 1
tar -C "$SRC" --exclude=build --exclude=dist --exclude=.cache --exclude=__pycache__ --exclude=.git -cf - . | tar -xf -
git remote add origin "$W/origin.git"
"${GIT[@]}" add -A && "${GIT[@]}" commit -qm base
BASE="$(git rev-parse HEAD)"
echo "lock change" > pin-change.txt && "${GIT[@]}" add -A && "${GIT[@]}" commit -qm "PR head"
B="$(git rev-parse HEAD)"
git push -q origin "$B:refs/pull/7/head" "$BASE:refs/heads/main"
SQUASH_SAME="$("${GIT[@]}" commit-tree "$B^{tree}" -p "$BASE" -m squash)"            # same tree as the PR head
echo "someone else pushed" > other.txt && "${GIT[@]}" add -A
TREE_DIFF="$(git write-tree)"
SQUASH_DIFF="$("${GIT[@]}" commit-tree "$TREE_DIFF" -p "$BASE" -m squash2)"           # tree differs from the PR head
git push -q origin "$SQUASH_SAME:refs/heads/same" "$SQUASH_DIFF:refs/heads/diff"
git fetch -q origin && git reset -q --hard "$B"

# the tools' own versions, from the lock file of this repo copy
read -r YT CP FF NP < <(python3 - <<'PY'
import sys
sys.path.insert(0, "scripts/ci")
import tools
lock = tools.load_lock()
print(*[tools.tool_version(t, lock) for t in ("yt-dlp", "cpython", "ffmpeg", "newpipe")])
PY
)
PLAN_ALL="$(python3 - <<'PY'
import json, sys
sys.path.insert(0, "scripts/ci")
import tools
print(json.dumps(tools.make_plan(tools.load_lock(), False)))
PY
)"
PLAN_YTDLP_ONLY="$(python3 - <<'PY'
import json, sys
sys.path.insert(0, "scripts/ci")
import tools
lock = tools.load_lock()
released = {tools.base_tag(t, lock): tools.content_id(t, lock) for t in ("cpython", "ffmpeg", "newpipe")}
print(json.dumps(tools.make_plan(lock, True, released.get)))
PY
)"

mk() { for f in "$@"; do echo z > "$W/assets/$f"; done; }   # mk <zip-name>...
fresh() { rm -rf "$W/assets" "$W/notes" "$W/tmp"; mkdir -p "$W/assets" "$W/tmp"; }
all_zips() {   # a complete build of every tool
  mk "viddown-native-yt-dlp-$YT.zip" \
     "viddown-native-cpython-arm64-v8a-$CP.zip" "viddown-native-cpython-x86_64-$CP.zip" "viddown-native-cpython-x86-$CP.zip" \
     "viddown-native-ffmpeg-arm64-v8a-$FF.zip" "viddown-native-ffmpeg-x86_64-$FF.zip" \
     "viddown-native-newpipe-linux-x86_64-$NP.zip"
}
run() {  # run <name> <yes|no> <merge-sha> <plan-json>      yes = at least one release is created
  : > "$FAKE_LOG"; rm -f "$FAKE_COMMENT"
  ( export PR_NUMBER=7 HEAD_SHA="$B" MERGE_SHA="$3" PLAN_JSON="$4" REPO=o/r RUN_ID=99 ASSETS_DIR="$W/assets" RELEASE_TMP="$W/tmp"
    bash "$SRC/scripts/ci/release_update.sh" >/dev/null 2>&1 )
  local called ok=FAIL; called="$(grep -c 'release create' "$FAKE_LOG")"
  { [ "$2" = yes ] && [ "$called" -ge 1 ]; } && ok=PASS
  { [ "$2" = no ] && [ "$called" = 0 ]; } && ok=PASS
  [ "$ok" = PASS ] || fails=$((fails + 1))
  printf '%-4s %-62s release-create-calls=%s\n' "$ok" "$1" "$called"
}
created() { grep -c "gh release create $1 " "$FAKE_LOG"; }

fresh; all_zips
run "every tool is new, merged tree == built tree" yes "$SQUASH_SAME" "$PLAN_ALL"
for t in "yt-dlp-v$YT" "cpython-v$CP" "ffmpeg-v$FF" "newpipe-v$NP"; do
  [ "$(created "$t")" = 1 ] || bad "no release was created with the tool's own version as tag: $t"
done
[ "$(grep -c 'gh release create' "$FAKE_LOG")" = 5 ] || bad "expected 4 tool releases + the index release"
grep -q -- "--target $SQUASH_SAME" "$FAKE_LOG" || bad "releases are not pinned to the merge commit"
grep -q -- "--latest=false" "$FAKE_LOG" || bad "tool releases must not compete for the 'latest' badge"
grep -q "SHA256SUMS.txt" "$FAKE_LOG" || bad "SHA256SUMS.txt is not among the release assets"
grep -q "\.zip\.sha256" "$FAKE_LOG" && bad "stale per-zip .sha256 files were uploaded"
# only the zips of ONE tool go into that tool's release
create_cp="$(grep "gh release create cpython-v$CP " "$FAKE_LOG")"
case "$create_cp" in *yt-dlp*|*ffmpeg*|*newpipe*) bad "cpython release contains another tool's files: $create_cp" ;; esac
grep -q "executed in CI" "$FAKE_NOTES_DIR/cpython-v$CP.md" || bad "cpython notes do not say what was run"
grep -q "content-id: [0-9a-f]\{12\}" "$FAKE_NOTES_DIR/yt-dlp-v$YT.md" || bad "notes carry no content id (needed to skip an unchanged tool next time)"
grep -q "Released \*\*cpython-v$CP\*\*" "$FAKE_COMMENT" || bad "PR was not told about the cpython release"
grep -q "release upload index" "$FAKE_LOG" || bad "components.json was not uploaded to the index release"
python3 - "$W/tmp/components.json" "$YT" "$CP" "$FF" "$NP" <<'PY' || bad "components.json is wrong"
import json, sys
d = json.load(open(sys.argv[1]))["tools"]
want = {"yt-dlp": sys.argv[2], "cpython": sys.argv[3], "ffmpeg": sys.argv[4], "newpipe": sys.argv[5]}
assert set(d) == set(want), sorted(d)
for t, v in want.items():
    assert d[t]["tag"] == f"{t}-v{v}" and d[t]["version"] == v, (t, d[t])
PY

fresh; all_zips
run "main moved meanwhile (trees differ): NO release" no "$SQUASH_DIFF" "$PLAN_ALL"
grep -q "main\` changed" "$FAKE_COMMENT" || bad "the PR comment does not explain why"

fresh; all_zips; rm "$W/assets/viddown-native-cpython-x86_64-$CP.zip"
run "a required zip (cpython x86_64) is missing: NO release at all" no "$SQUASH_SAME" "$PLAN_ALL"
grep -q "x86_64 zip of cpython" "$FAKE_COMMENT" || bad "missing-zip reason not shown"

fresh; all_zips; rm "$W/assets/viddown-native-newpipe-linux-x86_64-$NP.zip"
run "NewPipe zip missing (experimental): the others ARE released" yes "$SQUASH_SAME" "$PLAN_ALL"
[ "$(created "newpipe-v$NP")" = 0 ] || bad "newpipe was released without a zip"
[ "$(created "cpython-v$CP")" = 1 ] || bad "cpython was not released although only newpipe failed"
grep -q "not\*\* released" "$FAKE_COMMENT" || bad "the comment does not say newpipe was left out"
python3 - "$W/tmp/components.json" <<'PY' || bad "components.json lists a tool that has no release"
import json, sys
assert "newpipe" not in json.load(open(sys.argv[1]))["tools"]
PY

fresh; mk "viddown-native-yt-dlp-$YT.zip"
run "only yt-dlp is new: ONLY the yt-dlp release is created" yes "$SQUASH_SAME" "$PLAN_YTDLP_ONLY"
[ "$(created "yt-dlp-v$YT")" = 1 ] || bad "yt-dlp release missing"
[ "$(created "cpython-v$CP")" = 0 ] && [ "$(created "ffmpeg-v$FF")" = 0 ] || bad "unchanged tools were released again"
python3 - "$W/tmp/components.json" "$CP" <<'PY' || bad "components.json must still point at the unchanged tools' existing releases"
import json, sys
d = json.load(open(sys.argv[1]))["tools"]
assert d["cpython"]["tag"] == f"cpython-v{sys.argv[2]}", d["cpython"]
PY

fresh; all_zips
run "unknown merge commit: NO release" no "0000000000000000000000000000000000000000" "$PLAN_ALL"

[ "$fails" = 0 ] && echo "ALL RELEASE SCENARIOS OK" || echo "$fails RELEASE SCENARIO(S) FAILED"
exit "$fails"
