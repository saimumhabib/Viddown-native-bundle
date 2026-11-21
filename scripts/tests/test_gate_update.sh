#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Scenario tests for scripts/ci/gate_update.sh using a fake `gh` (scripts/tests/fakebin/gh).
# Proves WHEN an automated source update is merged and when it is not. No network needed.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
mkdir "$W/repo" && cd "$W/repo" || exit 1
tar -C "$SRC" --exclude=build --exclude=dist --exclude=.cache --exclude=__pycache__ --exclude=.git -cf - . | tar -xf -
git init -q -b main . && git -c user.name=t -c user.email=t@t add -A && git -c user.name=t -c user.email=t@t commit -qm base
git update-ref refs/remotes/origin/main HEAD
export PATH="$HERE/fakebin:$PATH" FAKE_LOG="$W/gh.log" FAKE_COMMENT="$W/comment.md" GH_TOKEN=x
export PR_NUMBER=7 HEAD_SHA=abc123 REPO=o/r RUN_ID=99 BASE_BRANCH=main
# Plans as build-native computes them. ALL: every tool is new (needs its artifacts). YTDLP_ONLY: only yt-dlp has a
# new version, the other tools already have a release (so they are not rebuilt and not required).
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
PLAN_NONE="$(python3 - <<'PY'
import json, sys
sys.path.insert(0, "scripts/ci")
import tools
lock = tools.load_lock()
released = {tools.base_tag(t, lock): tools.content_id(t, lock) for t in tools.TOOLS}
print(json.dumps(tools.make_plan(lock, True, released.get)))
PY
)"
# artifacts of a run in which every tool was built (+ an experimental ABI + the smoke marker)
BOTH="viddown-native-yt-dlp viddown-native-cpython-arm64-v8a viddown-native-cpython-x86_64 viddown-native-cpython-x86 viddown-native-ffmpeg-arm64-v8a viddown-native-ffmpeg-x86_64 smoke-passed-x86_64"
fails=0

run() {  # run <name> <yes|no> [VAR=value ...]   yes = must merge, no = must NOT merge
  local name="$1" expect="$2"; shift 2
  rm -rf "$W/tmp"; mkdir "$W/tmp"; : > "$FAKE_LOG"; : > "$W/out"
  (
    export GATE_TMP="$W/tmp" BUILD_RESULT=success TESTS_OUTCOME=success AUTO_MERGE=true
    export PLAN_JSON="$PLAN_ALL" FAKE_ARTIFACTS="$BOTH" FAKE_HEAD=abc123 FAKE_MERGE_SHA=feed456 FAKE_MERGE_RC=0 GITHUB_OUTPUT="$W/out"
    for kv in "$@"; do export "${kv?}"; done
    bash "$SRC/scripts/ci/gate_update.sh" >/dev/null 2>&1
  )
  local merged called ok=FAIL
  merged="$(grep -o 'merged=.*' "$W/out")"; called="$(grep -c 'pr merge' "$FAKE_LOG")"
  if [ "$expect" = yes ] && [ "$called" = 1 ] && [ "$merged" = "merged=true" ]; then ok=PASS; fi
  if [ "$expect" = no ] && [ "$merged" = "merged=false" ]; then ok=PASS; fi
  [ "$ok" = PASS ] || fails=$((fails + 1))
  printf '%-4s %-52s merge-called=%s %s\n' "$ok" "$name" "$called" "$merged"
}

run "all conditions met, auto-merge on"                 yes
grep -qx 'merge_sha=feed456' "$W/out" || { echo "FAIL the merge commit sha was not reported for the release job"; fails=$((fails + 1)); }
grep -q -- '--match-head-commit abc123' "$FAKE_LOG" || { echo "FAIL merge did not pin the head commit"; fails=$((fails + 1)); }
run "smoke test did not pass (no marker artifact)"       no  "FAKE_ARTIFACTS=${BOTH/smoke-passed-x86_64/}"
grep -qx 'merge_sha=' "$W/out" || { echo "FAIL merge_sha must be empty when nothing was merged"; fails=$((fails + 1)); }
grep -q "smoke-passed-x86_64" "$FAKE_COMMENT" || { echo "FAIL the comment does not name the missing smoke marker"; fails=$((fails + 1)); }
run "cpython x86_64 build missing"                      no  "FAKE_ARTIFACTS=${BOTH/viddown-native-cpython-x86_64/}"
run "ffmpeg arm64-v8a build missing"                    no  "FAKE_ARTIFACTS=${BOTH/viddown-native-ffmpeg-arm64-v8a/}"
run "yt-dlp zip missing"                                no  "FAKE_ARTIFACTS=${BOTH/viddown-native-yt-dlp /}"
run "experimental ABI failed, required ones fine"       yes BUILD_RESULT=failure "FAKE_ARTIFACTS=${BOTH/viddown-native-cpython-x86 /}"
run "failure AND smoke marker missing"                  no  BUILD_RESULT=failure "FAKE_ARTIFACTS=${BOTH/smoke-passed-x86_64/}"
# only the tool with a NEW version is built and required
run "only yt-dlp is new: its zip + smoke marker suffice" yes "PLAN_JSON=$PLAN_YTDLP_ONLY" "FAKE_ARTIFACTS=viddown-native-yt-dlp smoke-passed-x86_64"
run "only yt-dlp is new, but its zip is missing"        no  "PLAN_JSON=$PLAN_YTDLP_ONLY" "FAKE_ARTIFACTS=smoke-passed-x86_64"
run "no tool is new: nothing to build, nothing required" yes "PLAN_JSON=$PLAN_NONE" "FAKE_ARTIFACTS="
run "no plan at all (build never planned)"              no  "PLAN_JSON="
grep -q "no plan" "$FAKE_COMMENT" || { echo "FAIL the comment does not say the plan is missing"; fails=$((fails + 1)); }
run "build cancelled"                                   no  BUILD_RESULT=cancelled
run "PR branch moved during the build"                  no  FAKE_HEAD=zzz999
run "all good but auto-merge OFF"                       no  AUTO_MERGE=false
run "unit tests failed"                                 no  TESTS_OUTCOME=failure
run "merge refused by GitHub (branch protection)"       no  FAKE_MERGE_RC=1
grep -q refused "$FAKE_COMMENT" || { echo "FAIL the comment does not explain the refusal"; fails=$((fails + 1)); }

# a pinned hash silently changed for the SAME version must block the merge
python3 - <<'PY'
import re
p = "native/sources.lock.toml"
s = open(p).read()
s = re.sub(r'(name = "pypi-sdist"\ntype = "tarball"\nurl = "[^"]*certifi[^"]*"\nsha256 = ")[0-9a-f]{64}', r"\g<1>" + "0" * 64, s, count=1)
open(p, "w").write(s)
PY
run "a pinned hash changed for the same version"        no
grep -q "CHANGED for the same version" "$FAKE_COMMENT" || { echo "FAIL the comment does not show the policy reason"; fails=$((fails + 1)); }

[ "$fails" = 0 ] && echo "ALL GATE SCENARIOS OK" || echo "$fails GATE SCENARIO(S) FAILED"
exit "$fails"
