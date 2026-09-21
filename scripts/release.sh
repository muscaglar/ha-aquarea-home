#!/usr/bin/env bash
#
# Tag a release from main.
#
#   scripts/release.sh "<one-line summary>" <notes-file>
#
# The tag name is derived from the version in manifest.json, so the tag and the
# version Home Assistant and HACS show can never disagree. HACS ships whatever
# is tracked under custom_components/ at the tag, hence the checks for a clean
# tree and for hidden files.
#
# This script only ever creates a tag. It never moves or deletes one: once a
# version is public, a mistake is fixed by bumping the patch version. Pushing
# the tag and publishing the release stay manual; the commands are printed at
# the end.
#
# Keep the notes file outside the repository, or the clean-tree check refuses.

set -euo pipefail

die() {
  echo "release: $*" >&2
  exit 1
}

# Single-quote a string for pasting into a shell. printf %q is not used because
# bash 3.2 (macOS) mangles non-ASCII characters such as the dash in the title.
quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

[ $# -eq 2 ] || die "usage: scripts/release.sh \"<one-line summary>\" <notes-file>"
summary=$1
[ -n "$summary" ] || die "the summary is empty"
# Checked before tagging so the printed gh command is known to work.
[ -s "$2" ] || die "notes file '$2' is missing or empty"
notes="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"

cd "$(dirname "${BASH_SOURCE[0]}")/.."
integration=custom_components/aquarea_home
manifest=$integration/manifest.json
[ -f "$manifest" ] || die "$manifest not found"

# 1. The tag must describe exactly what is committed: nothing modified and
#    nothing left untracked. The untracked mode is explicit so that a personal
#    status.showUntrackedFiles=no cannot hide a forgotten file.
dirty=$(git status --porcelain --untracked-files=normal)
[ -z "$dirty" ] || die "working tree is not clean:"$'\n'"$dirty"

# 2. Releases are cut from main, and only from what origin already has, so a
#    tag never points at a commit that is missing from GitHub.
branch=$(git symbolic-ref --quiet --short HEAD || true)
[ "$branch" = main ] || die "on '${branch:-a detached HEAD}', not on main"
git fetch --quiet origin || die "could not fetch origin"
[ "$(git rev-parse HEAD)" = "$(git rev-parse refs/remotes/origin/main)" ] ||
  die "main is not equal to origin/main - push or pull first"

# 3. The version is read, never typed. python3 rather than jq: it is already
#    needed for the tests, jq is one more thing to install.
version=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["version"])' "$manifest") ||
  die "could not read \"version\" from $manifest"
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
  die "unexpected version '$version' in $manifest (expected X.Y.Z)"
tag=v$version
title="$tag — $summary"

# 4. A version is never reused. origin is asked directly because a fetch only
#    brings tags that point into the history it fetched.
if git rev-parse --quiet --verify "refs/tags/$tag" >/dev/null; then
  die "tag $tag already exists locally - bump the version in $manifest"
fi
if git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null; then
  die "tag $tag already exists on origin - bump the version in $manifest"
else
  rc=$?
  # 2 means "no such ref". Anything else is a failed lookup, not an answer.
  [ "$rc" -eq 2 ] || die "could not ask origin about $tag (git ls-remote exit $rc)"
fi

# 5. Nothing hidden may ship. HACS copies what is tracked, so ask git rather
#    than the disk: an ignored Finder .DS_Store must not block a release, but
#    a hidden file that was force-added or committed earlier must.
hidden=$(git ls-files -- "$integration" | grep -E '(^|/)\.[^/]+' || true)
[ -z "$hidden" ] || die "hidden files tracked under $integration - remove them:"$'\n'"$hidden"

git tag -a "$tag" -m "$title"

# --verify-tag makes gh refuse if the tag is not on origin yet. Without it gh
# would create its own tag from the default branch.
cat <<EOF
Created annotated tag $tag at $(git rev-parse --short HEAD). Nothing has been pushed.

To publish, run:

  git push origin $tag
  gh release create $tag --verify-tag --title $(quote "$title") --notes-file $(quote "$notes")
EOF
