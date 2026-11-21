#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Publish the GitHub Releases for an update PR that the gate has just merged: ONE release per tool whose
# version is new (tag = <tool>-v<version>, see scripts/ci/tools.py and publish_releases.py).
# Called by update-sources.yml (job "release"). Needs the `gh` CLI, a full git checkout of the merged
# default branch, and the build artifacts already downloaded into ASSETS_DIR.
#
# It publishes the files that were ALREADY built and smoke-tested for the PR head commit. That is only
# honest if the merged code is exactly that code, so it compares the git trees first. If main moved in
# the meantime it does NOT release; it tells the PR and a person decides (run "build-native").
#
# Required env: PR_NUMBER HEAD_SHA MERGE_SHA PLAN_JSON REPO RUN_ID ASSETS_DIR GH_TOKEN
set -uo pipefail
: "${PR_NUMBER:?}" "${HEAD_SHA:?}" "${MERGE_SHA:?}" "${PLAN_JSON:?}" "${REPO:?}" "${RUN_ID:?}" "${ASSETS_DIR:?}"
TMP="${RELEASE_TMP:-$(mktemp -d)}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say_pr() {  # say_pr <markdown>: print it and put it on the PR
  echo "$1"
  gh pr comment "$PR_NUMBER" --repo "$REPO" --body "$1" >/dev/null 2>&1 || echo "warning: could not post the PR comment" >&2
}

# 1. is the merged code exactly the code that was built and smoke-tested?
if ! git fetch --no-tags --quiet origin "refs/pull/$PR_NUMBER/head"; then
  say_pr "Merged, but **no automatic release**: could not fetch the PR head to compare it with the merge. Run the \`build-native\` workflow to release by hand."
  exit 0
fi
merged_tree="$(git rev-parse "$MERGE_SHA^{tree}" 2>/dev/null || echo none)"
built_tree="$(git rev-parse "$HEAD_SHA^{tree}" 2>/dev/null || echo none2)"
if [ "$merged_tree" != "$built_tree" ]; then
  say_pr "Merged, but **no automatic release**: \`main\` changed while this update was being built, so the merged code is not exactly the code that was built and tested. Run the \`build-native\` workflow to build and release it properly."
  exit 0
fi

# 2. one release per tool that needs one (the publisher checks that the required zips are all there)
python3 "$HERE/publish_releases.py" --plan "$PLAN_JSON" --assets "$ASSETS_DIR" --lock native/sources.lock.toml \
  --repo "$REPO" --target "$MERGE_SHA" --pr "$PR_NUMBER" \
  --run-url "https://github.com/$REPO/actions/runs/$RUN_ID" --tmp "$TMP" > "$TMP/published.md"
rc=$?
case "$rc" in
  0) say_pr "$(cat "$TMP/published.md")" ;;
  2) say_pr "Merged, but **no automatic release**:
$(cat "$TMP/published.md")" ;;
  *) cat "$TMP/published.md"; exit 1 ;;
esac
